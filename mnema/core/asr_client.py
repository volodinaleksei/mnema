"""ASR-klient (FT-12/FT-40). Vneshniy gigastt cherez OpenAI-sovmestimy endpoint.

GET /v1/audio/transcriptions (multipart WAV) -> verbose_json segment.
Faster-whisper fallback esli ASR=not_configured/misconfigured (FT-39).
"""
from __future__ import annotations

import io
import logging
import os
import time
import wave
from pathlib import Path

from .capabilities import capability_status

log = logging.getLogger("mnema.asr")

ASR_CHUNK_RETRIES = int(os.environ.get("ASR_CHUNK_RETRIES", "3") or 3)


def engine_name() -> str:
    return os.environ.get("ASR_ENGINE", "gigastt").strip() or "gigastt"


def _max_upload_bytes() -> int:
    """LIMIT: gigastt otkluchaet telos zaprosa svershe ~48 MB (HTTP 400
    invalid_multipart). PO-UMOLCHANIYU 40 MB, OVERRIDE: ASR_MAX_UPLOAD_MB."""
    mb = os.environ.get("ASR_MAX_UPLOAD_MB", "40").strip() or "40"
    try:
        return max(1, int(float(mb))) * 1024 * 1024
    except ValueError:
        return 40 * 1024 * 1024


def transcribe_segments(wav: Path, vad_segments: list[dict]) -> list[dict]:
    """Vozvraschaet [{text, lang, confidence}] paralleno vad_segments."""
    caps = capability_status()["asr"]
    use_remote = caps["state"] == "configured"
    if use_remote:
        try:
            return _gigastt_transcribe(wav, vad_segments)
        except Exception as e:  # noqa: BLE001
            log.warning("remote ASR failed (%s); falling back to faster-whisper", e)
    return _faster_whisper_transcribe(wav, vad_segments)


def _gigastt_transcribe(wav: Path, vad_segments: list[dict]) -> list[dict]:
    payload = _gigastt_chunks(wav)
    return _map_verbose_to_segments(payload, vad_segments)


def _norm_word(w: str) -> str:
    """Bukvenno-cifrovoy skeleton slova dlya sravneniya (bez punktua i regitra)."""
    return "".join(ch for ch in w.lower() if ch.isalnum() or ch == "'")


def _trail_punct(t: str) -> str:
    """Khvostovaya punktuaciya tokena (poslednie ne-bukvenno-cifrovye simvoly)."""
    out: list[str] = []
    for ch in reversed(t):
        if ch.isalnum() or ch == "'":
            break
        out.append(ch)
    return "".join(reversed(out))


_ALIGN_WINDOW = 12


