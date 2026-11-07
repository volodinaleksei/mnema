"""Start Job: peredacha v web-bot cherez redis, kontrol' zapuskov.

web-bot vylechivaet ochered' mnema:launch; vypolnyaet zahod i pishet statusy
obratno po REST api (POST /api/jobs/{id}/events i PATCH status — vnutrennie
endpoints).
"""
from __future__ import annotations

import datetime as dt
import json
import os

from .db import get_session
from .models import Job
from .queue import get_queue

LAUNCH_QUEUE_KEY = "mnema:launch"
STOP_QUEUE_KEY = "mnema:stop"
HEALTHCHECK_KEY = "mnema:session_healthcheck"
SALVAGE_QUEUE_KEY = "mnema:salvage"


def room_busy_gate_enabled() -> bool:
    """kill-switch ROOM_BUSY_GATE (po_umolchaniju vklyuchen)."""
    return os.environ.get("ROOM_BUSY_GATE", "1").strip() != "0"


def find_room_busy(job_id: int) -> tuple[int, str] | None:
    """Drugoy aktivnyy job (launching/recording) v TOY ZHE komnate (tot zhe
    zoom_meeting_id) — nositel zanyatosti. Vozvrat (busy_job_id, status) ili
    None. Sboy BDD schitaetsya «komnata svoboda» (fail-open): ne dolzhen
    blokirovat' zapusk."""
    from sqlalchemy import select
    try:
        with get_session() as s:
            job = s.get(Job, job_id)
            if job is None or not job.zoom_meeting_id:
                return None
            busy = s.scalars(
                select(Job)
                .where(Job.zoom_meeting_id == job.zoom_meeting_id,
                       Job.id != job.id,
                       Job.status.in_(("launching", "recording")))
                .order_by(Job.id).limit(1)
            ).first()
            return (busy.id, busy.status) if busy is not None else None
    except Exception:  # noqa: BLE001 — net BDD/ne popali v zapros: ne blokiruem
        return None


def fail_if_room_busy(job_id: int) -> bool:
    """Gate zapuska: v komnate uzhe pishet drugaya vstrecha — vtornoy agent
    v neye ne zakhodit. Novaya job failitsya s fail_reason=room_busy (pishem
    operatoru); True — zapusk gasit', push delayat' nelzya.

    Typichniy sluchay: predydushchiy sozvon zatyanut i nash zhe bot sidit v
    nem do tishiny (silenceEndTimeoutMin) — vtoroy zahod v tuzhe komnatu lish
    isportil by obe zapisi (dva mikrofona, odin zvuk, diarizaciya/split vsyo
    pereputaet). Skhozhie vstrechi podryad v odnoy komnate pozde razberezaet
    split.py po graniatsam, esli eto odna nepreeryvnaya zapis'."""
    if not room_busy_gate_enabled():
        return False
    busy = find_room_busy(job_id)
    if busy is None:
        return False
    busy_id, busy_status = busy
    from .pipeline import transition
    try:
        transition(job_id, "failed", fail_reason="room_busy", event="room_busy",
                   error=f"комната занята: встреча #{busy_id} уже идёт "
                         f"({busy_status}) — второй агент не заходит")
    except ValueError:
        pass  # status uzhe smenilsya (napimer cancelled) — push vs ravno ne nuzhen
    except Exception:  # noqa: BLE001 — sboy BDD pri perehode: ne lonim scheduler,
        import logging    # no i ne pishem v ochered' (agent vsyo ravno ne nuzhen)
        logging.getLogger("mnema.jobs_launch").exception(
            "room_busy: ne udalos' pometit' job %s failed (zanyat vstrechey %s)",
            job_id, busy_id)
    return True


def enqueue_launch(job_id: int, run_at: dt.datetime) -> bool:
    """Otlozhennyy start: esli vremya nastupilo — pryamo v redis-list;
    inache — APScheduler (v api) postavit' khoroshiy v remeni.

    Pered kazhdym real'nym push — gate komnaty (fail_if_room_busy): esli k
    momentu starta komnata zanyata nashey zhe idushchey zapisyu, job failitsya
    i v ochered' NE popadaet."""
    from redis import Redis
    from .queue import redis_url
    now = dt.datetime.now(dt.timezone.utc)
    delay = (run_at - now).total_seconds()
    if delay <= 0:
        if fail_if_room_busy(job_id):
            return False
        try:
            Redis.from_url(redis_url()).lpush(
                LAUNCH_QUEUE_KEY, json.dumps({"job_id": job_id, "run_at": run_at.isoformat()}))
            return True
        except Exception:
            return False
    # otdozhennyy start: postavit' v APScheduler, on vyzvaniet' etu zhe funktsiyu v nuzhnoye vremya
    def _push() -> None:
        if fail_if_room_busy(job_id):
            return
        try:
            Redis.from_url(redis_url()).lpush(
                LAUNCH_QUEUE_KEY, json.dumps({"job_id": job_id, "run_at": run_at.isoformat()}))
        except Exception:
            pass
    from .scheduler import get_scheduler
    sch = get_scheduler()
    sch.add_job(_push, trigger="date", run_date=run_at, id=f"launch:{job_id}",
                replace_existing=True)
    return True


def request_stop(job_id: int, reason: str | None = None) -> bool:
    try:
        from redis import Redis
        from .queue import redis_url
        payload = {"job_id": job_id}
        if reason:
            payload["reason"] = reason
        Redis.from_url(redis_url()).lpush(STOP_QUEUE_KEY, json.dumps(payload))
        return True
    except Exception:
        return False


def request_session_healthcheck() -> None:
    try:
        from redis import Redis
        from .queue import redis_url
        Redis.from_url(redis_url()).lpush(HEALTHCHECK_KEY,
                                          json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat()}))
    except Exception:
        pass


def request_salvage(job_id: int, reason: str | None = None) -> bool:
    """Postavit' zadachu v Salvage ochered web-bot: web-bot probeduet syryye
    segmenty v /data/jobs/<id>/, sberezhet ih v raw.webm/meeting.wav/meeting.mp3
    i soobschit finished. Ispolzuetsya watchdog-gilya (stalled recording)."""
    try:
        from redis import Redis
        from .queue import redis_url
        payload = {"job_id": job_id}
        if reason:
            payload["reason"] = reason
        Redis.from_url(redis_url()).lpush(SALVAGE_QUEUE_KEY, json.dumps(payload))
        return True
    except Exception:
        return False


def mark_launching(job_id: int) -> None:
    from .pipeline import transition
    transition(job_id, "launching", event="launch_enqueued")


def get_launch_payloads(limit: int = 10, pop: bool = True) -> list[dict]:
    """Dlya testov i dev: pyat' iz ocheredi pryamo."""
    try:
        from redis import Redis
        from .queue import redis_url
        r = Redis.from_url(redis_url())
        out = []
        for _ in range(limit):
            item = r.rpop(LAUNCH_QUEUE_KEY) if pop else None
            if item is None:
                break
            out.append(json.loads(item))
        return out
    except Exception:
        return []
