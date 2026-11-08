"""VAD (FT-13): Silero esli ustanovlen; fallback — RMS porog po PCM.

Vozvraschaet segmety rechi [{t_start, t_end}] s granicami po pauze;
korotkie (<0.5 s) otbrossyvayutsya; dlina cel 3-15 s (razrez po maksimumu).
"""
from __future__ import annotations

import logging
import os
import time
import wave
from pathlib import Path

log = logging.getLogger("mnema.vad")

TARGET_MAX_S = 15.0
MIN_SPEECH_S = 0.5


def _retry_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("VAD_RETRY_SEC", "600")))
    except ValueError:
        return 600.0


def segment_speech(wav: Path) -> list[dict]:
    """VAD-sogmenny; OVERRIDE: VAD=energy (tests/bez modeli), kak DIARIZER=off."""
    if os.environ.get("VAD", "").lower() in ("energy", "off", "rms"):
        return _energy_segments(wav)
    backend = _get_silero()
    if backend is not None and wav.exists():
        try:
            return _silero_segments(backend, wav)
        except Exception as e:  # noqa: BLE001 — energy-fallback, no s logom
            log.warning("silero VAD failed (%s) — energy fallback", e)
    return _energy_segments(wav)


_silero = None
_silero_failed_ts = 0.0


def _get_silero():
    """Silero-model': None yesli niedostupna; povtor popytki cherez
    VAD_RETRY_SEC (prezhniy vechnyy `= False` keshiral slabost'/diskovyy
    glitk navsegda i vse posledyushchie job tratili kachestvennyy VAD)."""
    global _silero, _silero_failed_ts
    if _silero:
        return _silero
    if _silero is False and time.monotonic() - _silero_failed_ts < _retry_sec():
        return None
    try:
        import torch  # noqa: F401
        from silero_vad import load_silero_vad
        _silero = load_silero_vad()
        return _silero
    except Exception as e:  # noqa: BLE001
        log.warning("silero VAD unavailable (%s); retry in %ds",
                    e, int(_retry_sec()))
        _silero = False
        _silero_failed_ts = time.monotonic()
        return None


def _silero_segments(model, wav: Path) -> list[dict]:
    from silero_vad import get_speech_timestamps, read_audio
    w, sr = read_audio(str(wav)), 16000
    ts = get_speech_timestamps(w, model, sampling_rate=sr)
    return _merge_and_split(
        _postprocess([{"t_start": t["start"] / sr, "t_end": t["end"] / sr}
                      for t in ts]), sr)


def _energy_segments(wav: Path) -> list[dict]:
    """Gistereziznyy RMS VAD bez dependency: 30 ms shkaly, porog ot noise-floor.

    Median*3 lagalas' na dlinnyh zapisyah s plotnoy rech'yu (median sama
    okazivalas' rechevoj). Noise floor = p10, urozen' rechi = p90; high/low
    poryogi dayut uvtoychivy start/stop na shume.
    """
    try:
        with wave.open(str(wav), "rb") as w:
            sr = w.getframerate()
            n = w.getnframes()
    except Exception:  # noqa: BLE001
        return []
    struct_mod = __import__("struct")
    frame = max(1, int(sr * 0.03))
    energies: list[float] = []
    # Po-tokennoe chtenie (ne vsey file v RAM): 3-chasovaya zapis' — ~330 MB
    # syrykh baytov; pri memory-limit konteynera eto OOM.
    step_frames = frame * 50
    with wave.open(str(wav), "rb") as w:
        for base in range(0, n, step_frames):
            chunk = w.readframes(min(step_frames, n - base))
            cnt = len(chunk) // 2
            for i in range(0, cnt, frame):
                sub = chunk[i * 2:(i + frame) * 2]
                m = len(sub) // 2
                if m == 0:
                    break
                samples = struct_mod.unpack(f"<{m}h", sub[: 2 * m])
                if not samples:
                    break
                energies.append((sum(s * s for s in samples) / m) ** 0.5)
    if not energies:
        return []
    hi, lo = energy_thresholds(energies)
    voice = _hysteresis_flags(energies, hi, lo)
    segs: list[dict] = []
    start = None
    for i, v in enumerate(voice):
        t = i * 0.03
        if v and start is None:
            start = t
        elif not v and start is not None:
            if t - start >= MIN_SPEECH_S / 2:
                segs.append({"t_start": start, "t_end": t})
            start = None
    if start is not None:
        segs.append({"t_start": start, "t_end": len(energies) * 0.03})
    return _merge_and_split(_postprocess(segs), sr)