def _retrofit_punctuation(payload: dict) -> dict:
    """gigastt v verbose_json derzhit segments[].text syrym (bez punktuacii i
    registra) - perzeom, ITN i punktuaciya privyatsya tol'ko k top-level 'text'
    (docs/api.md gigastt).

    Lineynoe dvukh-tochechnoe vyravnivanie syryh slov s tokenami top-level
    text po skeletam (registr/punktuaciya ne vazhny): sovpadshiy token
    perenositsya v segment (registr i znaki), nesovpadshie (ITN zamenil slovo
    cifroy) ostayutsya syrymi, ikh punktuaciya pri rezhime dospevaet k
    predydushemu slovu. Okno posiska ograniceno - difflib SequenceMatcher na
    povtoruyushimsya legal'nom tekste 2-3 chasov (odnim POST pri
    ASR_MAX_UPLOAD_MB>=1 GB) kvadratichnyy i vesit worker. Esli sovmalenie
    khuzhe ~30% - segmenty ne trogayu."""
    top = (payload.get("text") or "").strip()
    segs = payload.get("segments") or []
    if not top or not segs:
        return payload
    top_tokens = top.split()
    raw_words: list[str] = []
    counts: list[int] = []
    for s in segs:
        w = (s.get("text") or "").split()
        counts.append(len(w))
        raw_words.extend(w)
    if not raw_words or not top_tokens:
        return payload
    skel_a = [_norm_word(w) for w in raw_words]
    skel_b = [_norm_word(w) for w in top_tokens]
    nt = len(top_tokens)
    mapped = list(raw_words)
    matched = 0
    i = 0
    tail_buf = ""
    for idx, s in enumerate(skel_a):
        if i >= nt:
            break
        if not s:
            continue
        j = -1
        for c in range(i, min(nt, i + _ALIGN_WINDOW)):
            if skel_b[c] == s:
                j = c
                break
        if j >= 0:
            tail = tail_buf
            buf_from_num = bool(tail_buf)
            if j > i and not buf_from_num:
                tail = _trail_punct(top_tokens[j - 1])
            if tail and idx > 0:
                mapped[idx - 1] = mapped[idx - 1] + tail
            tail_buf = ""
            mapped[idx] = top_tokens[j]
            matched += 1
            i = j + 1
        else:
            while i < nt and skel_b[i].isdigit():
                tail = _trail_punct(top_tokens[i])
                if tail:
                    tail_buf = tail
                i += 1
    if matched < max(1, int(0.3 * len(raw_words))):
        return payload
    out = dict(payload)
    new_segs: list[dict] = []
    pos = 0
    for s, n in zip(segs, counts):
        ns = dict(s)
        ns["text"] = " ".join(mapped[pos:pos + n])
        pos += n
        new_segs.append(ns)
    out["segments"] = new_segs
    return out


def _gigastt_post(base: str, headers: dict, model: str, body: bytes) -> dict:
    import httpx
    files = {"file": ("chunk.wav", io.BytesIO(body), "audio/wav")}
    data = {"model": model, "language": "auto", "response_format": "verbose_json"}
    r = httpx.post(f"{base}/v1/audio/transcriptions", headers=headers,
                   files=files, data=data, timeout=600.0)
    if r.status_code >= 400:
        raise RuntimeError(f"gigastt HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _gigastt_post_retry(base: str, headers: dict, model: str, body: bytes,
                        retries: int | None = None) -> dict:
    """POST chunka s backoff: padenie odnogo chunka ne dolzhno sbrosat' ves'
    progress — ran'she isklyucheniye na chunke 9/10 vyzyvalo polnyy
    faster-whisper-progon vsey vstrechi v tom zhe jobe."""
    n = ASR_CHUNK_RETRIES if retries is None else retries
    last: Exception | None = None
    for attempt in range(max(1, n)):
        try:
            return _gigastt_post(base, headers, model, body)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < n - 1:
                time.sleep(min(2 ** attempt * 2, 15))
    raise RuntimeError(f"gigastt chunk failed after {n} attempts: {last}")


def _local_chunk_fallback(body: bytes, offset: float, rate: int) -> list[dict] | None:
    """Rasšifrovat' odin chunk lokâl'no (faster-whisper) — segm s absolyutnym
    vremem. None esli model' niedostupna (togda podnimem isklyucheniye)."""
    raw = _whisper_raw_bytes(body)
    if raw is None:
        return None
    for s in raw:
        s["start"] = s.get("start", 0) + offset
        s["end"] = s.get("end", 0) + offset
    return raw


