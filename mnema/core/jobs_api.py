"""CRUD Job (FT-3, FT-21 biznes-logika)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import select

from .db import get_session
from .models import Job, Protocol, ReviewRequest, SessionEvent
from .secrets_store import delete_pwd, store_pwd
from .zoom_link import extract_meeting_id, normalize_join_link


def create_job(*, meeting_link: str | None = None, meeting_id: str | None = None,
               pwd: str | None = None, topic: str = "",
               start_at: dt.datetime | None = None,
               end_at: dt.datetime | None = None,
               duration_max_min: int | None = None,
               created_by: str = "api",
               ics_uid: str | None = None) -> Job:
    src = meeting_link or (meeting_id or "")
    if not src:
        raise ValueError("Нужен meeting_link или meeting_id")
    mid, extracted_pwd = extract_meeting_id(src)
    if ics_uid:
        # P3-17: povtornyy import odnogo kalendarya (odot ot /import-ics,
        # perezapusk po raspisaniyu) ne dolzhen shtat' dubli — UID sobytiya
        # unikalen v predelakh kalendarya.
        with get_session() as s:
            dup = s.scalars(select(Job).where(Job.ics_uid == ics_uid).limit(1)).first()
            if dup is not None:
                raise ValueError(f"vstrecha s UID {ics_uid} uzhe importirovana (job #{dup.id})")
    pwd = pwd or extracted_pwd
    join_link = normalize_join_link(meeting_link) if meeting_link else None
    if start_at is not None and start_at.tzinfo is None:
        from .tz import local_to_utc
        start_at = local_to_utc(start_at)  # naivnyj vzor = lokal'noe vremya operatora
    if end_at is not None and end_at.tzinfo is None:
        from .tz import local_to_utc
        end_at = local_to_utc(end_at)
    if end_at is not None and start_at is not None and end_at <= start_at:
        raise ValueError("Время окончания должно быть позже начала")
    immediate = start_at is None
    if immediate:
        start_at = dt.datetime.now(dt.timezone.utc)
    status = "scheduled"
    with get_session() as s:
        job = Job(zoom_meeting_id=mid, join_link=join_link,
                  pwd_ref=(store_pwd(mid, pwd) if pwd else None),
                  topic=topic or f"Meeting {mid}", start_at=start_at,
                  end_at=end_at,
                  duration_max_min=duration_max_min, status=status,
                  created_by=created_by, ics_uid=ics_uid)
        s.add(job)
        s.flush()
        s.add(SessionEvent(job_id=job.id, event="created",
                            detail=f"создал: {created_by} · запуск: "
                                   f"{'сразу' if immediate else 'по расписанию'}"))
        s.commit()
        s.refresh(job)
    from .scheduler import schedule_job_for
    schedule_job_for(job.id)
    return job


def cancel_job(job_id: int) -> Job | None:
    from .pipeline import transition
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return None
        cur = job.status
        if cur == "uploading":
            from .models import Upload
            for up in job.uploads:
                if up.status in ("pending", "uploading"):
                    up.status = "failed"
                    up.error = "отменено вместе со встречей"
                    Path(up.path).unlink(missing_ok=True)
            s.commit()
    if cur in ("recording", "launching"):
        from . import jobs_launch
        jobs_launch.request_stop(job_id)
    if cur in ("scheduled", "launching", "recording", "processing", "uploading"):
        return transition(job_id, "cancelled", event="cancelled")
    return job


def rename_job(job_id: int, topic: str) -> Job | None:
    """Menyaem tol'ko nazvanie (topic); imya 'Vstrecha №' ostavlyaem kak est'.

    Pustaya/otсутstvuyushchaya topic -> 422 na urovne API, zdes' vozvrat None
    esli vstrechi net.
    """
    topic = (topic or "").strip()[:512]
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return None
        old = job.topic
        job.topic = topic
        s.add(SessionEvent(job_id=job_id, event="renamed",
                           detail=f"'{old}' -> '{topic}'"))
        s.commit()
        s.refresh(job)
        return job


_KEEP = object()  # marker: pole ne prisylalos' — ne trogat' (v otlicie ot None = ochistit')


def update_job(job_id: int, *, meeting_link: str = "", pwd: str = "",
               topic: str = "", start_at: dt.datetime | None = None,
               end_at: dt.datetime | None | object = _KEEP) -> Job:
    """Parametry ESHCHYO NE NACHATOY vstrechi (status scheduled).

    KeyError esli vstrechi net; ValueError pri validacii ili popytke
    redaktirovat' zapushchennuyu vstrechu. Naivnye start_at/end_at —
    lokal'noe vremya operatora (kak v create_job). Pustaya pwd/ssylka —
    bez izmeneniya. Posle smeny vremeni planovyy zapusk perenaznachaetsya.
    """
    topic = (topic or "").strip()[:512]
    if not topic:
        raise ValueError("Название не может быть пустым")
    meeting_link = (meeting_link or "").strip()
    pwd = (pwd or "").strip()
    if start_at is not None and start_at.tzinfo is None:
        from .tz import local_to_utc
        start_at = local_to_utc(start_at)
    if end_at is not _KEEP and isinstance(end_at, dt.datetime) \
            and end_at.tzinfo is None:
        from .tz import local_to_utc
        end_at = local_to_utc(end_at)
    if isinstance(end_at, dt.datetime) and start_at is not None and end_at <= start_at:
        raise ValueError("Время окончания должно быть позже начала")
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status != "scheduled":
            raise ValueError(
                "Менять параметры можно только до начала встречи")
        job.topic = topic
        extracted_pwd = None
        if meeting_link:
            mid, extracted_pwd = extract_meeting_id(meeting_link)
            job.zoom_meeting_id = mid
            job.join_link = normalize_join_link(meeting_link)
        new_pwd = pwd or extracted_pwd
        if new_pwd:
            if job.pwd_ref:
                delete_pwd(job.pwd_ref)
            job.pwd_ref = store_pwd(job.zoom_meeting_id or str(job_id),
                                    new_pwd)
        if start_at is not None:
            job.start_at = start_at
        if end_at is not _KEEP:
            job.end_at = end_at  # None = ocharovano formy, _KEEP = ne prisylalos'
        s.add(SessionEvent(job_id=job_id, event="updated",
                           detail="параметры встречи изменены"))
        s.commit()
        s.refresh(job)
    from .scheduler import schedule_job_for
    schedule_job_for(job.id)
    return job


def delete_job(job_id: int) -> bool:
    """Polnoe udalenie vstrechi: BDD-zapisi + fayly na diske + secret.

    Vozvra True YESLI vstecha sushchestvovala i udalena, False esli net.
    Potomki razdeleniya (parent_job_id) udalyayutsya vmeste s roditelem,
    inache FK-potomki ostrayutsya i dal'neysheye udalenie nevozmozhno."""
    targets = [job_id]
    with get_session() as s:
        if s.get(Job, job_id) is None:
            return False
        kids = s.scalars(select(Job.id).where(Job.parent_job_id == job_id)).all()
    targets = list(kids) + [job_id]  # детей чистим раньше родителя (FK)
    for tid in targets:
        _purge_job(tid)
    try:
        from .scheduler import get_scheduler
        for tid in targets:
            get_scheduler().remove_job(f"launch:{tid}")
    except Exception:  # noqa: BLE001 — no zadaschika / net taska
        pass
    return True


