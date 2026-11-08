"""Выгрузка записей и протоколов в самба-зеркало (SMB/CIFS, bind-mount /export).

Самба — дополнительное зеркало: копия в /data остаётся, отказ сети не ломает
конвейер (ошибка -> SessionEvent export_error, job не падает). Транспорт —
обычная файловая запись в CIFS-маунт; SMB-библиотек нет.

Структура: <EXPORT_DIR>/дд.мм.гг/<Топик>.mp3 (+ <Топик> - протокол.md/.docx).
Выгрузка включена только если EXPORT_DIR задан и каталог доступен на запись.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
from pathlib import Path

from sqlalchemy import select

from .db import get_session
from .models import Job, Protocol, SessionEvent
from .tz import to_local

MAX_STEM_BYTES = 120          # запас до лимита 255 байт UTF-8 на имя файла
DAY_FMT = "%d.%m.%y"
EXPORT_RETRY_STATUSES = ("processing", "awaiting_llm", "awaiting_review", "done")
MAX_ATTEMPTS = 6
RETRY_BATCH = 5
_PROTOCOL_SUFFIXES = {".md": " - протокол.md", ".docx": " - протокол.docx"}


def export_dir() -> Path | None:
    raw = (os.environ.get("EXPORT_DIR") or "").strip()
    return Path(raw) if raw else None


def trim_silence_enabled() -> bool:
    """kill-switch EXPORT_TRIM_SILENCE (по умолчанию включён): обрезка ведущей
    и хвостовой тишины в копии шарами (оригиналы в /data не трогаются)."""
    return os.environ.get("EXPORT_TRIM_SILENCE", "1").strip() != "0"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def export_configured() -> bool:
    """EXPORT_DIR задан, каталог существует и доступен на запись."""
    d = export_dir()
    if d is None:
        return False
    return d.is_dir() and os.access(str(d), os.W_OK | os.X_OK)


def _sanitize_char(c: str) -> str:
    if c in '<>:"/\\|?*' or ord(c) < 32 or ord(c) == 127:
        return "-"
    return c


def sanitize_topic(topic: str | None, job_id: int) -> str:
    """Имя файла из топика: запрещённые символы -> '-', схлопнуть пробелы,
    обрезка до MAX_STEM_BYTES по БАЙТАМ UTF-8 (не символам). Пусто -> job{N}."""
    text = "".join(_sanitize_char(c) for c in (topic or ""))
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    while len(text.encode("utf-8")) > MAX_STEM_BYTES:
        text = text[:-1]
    text = text.strip().strip(".")
    if not text or set(text) <= set("- "):
        return f"job{job_id}"
    return text


def export_day_dir(dt_value: dt.datetime | None) -> Path:
    """Папка дня по локальному поясу (дд.мм.гг)."""
    d = export_dir()
    assert d is not None
    local = to_local(dt_value or dt.datetime.now(dt.timezone.utc))
    return d / local.strftime(DAY_FMT)


def _exported_paths(job: Job) -> set[str]:
    ej = job.export_json or {}
    return {str(v) for v in ej.values() if isinstance(v, str)}


def _occupied_audio_stems(day_dir: Path, own_paths: set[str]) -> set[str]:
    """Bазы имён аудиофайлов в папке дня, НЕ принадлежащие этому job'у.

    Протокольные файлы (' - протокол.*') не учитываются: они всегда идут за
    своим аудио-stem, иначе собственный протокол считался бы занявшим stem."""
    stems: set[str] = set()
    if not day_dir.is_dir():
        return stems
    for f in day_dir.iterdir():
        if not f.is_file():
            continue
        if " - протокол" in f.name or f.name.startswith("."):
            continue
        if str(f) in own_paths:
            continue
        stem = f.name
        for suf in (".mp3", ".wav"):
            if stem.endswith(suf):
                stem = stem[: -len(suf)]
                break
        m = re.search(r" \(\d+\)$", stem)
        if m:
            stem = stem[: m.start()]
        stems.add(stem)
    return stems


def unique_stem(job: Job, day_dir: Path, base_stem: str) -> str:
    """Свободная база имени: коллизия с другим job'ом -> ' (2)', ' (3)'…
    Свой job (path уже в export_json) — тот же stem (перезапись)."""
    taken = _occupied_audio_stems(day_dir, _exported_paths(job))
    if base_stem not in taken:
        return base_stem
    n = 2
    while f"{base_stem} ({n})" in taken:
        n += 1
    return f"{base_stem} ({n})"


def _fail(job_id: int, exc: Exception) -> None:
    """Единственная точка записи ошибки: событие + инкремент attempts."""
    try:
        with get_session() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            ej = dict(job.export_json or {})
            ej["attempts"] = int(ej.get("attempts") or 0) + 1
            job.export_json = ej
            s.add(SessionEvent(job_id=job_id, event="export_error",
                               detail=str(exc)[:500]))
            s.commit()
    except Exception:  # noqa: BLE001 — ошибка выгрузки не должна ломатьMaintenance
        pass


def _event(job_id: int, event: str, detail: str) -> None:
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event=event, detail=detail[:500]))
        s.commit()


def _update_export_json(job_id: int, **fields: str) -> None:
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        ej = dict(job.export_json or {})
        ej.update(fields)
        job.export_json = ej
        s.commit()


def _day_dt(job: Job) -> dt.datetime | None:
    return job.ended_ts or job.start_at


def _ensure_day_and_stem(job: Job, day_dir: Path) -> str:
    """Создать папку дня и выбрать/зафиксировать stem (первый успех — в
    export_json, повтор — тот же stem)."""
    ej = job.export_json or {}
    stem = str(ej.get("stem") or "")
    if stem and ej.get("dir") == str(day_dir):
        return stem
    day_dir.mkdir(parents=True, exist_ok=True)
    stem = unique_stem(job, day_dir, sanitize_topic(job.topic, job.id))
    _update_export_json(job.id, stem=stem, dir=str(day_dir))
    return stem


def _atomic_copy(src: Path, dst: Path, job_id: int) -> None:
    """Копия через temp + os.replace: читатели никогда не видят полфайла.
    Повтор unique_stem перед replace — mitigation редкой гонки на «(2)»."""
    tmp = dst.parent / f".{dst.name}.part-{job_id}"
    try:
        shutil.copyfile(str(src), str(tmp))
        os.replace(str(tmp), str(dst))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _find_source_audio(job_id: int) -> Path | None:
    from ..worker.tasks import _job_paths
    p = _job_paths(job_id)
    for key in ("mp3", "wav"):
        path = p[key]
        if path.exists() and path.stat().st_size > 0:
            return path
    return None


def _trimmed_payload(src: Path, job_id: int) -> Path | None:
    """Локальная (в каталоге job'а) копия src с обрезанной ведущей/хвостовой
    тишиной; None — резать нечего либо ffmpeg не смог (тогда выгружаем
    оригинал как есть). Писать ffmpeg сразу на шару нельзя: читатели увидят
    полфайла, а _atomic_copy гарантирует целый только при copy-in."""
    if not trim_silence_enabled():
        return None
    from .audio import trim_silence
    tmp = src.parent / f".trim.{job_id}{src.suffix.lower()}"
    try:
        ok = trim_silence(src, tmp,
                          pad_s=max(0.0, _env_float("EXPORT_TRIM_PAD_S", 0.5)),
                          noise_db=_env_float("EXPORT_TRIM_NOISE_DB", -30.0),
                          min_dur_s=max(0.05, _env_float("EXPORT_TRIM_MIN_S", 0.5)))
    except Exception:  # noqa: BLE001 — обрезка не должна ломать выгрузку
        ok = False
    if not ok:
        tmp.unlink(missing_ok=True)
        return None
    return tmp


def export_recording(job_id: int) -> bool:
    """Копия meeting.mp3 (fallback meeting.wav) в <шара>/дд.мм.гг/<stem>.<ext>.

    Копия — с обрезкой тишины по краям (оригинал в /data не трогаем); при
    недоступности ffmpeg/отсутствии тишины — обычный copy. Идемпотентность ПО
    РАЗМЕРУ ИСТОЧНИКА (export_json.src_size), не копии: обрезанная копия
    заведомо меньше источника, а split-перезапись усечённым меняет источник.

    Никак не бросает наружу: True/False; при ошибке — событие export_error
    и инкремент export_json.attempts."""
    try:
        if not export_configured():
            return False
        with get_session() as s:
            job = s.get(Job, job_id)
        if job is None:
            return False
        src = _find_source_audio(job_id)
        if src is None:
            return False
        ej = job.export_json or {}
        old = str(ej.get("audio") or "")
        src_size = src.stat().st_size
        try:
            done_size = int(ej.get("src_size") or 0)
        except (TypeError, ValueError):
            done_size = -1
        if old and Path(old).is_file() and done_size == src_size:
            return True  # уже выгружено, идемпотентно (источник не менялся)
        day_dir = export_day_dir(_day_dt(job))
        stem = _ensure_day_and_stem(job, day_dir)
        dst = day_dir / f"{stem}{src.suffix.lower()}"
        if dst.exists() and str(dst) not in _exported_paths(job):
            # gonka na sozdanie «(2)»: chuzhoy fayl zanyal nash stem — perevybor
            stem = unique_stem(job, day_dir, sanitize_topic(job.topic, job.id))
            _update_export_json(job_id, stem=stem)
            dst = day_dir / f"{stem}{src.suffix.lower()}"
        payload = _trimmed_payload(src, job_id)
        try:
            _atomic_copy(payload or src, dst, job_id)
        finally:
            if payload is not None:
                try:
                    payload.unlink()
                except OSError:
                    pass
        _update_export_json(job_id, audio=str(dst), src_size=str(src_size))
        _event(job_id, "audio_exported",
               f"{dst} (тишина по краям обрезана)" if payload else str(dst))
        return True
    except Exception as e:  # noqa: BLE001 — зеркало не должно ронять конвейер
        _fail(job_id, e)
        return False


def export_protocol_files(job_id: int) -> bool:
    """Копии protocol.md/docx в ту же папку как '<stem> - протокол.md/.docx'
    (stem из export_json, он же — у аудио). Повтор/refresh — перезапись того
    же пути. Никак не бросает наружу."""
    try:
        if not export_configured():
            return False
        with get_session() as s:
            job = s.get(Job, job_id)
            proto = s.scalars(select(Protocol).where(
                Protocol.job_id == job_id)).first()
        if job is None or proto is None:
            return False
        sources = {".md": proto.md_path, ".docx": proto.docx_path}
        have = {ext: p for ext, p in sources.items()
                if p and Path(p).is_file()}
        if not have:
            return False
        # протоколы всегда идут в папку дня аудио; если аудио ещё не выгружено —
        # заводим stem/dir сами по той же дате
        ej = job.export_json or {}
        audio = str(ej.get("audio") or "")
        day_dir = Path(audio).parent if audio else export_day_dir(_day_dt(job))
        stem = _ensure_day_and_stem(job, day_dir)
        exported: list[str] = []
        for ext, src_path in have.items():
            key = "protocol_md" if ext == ".md" else "protocol_docx"
            dst = day_dir / f"{stem}{_PROTOCOL_SUFFIXES[ext]}"
            if not dst.exists():
                # редкая гонка: stem могли занять позже — перепроверка
                fresh = unique_stem(_reload(job_id), day_dir, stem)
                if fresh != stem:
                    stem = fresh
                    _update_export_json(job_id, stem=stem)
                    dst = day_dir / f"{stem}{_PROTOCOL_SUFFIXES[ext]}"
            _atomic_copy(Path(src_path), dst, job_id)
            _update_export_json(job_id, **{key: str(dst)})
            exported.append(str(dst))
        if exported:
            _event(job_id, "export_ok", ", ".join(exported))
        return True
    except Exception as e:  # noqa: BLE001
        _fail(job_id, e)
        return False


def _reload(job_id: int) -> Job:
    with get_session() as s:
        return s.get(Job, job_id)


def _needs_audio(job: Job) -> bool:
    if (job.export_json or {}).get("audio"):
        return False
    return _find_source_audio(job.id) is not None


def _needs_protocols(job: Job, proto: Protocol | None) -> bool:
    if proto is None:
        return False
    ej = job.export_json or {}
    for ext, key, src in ((".md", "protocol_md", proto.md_path),
                          (".docx", "protocol_docx", proto.docx_path)):
        if src and Path(src).is_file() and not ej.get(key):
            return True
    return False


def _retry_window_cutoff() -> dt.datetime:
    """Okno retraev po created_ts: nesmyeslivat' tysyachi arhivnyh `done` job
    kazhdye 5 minut (DB-scan + CIFS stat v kazhdom `_needs_audio`). Zadaschiki
    so sboyom SMB vyshyat MAX_ATTEMPTS bystro; starshe okna schitaem
    zabvshennymi (povernut' vypyruchku mozhno ruchnym export). OVERRIDE:
    EXPORT_RETRY_DAYS (0 = bez okna)."""
    try:
        days = float(os.environ.get("EXPORT_RETRY_DAYS", "60") or 60)
    except ValueError:
        days = 60.0
    if days <= 0:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)


def retry_pending_exports(limit: int = RETRY_BATCH) -> int:
    """Maintenance-ретраи: недовыгруженные job (attempts < MAX_ATTEMPTS).

    При достижении лимита попыток — уведомление job_export_failed (один раз,
    по dedup_key). Возвращает число успешных выгрузок за проход."""
    if not export_configured():
        return 0
    cutoff = _retry_window_cutoff()
    candidates: list[tuple[int, bool, bool]] = []
    exhausted: list[Job] = []
    with get_session() as s:
        for job in s.scalars(select(Job).where(
                Job.status.in_(EXPORT_RETRY_STATUSES),
                Job.created_ts >= cutoff)).all():
            ej = job.export_json or {}
            if int(ej.get("attempts") or 0) >= MAX_ATTEMPTS:
                exhausted.append(job)
                continue
            na = _needs_audio(job)
            np_ = _needs_protocols(job, job.protocol)
            if na or np_:
                candidates.append((job.id, na, np_))
        candidates = candidates[:limit]
    for job in exhausted:
        _notify_export_failed(job)
    done = 0
    for job_id, na, np_ in candidates:
        ok = True
        if na:
            ok = export_recording(job_id) and ok
        if np_:
            ok = export_protocol_files(job_id) and ok
        if ok:
            done += 1
    return done


def _notify_export_failed(job: Job) -> None:
    dedup = f"export_failed:{job.id}"
    try:
        from .notify import NotifyService
        NotifyService().enqueue(
            kind="job_export_failed",
            dedup_key=dedup,
            subject=f"[Mnema] Не выгружено в сетевую папку: {job.topic or job.id}",
            body=(f"Встреча: {job.topic or '(без темы)'}\n"
                  f"Задача: {job.id}\n"
                  f"Попыток выгрузки: {MAX_ATTEMPTS} — дальнейшие повторы остановлены.\n"
                  f"Проверьте доступность самба-шары (/export) и параметры CIFS-маунта."),
            data={"job_id": job.id, "topic": job.topic,
                  "message": (f"Файлы задачи не выгружены в сетевую папку. "
                              f"Попыток: {MAX_ATTEMPTS} — дальнейшие повторы "
                              "остановлены. Проверьте доступность самба-шары "
                              "(/export) и параметры CIFS-маунта.")})
    except Exception:  # noqa: BLE001 — уведомление не должно ронять maintenance
        pass