def _gigastt_chunks(wav: Path) -> dict:
    """Post WAV chunkami: gigastt otkluchaet bolshie multipart-tela (400).

    Chanki wyravnenny po kadram PCM; segmenty sovmeschayutsya s smeshcheniem
    po vremeni. Esli WAV ne PCM (chitaet'sya nechedrez `wave`) — odin zabros.
    """
    base = os.environ.get("ASR_BASE_URL", "").rstrip("/")
    key = os.environ.get("ASR_API_KEY", "").strip()
    model = os.environ.get("ASR_MODEL", "ml_ctc_large").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    with wave.open(str(wav), "rb") as w:
        nch, sw, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        nframes = w.getnframes()
        if nframes <= 0:
            raise RuntimeError("pustoy WAV")
        max_bytes = _max_upload_bytes()
        chunk_frames = max(1, max_bytes // (nch * sw))
        if chunk_frames >= nframes:
            w.rewind()
            return _retrofit_punctuation(
                _gigastt_post_retry(base, headers, model, _wav_bytes(w.readframes(nframes), nch, sw, rate)))

        raw: list[dict] = []
        lang = None
        step_frames = max(1, chunk_frames - rate)  # overlapping 1 s granicy
        pos = 0
        while pos < nframes:
            take = min(chunk_frames, nframes - pos)
            w.setpos(pos)
            frames = w.readframes(take)
            offset = pos / rate
            body = _wav_bytes(frames, nch, sw, rate)
            try:
                payload = _retrofit_punctuation(
                    _gigastt_post_retry(base, headers, model, body))
                for s in payload.get("segments") or []:
                    s = dict(s)
                    s["start"] = s.get("start", 0) + offset
                    s["end"] = s.get("end", 0) + offset
                    raw.append(s)
                lang = lang or payload.get("language")
            except Exception as e:  # noqa: BLE001
                # otkaz etogo chunka — transkirbuem lokâl'no, SOHRANYAYA
                # sdelannuyu rabotu ostal'nyh chunkov (ne polnyy whisper-progon)
                local = _local_chunk_fallback(body, offset, rate)
                if local is None:
                    raise
                log.warning("chunk at %.0fs: remote failed (%s); local fallback",
                            offset, e)
                raw.extend(local)
            if pos + take >= nframes:
                break
            pos = min(pos + step_frames, nframes - 1)
        return {"segments": _dedupe_segments(raw), "language": lang}


def _dedupe_segments(raw: list[dict]) -> list[dict]:
    """Ubrat dubli v zone perekrytiya chankov: pri sil'nom peresechenii
    ostaet'sya bolee dlinnyy segment."""
    out: list[dict] = []
    for s in sorted(raw, key=lambda x: (x.get("start", 0), x.get("end", 0))):
        if out:
            prev = out[-1]
            ov = min(prev.get("end", 0), s.get("end", 0)) - max(prev.get("start", 0), s.get("start", 0))
            dur_prev = max(prev.get("end", 0) - prev.get("start", 0), 1e-9)
            dur_s = max(s.get("end", 0) - s.get("start", 0), 1e-9)
            if ov > 0 and ov >= 0.85 * min(dur_prev, dur_s):
                if dur_s > dur_prev:
                    out[-1] = s
                continue
        out.append(s)
    return out


def _wav_bytes(frames: bytes, nch: int, sw: int, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def _map_verbose_to_segments(payload: dict, vad_segments: list[dict]) -> list[dict]:
    """Sootnesti ASR-segmenty VAD'yu: kazhdiy ASR-segment naznachaetsya odnomu
    VAD-segmentu (maks. peresechenie), teksty one zhe VAD skleydayutsya po
    vremeni. Ranshe bralsya tol'ko 'luchshiy' perosek — slova gubilis', a
    odin ASR-segment popadalo v neskol'ko VAD (dubli v stenogamme).

    Dlya kazhdogo VAD-ryada sohralyayetsya `parts` — vkhodyashchie ASR-segmenty
    s ih sobstvennymi timesh tampami: granicy govorivykh (diar turns) layut
    mezhdu nimi, poetomi bez nikh fraza vtorogo golosa pri kleike propadaet
    kak odna repika odnogo cheloveka."""
    raw = sorted(payload.get("segments") or [],
                 key=lambda r: (r.get("start", 0), r.get("end", 0)))
    lang = payload.get("language")
    texts: list[list[str]] = [[] for _ in vad_segments]
    parts: list[list[dict]] = [[] for _ in vad_segments]
    logprobs: list[list[float]] = [[] for _ in vad_segments]
    for r in raw:
        idx = _owner_vad(r, vad_segments)
        if idx is None:
            continue
        t = (r.get("text") or "").strip()
        if t:
            texts[idx].append(t)
            parts[idx].append({"s": round(float(r.get("start", 0)), 3),
                               "e": round(float(r.get("end", 0)), 3),
                               "t": t})
        lp = r.get("avg_logprob")
        if lp is not None:
            logprobs[idx].append(lp)
    out: list[dict] = []
    for i, seg in enumerate(vad_segments):
        lp = logprobs[i]
        conf = _conf_from_logprob(sum(lp) / len(lp)) if lp else None
        out.append({"text": " ".join(texts[i]).strip(),
                    "lang": lang, "confidence": conf,
                    "parts": parts[i] or None})
    return out


def _owner_vad(raw_seg: dict, vad_segments: list[dict]) -> int | None:
    """Index VAD-segmenta s naibol'shim peresecheniem (0 overlap -> None)."""
    rs, re = raw_seg.get("start", 0), raw_seg.get("end", 0)
    best, best_ov = None, 0.0
    for i, seg in enumerate(vad_segments):
        ov = min(re, seg["t_end"]) - max(rs, seg["t_start"])
        if ov > best_ov:
            best, best_ov = i, ov
    return best


def _conf_from_logprob(v: float) -> float:
    try:
        import math
        return max(0.0, min(1.0, math.exp(v)))
    except Exception:  # noqa: BLE001
        return None


_whisper = None


def _whisper_model():
    """Model' singleton (inache gruzim GGPT na kazhdiy padayushchiy chunk)."""
    global _whisper
    if _whisper is not None:
        return _whisper
    model_id = os.environ.get("ASR_MODEL", "large-v3")
    if model_id in ("ml_ctc_large", "ml_ctc"):
        model_id = os.environ.get("WHISPER_FALLBACK_MODEL", "large-v3")
    try:
        from faster_whisper import WhisperModel
        device = os.environ.get("ASR_DEVICE", "cpu")
        _whisper = WhisperModel(model_id, device=device,
                                compute_type=_whisper_compute_type(device))
    except Exception as e:  # noqa: BLE001
        log.error("faster-whisper unavailable (%s)", e)
        _whisper = None
    return _whisper


def _whisper_raw_bytes(wav_bytes: bytes) -> list[dict] | None:
    """Transkribirovat' WAV-byty lokâl'no v syroy format gigastt; None pri
    niedostupnosti modeli."""
    model = _whisper_model()
    if model is None:
        return None
    try:
        seg_iter, _info = model.transcribe(io.BytesIO(wav_bytes), vad_filter=False)
        return [{"start": s.start, "end": s.end, "text": s.text,
                 "avg_logprob": getattr(s, "avg_logprob", None)} for s in seg_iter]
    except Exception as e:  # noqa: BLE001
        log.error("faster-whisper chunk transcription failed: %s", e)
        return None


def _whisper_compute_type(device: str) -> str:
    """float16 na GPU, int8 na CPU; OVERRIDE: WHISPER_COMPUTE_TYPE."""
    override = os.environ.get("WHISPER_COMPUTE_TYPE", "").strip()
    if override:
        return override
    return "float16" if device.startswith("cuda") else "int8"


def _faster_whisper_transcribe(wav: Path, vad_segments: list[dict]) -> list[dict]:
    model = _whisper_model()
    if model is None:
        log.error("faster-whisper unavailable — empty transcript")
        return [{"text": "", "lang": None, "confidence": None} for _ in vad_segments]
    try:
        seg_iter, _info = model.transcribe(str(wav), vad_filter=False)
        raw = [{"start": s.start, "end": s.end, "text": s.text,
                "avg_logprob": getattr(s, "avg_logprob", None)} for s in seg_iter]
    except Exception as e:  # noqa: BLE001
        log.error("faster-whisper transcription failed: %s", e)
        raw = []
    return _map_verbose_to_segments({"segments": raw, "language": "ru"}, vad_segments)