def energy_thresholds(energies: list[float]) -> tuple[float, float]:
    """(high, low) po noise-floor/rechevomu urovnyu: p10/shum, p90/rech'.

    high otkryvaet rechevoy fragment, low zamykaet (gisterezi — bez drobezhka
    na mezhsloviesh pauzah). Vylegshiy sluchay (tishina vsyu dolginu) —
    0.5*max s podom.
    """
    import statistics
    med = statistics.median(energies) or 1
    srt = sorted(energies)
    noise = srt[max(0, int(len(srt) * 0.10))]
    loud = srt[min(len(srt) - 1, int(len(srt) * 0.90))]
    top = srt[-1]
    if loud <= noise * 2:
        high = max(top / 3, med * 2, 200)
    else:
        high = max(loud / 3, noise * 6, 300)
    lo = max(min(high / 3, med), noise * 3, 120)
    return high, lo


def _hysteresis_flags(energies: list[float], hi: float, lo: float) -> list[bool]:
    out: list[bool] = []
    on = False
    for e in energies:
        if not on and e >= hi:
            on = True
        elif on and e < lo:
            on = False
        out.append(on)
    return out


def _merge_and_split(segs: list[dict], sr: int) -> list[dict]:
    """Skleyka segmetov s malyh zazoram (<0.3 s) i razrez slishkom dlinnyh."""
    out: list[dict] = []
    for s in segs:
        if out and s["t_start"] - out[-1]["t_end"] < 0.3:
            out[-1]["t_end"] = s["t_end"]
        else:
            out.append(dict(s))
    final: list[dict] = []
    for s in out:
        cur = s["t_start"]
        while s["t_end"] - cur > TARGET_MAX_S:
            final.append({"t_start": cur, "t_end": cur + TARGET_MAX_S})
            cur += TARGET_MAX_S
        if s["t_end"] - cur >= MIN_SPEECH_S:
            final.append({"t_start": cur, "t_end": s["t_end"]})
    return final


def _postprocess(segs: list[dict]) -> list[dict]:
    return [s for s in segs if s["t_end"] - s["t_start"] >= MIN_SPEECH_S]


def _wav_slice_inproc(src: Path, t0: float, dur: float, dst: Path) -> bool:
    """Fast path dlja extract_wav_slice: PCM16 mono 16 kHz chitaetsya napryamyu
    cherez wave — bez subprotsessa ffmpeg na kazhduyu frazu. Meeting.wav vseghda
    imeno takoy (-ar 16000 -ac 1 v finalize); frazovykh emeddingerov tyisyachi
    na vstrechu, i každyy subprotsess — glavnyy proigrysh speaker-etapa."""
    try:
        with wave.open(str(src), "rb") as w:
            if (w.getnchannels() != 1 or w.getsampwidth() != 2
                    or w.getframerate() != 16000):
                return False
            i0 = max(0, min(int(t0 * 16000), w.getnframes()))
            n = max(1, min(int(dur * 16000), w.getnframes() - i0))
            if n < 1:
                return False
            w.setpos(i0)
            frames = w.readframes(n)
        with wave.open(str(dst), "wb") as o:
            o.setnchannels(1)
            o.setsampwidth(2)
            o.setframerate(16000)
            o.writeframes(frames)
        return True
    except Exception:  # noqa: BLE001 — ne waw/PCM → puskay ffmpeg
        return False


def extract_wav_slice(src: Path, t0: float, t1: float, dst: Path) -> None:
    """Vyrezaet fragment WAV (dlя emgeddingov): PCM16 mono 16k — v protsesse,
    inache cherez ffmpeg (tochnye shkaly dlya prochiekh formatach)."""
    from .audio import run
    dur = max(0.1, t1 - t0)
    if _wav_slice_inproc(src, t0, dur, dst):
        return
    r = run(["ffmpeg", "-y", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}",
             "-i", str(src), "-ar", "16000", "-ac", "1", str(dst)])
    if r.returncode != 0:
        # fallback: pryamaya nakhotka cherez wave
        with wave.open(str(src), "rb") as w:
            sr = w.getframerate()
            w.setpos(int(t0 * sr))
            frames = w.readframes(int(dur * sr))
        with wave.open(str(dst), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(frames)
