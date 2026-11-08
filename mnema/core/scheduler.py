"""Local'noye raskhanie vstrech (FT-3a) na APScheduler + import .ics.

Planirovshchik — tread' vnutri protsessa api (6.4). Postanovka Job:
za BOT_PREJOIN_SECONDS do start_at. Bez Zoom API.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .db import get_session
from .models import Job, SessionEvent
from .notify import NotifyService
from .zoom_link import extract_meeting_id

log = logging.getLogger("mnema.scheduler")


def parse_ics(text: str) -> list[dict]:
    """Minimal'nyy parser VEVENT (DTSTART/DTEND/SUMMARY/DESCRIPTION/LOCATION/UID).

    Vozvrachaet [{meeting_id, pwd, topic, start_at, duration_max_min, uid}].
    """
    from icalendar import Calendar
    events = []
    cal = Calendar.from_ical(text)
    for comp in cal.walk("VEVENT"):
        summary = str(comp.get("SUMMARY", "") or "")
        desc = str(comp.get("DESCRIPTION", "") or "")
        loc = str(comp.get("LOCATION", "") or "")
        uid = str(comp.get("UID", "") or "")
        start = comp.get("DTSTART")
        if start is None:
            continue
        start_dt = start.dt
        # all-day VEVENT: DTSTART/DTEND — datetime.date (ne datetime), net
        # .tzinfo → AttributeError, ktoryy gulsya vysshe i sobytie tishye
        # propuskalos'. Traktuem kak polunoch' lokal'nogo vremeni.
        if not isinstance(start_dt, dt.datetime):
            start_dt = dt.datetime(start_dt.year, start_dt.month, start_dt.day)
        if start_dt.tzinfo is None:
            from .tz import local_to_utc
            start_dt = local_to_utc(start_dt)
        end = comp.get("DTEND")
        dur_min = None
        if end is not None:
            end_dt = end.dt
            if not isinstance(end_dt, dt.datetime):
                end_dt = dt.datetime(end_dt.year, end_dt.month, end_dt.day)
            if end_dt.tzinfo is None:
                # floating time (RFC 5545): naivnye DTSTART/DTEND — oboe
                # lokal'noe vremya; traktovat DTEND kak UTC sdvigalo
                # dlitel'nost' na +3ch (14:00-15:00 -> 4 chasa)
                from .tz import local_to_utc
                end_dt = local_to_utc(end_dt)
            dur_min = int((end_dt - start_dt).total_seconds() // 60)
        link_src = f"{desc} {loc} {summary}"
        mid = pwd = None
        m = re.search(r"zoom\.us/[jp]/(\d+)", link_src)
        if m:
            mid = m.group(1)
        else:
            m = re.search(r"/wc/(\d+)", link_src)
            if m:
                mid = m.group(1)
        p = re.search(r"[?&](?:pwd|passcode)=([\w-]+)", link_src)
        if p:
            pwd = p.group(1)
        events.append({"meeting_id": mid, "pwd": pwd, "topic": summary,
                       "start_at": start_dt, "duration_max_min": dur_min,
                       "uid": uid})
    return [e for e in events if e["meeting_id"]]


def schedule_job_for(job_id: int) -> None:
    """Postavit' v ochered' zapuska: za BOT_PREJOIN do start_at (ili srazu)."""
    from . import jobs_launch  # lok. import dlya uniklanija cikla
    with get_session() as s:
        job = s.get(Job, job_id)
        start_at = job.start_at
    prejoin = int(os.environ.get("BOT_PREJOIN_SECONDS", "120") or 120)
    nowts = dt.datetime.now(dt.timezone.utc)
    if start_at is None:
        run_at = nowts
    else:
        if start_at.tzinfo is None:
            # SQLite вернёт start_at наивным: записан он всегда aware UTC
            # (web/API переводят локальный ввод в UTC до записи), значит
            # наивное значение из БД — это UTC, а НЕ локальное время.
            # Повторный local_to_utc сдвигал запуск на -3ч (vstrecha 56/57).
            start_at = start_at.replace(tzinfo=dt.timezone.utc)
        run_at = max(start_at - dt.timedelta(seconds=prejoin), nowts)
    jobs_launch.enqueue_launch(job_id, run_at)


def reschedule_missed_jobs() -> int:
    """Vosstanovlenie posle restarta api: in-memory APScheduler teryaet
    odozhennye zapiski launch:*, rposeskhaem ih zanovo dlya vseh scheduled job.

    Prossrochennye start_at gaydut srazu (schedule_job_for ogranichivaet
    run_at = max(start_at - prejoin, now)): bot zaidyot v idushchuyu vstrechu.
    """
    with get_session() as s:
        ids = [j.id for j in s.execute(
            select(Job).where(Job.status == "scheduled")).scalars()]
    for jid in ids:
        try:
            schedule_job_for(jid)
        except Exception:  # noqa: BLE001 — odna upavshaya job ne lonit rest
            continue
    return len(ids)


_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        from .tz import app_tz
        # cron (digest 09:00, health 09:00) — po lokal'nomu poyasu, ne po UTC
        _scheduler = BackgroundScheduler(timezone=app_tz())
    return _scheduler


def start_default_jobs() -> None:
    sch = get_scheduler()
    health = os.environ.get("SESSION_HEALTHCRON", "0 9 * * *").strip()
    if health:
        sch.add_job(session_health_job, CronTrigger.from_crontab(health),
                    id="session_health", replace_existing=True)
    digest = os.environ.get("ALERT_DIGEST_CRON", "0 9 * * *").strip()
    if digest:
        sch.add_job(digest_job, CronTrigger.from_crontab(digest),
                    id="alert_digest", replace_existing=True)
    # ezhechasy: review timeouts + dispatch uedomebleniy
    sch.add_job(maintenance_job, "interval", minutes=5, id="maintenance",
                replace_existing=True)
    if not sch.running:
        sch.start()


def session_health_job() -> None:
    """Health-check Zoom-sessii (FT-24): proverka cherez web-bot queue."""
    from . import jobs_launch
    jobs_launch.request_session_healthcheck()


def digest_job() -> None:
    NotifyService().build_digest()


def maintenance_job() -> None:
    from .pipeline import review_timeout_check, stalled_recording_watch
    from .uploads import gc_stale_uploads
    review_timeout_check()
    try:
        stalled_recording_watch()
    except Exception:  # noqa: BLE001 — watchdog ne dolzhen lonit' maintenance
        pass
    NotifyService().dispatch_pending()
    try:
        gc_stale_uploads()
    except Exception:  # noqa: BLE001 — GC ne dolzhen lonit' maintenance
        pass
    try:
        from .export import retry_pending_exports
        retry_pending_exports()
    except Exception:  # noqa: BLE001 — export ne dolzhen lonit' maintenance
        pass


def import_ics(text: str) -> int:
    from .jobs_api import create_job
    count = 0
    for ev in parse_ics(text):
        try:
            create_job(meeting_id=ev["meeting_id"], pwd=ev["pwd"],
                       topic=ev["topic"], start_at=ev["start_at"],
                       duration_max_min=ev["duration_max_min"],
                       created_by="ics")
            count += 1
        except Exception as e:  # noqa: BLE001 — propuskaem nekorrektnye sobytiya
            log.warning("ics: propushcheno sobytie %r (%s): %s",
                        ev.get("topic"), ev.get("uid"), e)
            continue
    return count
