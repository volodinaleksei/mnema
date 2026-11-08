"""Rabota so zvukom: ffmpeg/mkvmerge (FT-6/FT-41)."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import struct
import subprocess
import wave
from pathlib import Path

FFMPEG_TIMEOUT_S = float(os.environ.get("FFMPEG_TIMEOUT_S", "7200"))


def run(cmd: list[str], timeout: float | None = None,
        **kw) -> subprocess.CompletedProcess:
    """subprocess.run s zashhitoy ot zavisaniya.

    Bez timeout zavishiy ffmpeg (pobityy konteyner, otval CIFS `/export`) derzhit
    RQ-horse do 4-ch queue-taymauta, a SIGKILL ot RQ osirablyaet process.
    start_new_session + uboystvo vershiny gropppy pri taymaute garantiuyet,
    chto potomkov ne ostayotsya. Vzmesto isklyucheniya - returncode=124,
    chtoby vse vyzyvayushchie, proveryayushchie returncode, obrabotali eto
    shtatno."""
    kw.setdefault("stdin", subprocess.DEVNULL)  # ffmpeg ne dolzhen zhdat stdin
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              start_new_session=True,
                              timeout=timeout or FFMPEG_TIMEOUT_S, **kw)
    except subprocess.TimeoutExpired as e:
        if e.stdout is not None and not isinstance(e.stdout, str):
            e.stdout = e.stdout.decode(errors="replace")
        if e.stderr is not None and not isinstance(e.stderr, str):
            e.stderr = e.stderr.decode(errors="replace")
        if e.pid:
            try:
                os.killpg(os.getpgid(e.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        return subprocess.CompletedProcess(
            cmd, 124, stdout=e.stdout or "",
            stderr=(e.stderr or "") + f"\nffmpeg run: timeout {timeout or FFMPEG_TIMEOUT_S}s")


def _kill_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def decoded_duration_s(path: Path, timeout: float = 900.0) -> float:
    """Dlitel'nost' PO POLNOMU DEKODU (ignore_err), ne po container-metadannym.

    U obryvannogo MediaRecorder-webm net cues/Duration: ffprobe daet 0, no
    ffmpeg schitaet vsey payload. 0.0 yesli fayl sovschem nie chiotsya."""
    if shutil.which("ffmpeg") is None:
        return 0.0
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-nostats", "-v", "error", "-err_detect",
             "ignore_err", "-progress", "pipe:1", "-i", str(path),
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout,
            start_new_session=True)
    except subprocess.TimeoutExpired as e:
        _kill_group(e.pid)
        return 0.0
    except FileNotFoundError:
        return 0.0
    import re
    best = 0.0
    for line in re.split(r"[\r\n]+", r.stdout or ""):
        m = re.match(r"out_time_ms=(\d+)", line.strip())
        if m:
            best = max(best, int(m.group(1)) / 1e6)
    return best


def mkvmerge_available() -> bool:
    return shutil.which("mkvmerge") is not None


def repaired_pick(cands: list[tuple], slack: float = 60.0):
    """Iz variantov (put', polnyy_dekod_s, fixed=False/True) — luchshiy. Fiksiro-
    vannyy variant poluchaet bonus slack na shkly perekodirovaniya, no proigryayet
    pri deficitu boleye slack: mkvmerge, syedshiy 97% payload (3980 -> 72 s),
    otvetdeny. decodedS<=0 ne uchityvaetsya. Vozvrat (p, decodedS) ili None."""
    living = [(p, d, f) for p, d, f in cands if d > 0]
    if not living:
        return None
    best = max(living, key=lambda c: c[1] + (slack if c[2] else 0.0))
    return best[0], best[1]


def repair_webm(webm: Path) -> bool:
    """Pochinit' povrezhdennyy pri padenii webm (FT-6).

    Rezhim mkvmerge opasen na obryvanom bez cues: on pishet «ispravlennyy»
    index, syedya payload (job 17: 34 MB -> 973 KB, 72 s iz 3980 s). Poetomu
    varianty sravnivayutsya PO POLNOMU DEKODU, i peresobiraetsya tol'ko tot,
    kotoryy chiotsya dalshe Syracuse."""
    src = webm.with_suffix(webm.suffix + ".src")
    shutil.copyfile(str(webm), str(src))
    try:
        cands: list[tuple] = [(src, decoded_duration_s(src), False)]
        tmp_ff = webm.with_suffix(".fffix.webm")
        run(["ffmpeg", "-y", "-v", "error", "-err_detect", "ignore_err",
             "-i", str(src), "-map", "0:a:0", "-c:a", "libopus", "-b:a",
             "64k", "-ar", "48000", "-ac", "1", str(tmp_ff)])
        if tmp_ff.exists() and tmp_ff.stat().st_size > 0:
            cands.append((tmp_ff, decoded_duration_s(tmp_ff), True))
        else:
            tmp_ff.unlink(missing_ok=True)
        tmp_mkv = webm.with_suffix(".fixed.webm")
        if mkvmerge_available():
            run(["mkvmerge", "-o", str(tmp_mkv), str(src)])
            if tmp_mkv.exists() and tmp_mkv.stat().st_size > 0:
                cands.append((tmp_mkv, decoded_duration_s(tmp_mkv), True))
            else:
                tmp_mkv.unlink(missing_ok=True)
        winner = repaired_pick(cands)
        for p, _, _ in cands:
            if winner is None or p != winner[0]:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        if winner is None:
            return False
        shutil.move(str(winner[0]), str(webm))
        return True
    finally:
        src.unlink(missing_ok=True)


def webm_to_wav16k(webm: Path, wav: Path) -> None:
    """webm (opus 48k) -> PCM WAV 16 kHz 16-bit mono (FT-6).

    `-f wav` yavno: vyzvayushchiye pishut v `.part` dlya atomarnoy zamenny,
    a ffmpeg vyvodit muxer po rasshireniyu i na `meeting.wav.part` padal by
    s 'Unable to choose an output format'."""
    r = run(["ffmpeg", "-y", "-err_detect", "ignore_err", "-i", str(webm),
             "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", "-f", "wav",
             str(wav)])
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg webm->wav: {r.stderr[-500:]}")


def wav16k_to_mp3(wav: Path, mp3: Path, bitrate: str = "64k") -> bool:
    """Rasprostorny mp3 dlyavygruzki (FT-41a): 44.1 kHz mono ~64 kbit/s.

    Vozvraschaet False esli mp3-encoder niedostupen (togda vydayetsya webm).
    """
    r = run(["ffmpeg", "-y", "-i", str(wav), "-ar", "44100", "-ac", "1",
             "-b:a", bitrate, str(mp3)])
    if r.returncode != 0 or not mp3.exists() or mp3.stat().st_size == 0:
        mp3.unlink(missing_ok=True)
        return False
    return True


def webm_to_mp3(webm: Path, mp3: Path, bitrate: str = "64k") -> bool:
    """Iz syrovogo webm srazu mp3 (ne zhdya ASR — FT-41a: mp3 poyavlyaetsya srazu)."""
    r = run(["ffmpeg", "-y", "-err_detect", "ignore_err", "-i", str(webm),
             "-ar", "44100", "-ac", "1", "-b:a", bitrate, str(mp3)])
    ok = r.returncode == 0 and mp3.exists() and mp3.stat().st_size > 0
    if not ok:
        mp3.unlink(missing_ok=True)
    return ok


AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus", ".webm", ".amr"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".flv", ".ts", ".mpg", ".mpeg", ".3gp"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


def media_ext_ok(filename: str) -> bool:
    return Path(filename).suffix.lower() in MEDIA_EXTENSIONS


def probe_media(path: Path) -> dict:
    """ffprobe: dlitelnost' i nalichie audio-dorozhki v lyubom konteyenerye.

    Vozvraschaet {"ok": bool, "duration_s": float, "error": str|None};
    ok=False esli fayl neprochitaem ili audio-stream otsutstvuet."""
    if shutil.which("ffprobe") is None:
        return {"ok": False, "duration_s": 0.0, "error": "ffprobe отсутствует в системе"}
    try:
        r = run(["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)])
    except FileNotFoundError:
        return {"ok": False, "duration_s": 0.0, "error": "ffprobe не запустился"}
    if r.returncode != 0:
        return {"ok": False, "duration_s": 0.0,
                "error": "файл повреждён или формат не поддерживается"}
    try:
        data = json.loads(r.stdout)
    except (ValueError, TypeError):
        return {"ok": False, "duration_s": 0.0, "error": "ffprobe вернул некорректный вывод"}
    streams = data.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        return {"ok": False, "duration_s": 0.0, "error": "в файле нет аудиодорожки"}
    dur = 0.0
    try:
        dur = float(data.get("format", {}).get("duration", 0) or 0)
    except (ValueError, TypeError):
        dur = 0.0
    if dur <= 0:
        for a in audio:
            try:
                dur = float(a.get("duration", 0) or 0)
                if dur > 0:
                    break
            except (ValueError, TypeError):
                continue
    if dur <= 0:
        return {"ok": False, "duration_s": 0.0, "error": "не удалось определить длительность"}
    return {"ok": True, "duration_s": dur, "error": None}


def media_to_wav16k(src: Path, wav: Path) -> None:
    """Pervaya audio-dorozhka iz video/audio -> PCM WAV 16 kHz 16-bit mono.

    `-f wav` yavno (sm. webm_to_wav16k): zapis' idyot v `.part`, rasshireniye
    kotorogo ffmpeg ne rasspoznaet kak WAV."""
    r = run(["ffmpeg", "-y", "-i", str(src), "-vn", "-map", "0:a:0",
             "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", "-f", "wav",
             str(wav)])
    if r.returncode != 0 or not wav.exists() or wav.stat().st_size <= 44:
        wav.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg media->wav: {r.stderr[-500:]}")


def media_to_mp3(src: Path, mp3: Path, bitrate: str = "64k") -> bool:
    """Pervaya audio-dorozhka -> mp3 44.1 kHz mono (vygruzka/pleer, kak u zapisey)."""
    r = run(["ffmpeg", "-y", "-i", str(src), "-vn", "-map", "0:a:0",
             "-ar", "44100", "-ac", "1", "-b:a", bitrate, str(mp3)])
    ok = r.returncode == 0 and mp3.exists() and mp3.stat().st_size > 0
    if not ok:
        mp3.unlink(missing_ok=True)
    return ok


_SILENCE_RE = re.compile(r"silence_(start|end):\s*([0-9]+\.?[0-9]*)")


def speech_bounds_silencedetect(path: Path, noise_db: float = -30.0,
                                min_dur_s: float = 0.5) -> tuple[float, float, float] | None:
    """Границы речи (start, end, dur) по ffmpeg silencedetect, None если не читается.

    start — где заканчивается тишина, начавшаяся с 0 (только лидирующая);
    end — где начинается хвостовая тишина, дотянувшая до конца. Файл из одной
    тишины даёт start >= end. min_dur_s отсекает паузы короче порога, чтобы
    не откусить вдох между словами. Краткие паузы внутри ffmpeg не помечает —
    внутренние они и есть.
    """
    if shutil.which("ffmpeg") is None:
        return None
    r = run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(path),
             "-af", f"silencedetect=noise={noise_db}dB:d={min_dur_s}",
             "-f", "null", "-"])
    dur = ffprobe_duration(path)
    if dur <= 0:
        dur = decoded_duration_s(path)
    if dur <= 0:
        return None
    events: list[tuple[str, float]] = [
        (kind, float(val)) for kind, val in _SILENCE_RE.findall(r.stderr or "")]
    tol = 0.25   # допуск «у самого края»: квантование silencedetect и шкала
                 # ffprobe расходятся на единицы миллисекунд
    start = 0.0
    for kind, val in events:
        if kind == "start" and val > tol:
            break                      # первая пауза не с нуля — речи до неё нет
        start = val if kind == "end" else 0.0
    end = dur
    for kind, val in reversed(events):
        if kind == "end" and val < dur - tol:
            break                      # последняя пауза не до конца — хвоста нет
        end = val if kind == "start" else dur
    return start, end, dur


def trim_silence(src: Path, dst: Path, pad_s: float = 0.5,
                 noise_db: float = -30.0, min_dur_s: float = 0.5) -> bool:
    """Обрезка ведущей и хвостовой тишины с перекодированием в формат dst.

    Внутренние паузы не трогаются. pad_s — буфер тишины, оставляемый у речи
    с каждого края (речь не «прилипает» к началу файла). False — резать
    нечего (тишины на краях нет, весь файл тишина, не читается) или ffmpeg
    ошибся; dst при этом не оставляем — вызывающий копирует оригинал как есть.
    """
    bounds = speech_bounds_silencedetect(src, noise_db=noise_db, min_dur_s=min_dur_s)
    if bounds is None:
        dst.unlink(missing_ok=True)
        return False
    start, end, dur = bounds
    t0 = max(0.0, start - pad_s)
    t1 = min(dur, end + pad_s)
    if not (t0 > 0.0 or t1 < dur) or t1 - t0 < 1.0:
        dst.unlink(missing_ok=True)
        return False
    if dst.suffix.lower() == ".wav":
        out_args = ["-ar", "16000", "-ac", "1", "-sample_fmt", "s16"]
    else:
        out_args = ["-ar", "44100", "-ac", "1", "-b:a", "64k"]
    r = run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src),
             "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}",
             *out_args, str(dst)])
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        return False
    return True


def media_slice(src: Path, t0: float, t1: float, dst: Path) -> None:
    """Narezat' fragment lyubogo audio (wav/mp3) s perecodirovaniem v format dst.

    v ot lichie ot extract_wav_slice: hodit i dlya mp3 (tochnye shkaly, -ss
    pered -i dlya skorosti, perekodirovanie garantiryuet chisty start)."""
    dur = max(0.1, t1 - t0)
    out_args: list[str] = []
    if dst.suffix.lower() == ".wav":
        out_args = ["-ar", "16000", "-ac", "1", "-sample_fmt", "s16"]
    r = run(["ffmpeg", "-y", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}",
             "-i", str(src), *out_args, str(dst)])
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg media_slice: {r.stderr[-500:]}")


def wav_duration_s(wav: Path) -> float:
    try:
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:  # noqa: BLE001
        return ffprobe_duration(wav)


def ffprobe_duration(path: Path) -> float:
    if shutil.which("ffprobe") is None:
        return 0.0
    try:
        r = run(["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", str(path)])
    except FileNotFoundError:
        return 0.0
    if r.returncode != 0:
        return 0.0
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def rms_at(wav: Path, t: float, dur: float = 1.0) -> float:
    """RMS signaliza v okne dlya provery tihi (FT-4)."""
    try:
        with wave.open(str(wav), "rb") as w:
            fr = w.getframerate()
            w.setpos(int(max(0, t) * fr))
            frames = w.readframes(int(dur * fr))
    except Exception:  # noqa: BLE001
        return 0.0
    if not frames:
        return 0.0
    n = len(frames) // 2
    samples = struct.unpack(f"<{n}h", frames[: 2 * n])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / n) ** 0.5


DECODE_GUARD_S = float(os.environ.get("DECODE_GUARD_S", "3600"))


def decode_pcm16_16k(path: Path, max_s: float | None = None) -> bytes | None:
    """Lyuboy audioformat -> PCM s16le mono 16 kГц baytami (None esli ne chinaetsya).

    max_s — zashhita ot RAM-vzryva: dlinnye faily nesmyeslivayutsya tselikom
    (po defaultu DECODE_GUARD_S). Vozvraschaetsya tol'ko pervye max_s."""
    if shutil.which("ffmpeg") is None:
        return None
    limit = DECODE_GUARD_S if max_s is None else max_s
    args = ["ffmpeg", "-nostdin", "-v", "quiet", "-i", str(path)]
    if limit:
        args += ["-t", f"{limit:.3f}"]
    args += ["-ar", "16000", "-ac", "1", "-f", "s16le", "-"]
    try:
        r = subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL,
                           start_new_session=True, timeout=FFMPEG_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        _kill_group(e.pid)
        return None
    except FileNotFoundError:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return r.stdout


def snr_db(path: Path) -> float:
    """Ocenka SNR v dB: p90 (rotevoy uroven') / p10 (noys-floor) po 30-msok kadram.

    -1.0 esli fayl ne chinaetsya. Shum-ot-noisy zapis' daet ~0-2 dB;
    chistaya rech' — 10+ dB."""
    data = decode_pcm16_16k(path)
    if not data or len(data) < 4:
        return -1.0
    n = len(data) // 2
    samples = struct.unpack(f"<{n}h", data[: 2 * n])
    sr = 16000
    frame = int(sr * 0.03)
    energies = []
    for i in range(0, len(samples) - frame + 1, frame):
        chunk = samples[i:i + frame]
        energies.append((sum(s * s for s in chunk) / frame) ** 0.5)
    if not energies:
        return -1.0
    srt = sorted(energies)
    noise = srt[max(0, int(len(srt) * 0.10))]
    loud = srt[min(len(srt) - 1, int(len(srt) * 0.90))]
    if noise <= 0:
        return 60.0 if loud > 0 else -1.0
    import math
    return 20.0 * math.log10(max(loud, 1e-6) / noise)


def speech_level(path: Path) -> float:
    """RMS vseh 'gromkih' kadrov (p90): mrtvy mikrofon / tishina — pochti 0."""
    data = decode_pcm16_16k(path)
    if not data or len(data) < 4:
        return 0.0
    n = len(data) // 2
    samples = struct.unpack(f"<{n}h", data[: 2 * n])
    sr = 16000
    frame = int(sr * 0.03)
    energies = []
    for i in range(0, len(samples) - frame + 1, frame):
        chunk = samples[i:i + frame]
        energies.append((sum(s * s for s in chunk) / frame) ** 0.5)
    if not energies:
        return 0.0
    srt = sorted(energies)
    return srt[min(len(srt) - 1, int(len(srt) * 0.90))]
