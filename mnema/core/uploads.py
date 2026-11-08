"""Zagruzki arhivnyh zapisey soveshchaniy (video/audio) na vstreche source=upload.

Vozymlayemaya zagruzka (resumable): sozdaetsya Job+Upload, zatem chunki
dokleyivayutsya v vremennyy file; finalize proveryaet audio-dorozhku i
zapuskaet shtatnyy konveyer (run_asr_pipeline).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import os
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select

from .audio import MEDIA_EXTENSIONS, probe_media
from .db import get_session
from .models import Job, SessionEvent, Upload


@contextlib.contextmanager
def _upload_lock(upload_id: int):
    """Kross-processnaya/interpotocnaya serializaciya dopisivaniya odnogo
    uploada: dva parallel'nyh POST s odnim offsetom ran'she prohodili provierku
    `eff != cur` oboe i portili fayl interleavannymi baytami. Derzhim LOCK_EX
    na sosednem lock-fayl ote chteniya bytes_received do commita."""
    lock_path = _uploads_dir() / f".up{upload_id}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def max_upload_bytes() -> int:
    return int(float(os.environ.get("UPLOAD_MAX_GB", "8") or 8) * 1024 ** 3)


def chunk_mb() -> int:
    return int(os.environ.get("UPLOAD_CHUNK_MB", "16") or 16)


def keep_source() -> bool:
    return (os.environ.get("KEEP_UPLOADED_SOURCE", "1") or "1").strip().lower() \
        not in ("0", "false", "no")


def _uploads_dir() -> Path:
    d = Path(os.environ.get("DATA_DIR", "/data")) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _free_bytes(path: Path) -> int | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


def _check_free_space(need: int | None) -> None:
    """Pri izvestnom razmere — trebovat' free >= need. Bez razmera — tol'ko chto
    disk ochiten (ne blokiruschem sozdanie vremennogo fayla zagruzki)."""
    if need is None:
        return
    free = _free_bytes(Path(os.environ.get("DATA_DIR", "/data")))
    if free is not None and free < need:
        raise ValueError("На диске мало свободного места для загрузки")


def sanitize_filename(filename: str | None) -> str:
    name = Path(filename or "upload.bin").name.strip()
    name = name.replace("/", "_").replace("\\", "_")
    if not name or name in (".", ".."):
        return "upload.bin"
    return name[:200]


def _media_ext(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    return ext if ext in MEDIA_EXTENSIONS else None


def create_upload(*, filename: str, bytes_total: int | None = None,
                  job_id: int | None = None, topic: str = "",
                  meeting_date: dt.datetime | None = None,
                  note: str | None = None, created_by: str = "web") -> tuple[Job, Upload]:
    """Sozdat' vstrechu-arhiv (ili doshat' zagruzku k suschestvuyushchey).

    Vozvraschaet (job, upload). Oshibki — ValueError (400) / OverflowError (413)."""
    if job_id is None and not (topic or "").strip():
        raise ValueError("Укажите тему встречи")
    ext = _media_ext(filename)
    if ext is None:
        raise ValueError("Файл не поддерживается; ожидается аудио/видео: "
                         + " ".join(sorted(MEDIA_EXTENSIONS)))
    if bytes_total is not None and bytes_total <= 0:
        raise ValueError("Пустой файл")
    cap = max_upload_bytes()
    if bytes_total is not None and bytes_total > cap:
        raise OverflowError(f"Файл больше лимита {cap // 1024 ** 3} ГБ (UPLOAD_MAX_GB)")
    fname = sanitize_filename(filename)
    with get_session() as s:
        if job_id is not None:
            job = s.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.source != "upload":
                raise ValueError("Загрузка допустима только для встреч типа архив")
            if job.status not in ("uploading", "failed"):
                raise ValueError(f"Загрузка недоступна при статусе {job.status}")
            active = s.scalars(select(Upload).where(
                Upload.job_id == job.id,
                Upload.status.in_(("pending", "uploading")))).first()
            if active is not None:
                raise ValueError("Для этой встречи уже идёт загрузка")
        else:
            if meeting_date is not None and meeting_date.tzinfo is None:
                from .tz import local_to_utc
                meeting_date = local_to_utc(meeting_date)
            _check_free_space(bytes_total)
            job = Job(zoom_meeting_id=None, source="upload", topic=topic.strip(),
                      start_at=(meeting_date
                                or dt.datetime.now(dt.timezone.utc)),
                      status="uploading", note=(note.strip() if note else None),
                      created_by=f"{created_by}:upload")
            s.add(job)
            s.flush()
            s.add(SessionEvent(job_id=job.id, event="created",
                               detail=f"архив-загрузка · файл: {fname}"))
        uid = uuid.uuid4().hex
        tmp_path = _uploads_dir() / f"{uid}{ext}"
        tmp_path.touch()
        up = Upload(job_id=job.id, filename=fname, path=str(tmp_path),
                    bytes_total=bytes_total, status="pending")
        s.add(up)
        s.commit()
        s.refresh(job)
        s.refresh(up)
        return job, up


def append_chunk(upload_id: int, data: bytes, offset: int | None = None) -> Upload:
    """Dopisat' chunk v pozicii offset (po umolchaniyu — tekushchiy bytes_received).

    Pri obryve client vsegda sashivaet /offset, poetomu offset vsegda dolzhen
    sovpadat' s poluchennym; lyuboe raskhozhdenie = oshibka, chtoby ne isportit'
    fayl propuskom ili dublirovaniem baytov."""
    if not data:
        raise ValueError("Пустые данные фрагмента")
    with _upload_lock(upload_id), get_session() as s:
        up = s.get(Upload, upload_id)
        if up is None:
            raise KeyError(upload_id)
        if up.status not in ("pending", "uploading"):
            raise ValueError(f"Загрузка в статусе {up.status} недопустима")
        cur = up.bytes_received
        eff = cur if offset is None else offset
        if eff != cur:
            raise ValueError(f"Несовпадение offset: запрошен {eff}, получено {cur}")
        cap = max_upload_bytes()
        new_size = cur + len(data)
        if new_size > cap:
            raise OverflowError(f"Превышен лимит {cap // 1024 ** 3} ГБ (UPLOAD_MAX_GB)")
        if up.bytes_total is not None and new_size > up.bytes_total:
            raise ValueError("Данных больше, чем размер файла")
        path = Path(up.path)
        with open(path, "r+b" if path.exists() else "wb") as f:
            f.seek(cur)
            f.write(data)
        up.bytes_received = new_size
        up.status = "uploading"
        s.commit()
        s.refresh(up)
        return up


def offset_of(upload_id: int) -> tuple[int, str]:
    with get_session() as s:
        up = s.get(Upload, upload_id)
        if up is None:
            raise KeyError(upload_id)
        # fault-tolerant: fakticheskiy razmer fila na diske
        real = Path(up.path).stat().st_size if Path(up.path).exists() else 0
        return min(up.bytes_received, real), up.status


def finalize(upload_id: int) -> Job:
    """Proverka audio-dorozhki -> perenos v jobs/<id>/raw<ext> -> processing + ASR.

    Dlitel'nyy ffprobe vypolnyaetsya VNE sessii BDD, chtoby ne blokirovat'
    SQLite (seriyaziruet pisateley) na vremya validatsii fayla v neskol'ko GB."""
    from .pipeline import transition
    with get_session() as s:
        up = s.get(Upload, upload_id)
        if up is None:
            raise KeyError(upload_id)
        job_id = up.job_id
        state = up.status
        path = Path(up.path)
        expected = up.bytes_total
    if state not in ("pending", "uploading"):
        raise ValueError(f"Загрузка в статусе {state} уже завершена")
    if not path.exists() or path.stat().st_size == 0:
        with get_session() as s:
            up = s.get(Upload, upload_id)
            up.status = "failed"
            up.error = "файл загрузки отсутствует"
            s.commit()
        raise ValueError("Файл загрузки отсутствует")
    if expected is not None and path.stat().st_size < expected:
        raise ValueError("Файл загружен не полностью")
    probe = probe_media(path)  # dolgo; bez sessii BDD
    with get_session() as s:
        up = s.get(Upload, upload_id)
        # povtornaya proverka: paralnaya otkhmena/finalizatsiya mogla izmenit' sostoyanie
        if up.status not in ("pending", "uploading"):
            raise ValueError(f"Загрузка в статусе {up.status} уже завершена")
        if not probe["ok"]:
            up.status = "failed"
            up.error = probe["error"]
            s.commit()
            raise ValueError(probe["error"] or "не удалось провести валидацию")
        duration = probe["duration_s"] or 0.0
        ext = path.suffix.lower()
        # perenos v kanonicheskiy katalog joba
        dest_dir = Path(os.environ.get("DATA_DIR", "/data")) / "jobs" / str(job_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"raw{ext}"
        if ext == ".webm":
            dest = dest_dir / "raw.webm"
        if dest.exists():
            dest.unlink()
        shutil.move(str(path), str(dest))
        up.path = str(dest)
        up.status = "complete"
        up.duration_s = duration
        up.bytes_received = dest.stat().st_size
        up.bytes_total = up.bytes_total or dest.stat().st_size
        from .models import AudioFile
        exists = s.scalars(select(AudioFile).where(
            AudioFile.job_id == job_id,
            AudioFile.kind == ext.lstrip("."))).first()
        if exists is None:
            s.add(AudioFile(job_id=job_id, kind=ext.lstrip("."), path=str(dest),
                            bytes=dest.stat().st_size, duration_s=duration))
        s.add(SessionEvent(job_id=job_id, event="upload_complete",
                           detail=f"файл: {dest.name} · {dest.stat().st_size} Б · "
                                  f"{duration:.1f} с"))
        s.commit()
    transition(job_id, "processing", event="upload_finished")
    from .queue import enqueue_job
    enqueue_job("asr", "mnema.worker.tasks.run_orchestrate", job_id)
    with get_session() as s:
        return s.get(Job, job_id)


def cancel_upload(upload_id: int) -> Job:
    """Otkhmenie aktivnoy zagruzki: udalyaet vremennyy fayl, shag transition->cancelled."""
    with get_session() as s:
        up = s.get(Upload, upload_id)
        if up is None:
            raise KeyError(upload_id)
        job_id = up.job_id
        if up.status in ("pending", "uploading"):
            up.status = "failed"
            up.error = "отменено оператором"
            Path(up.path).unlink(missing_ok=True)
            s.commit()
    from .pipeline import transition
    with get_session() as s:
        job = s.get(Job, job_id)
        cur = job.status if job else None
    if cur in ("uploading",):
        return transition(job_id, "cancelled", event="upload_cancelled")
    return job


def gc_stale_uploads(max_age_hours: int = 24) -> int:
    """UDalenie osirotevshih chasnichyh zagruzok starshe N chasov (maintenance)."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max_age_hours)
    removed = 0
    with get_session() as s:
        rows = s.scalars(select(Upload).where(
            Upload.status.in_(("pending", "uploading")))).all()
        for up in rows:
            ts = up.updated_ts or up.created_ts
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            if ts >= cutoff:
                continue
            Path(up.path).unlink(missing_ok=True)
            up.status = "failed"
            up.error = "очищено по тайм-ауту загрузки"
            s.add(SessionEvent(job_id=up.job_id, event="upload_gc",
                               detail=str(up.filename)))
            removed += 1
        s.commit()
    return removed