def _purge_job(job_id: int) -> None:
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        paths: list[str] = []
        for up in job.uploads:
            if up.path:
                paths.append(up.path)
        for a in job.audio_files:
            if a.path:
                paths.append(a.path)
        proto = s.scalars(select(Protocol).where(
            Protocol.job_id == job_id)).first()
        if proto:
            for p in (proto.md_path, proto.docx_path, proto.steno_md_path):
                if p:
                    paths.append(p)
        for e in job.events:
            s.delete(e)
        for a in job.audio_files:
            s.delete(a)
        for up in job.uploads:
            s.delete(up)
        for seg in job.segments:
            s.delete(seg)
        for rr in s.scalars(select(ReviewRequest).where(
                ReviewRequest.job_id == job_id)).all():
            s.delete(rr)
        from .models import ConfirmEndRequest, PendingCommand
        for cer in s.scalars(select(ConfirmEndRequest).where(
                ConfirmEndRequest.job_id == job_id)).all():
            s.delete(cer)
        for pc in s.scalars(select(PendingCommand).where(
                PendingCommand.job_id == job_id)).all():
            s.delete(pc)
        if proto:
            s.delete(proto)
        delete_pwd(job.pwd_ref)
        s.delete(job)
        s.commit()
    for p in paths:
        Path(p).unlink(missing_ok=True)
