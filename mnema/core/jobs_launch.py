"""Start Job: peredacha v web-bot cherez redis, kontrol' zapuskov.

web-bot vylechivaet ochered' mnema:launch; vypolnyaet zahod i pishet statusy
obratno po REST api (POST /api/jobs/{id}/events i PATCH status — vnutrennie
endpoints).
"""
from __future__ import annotations

import datetime as dt
import json
import os

from sqlalchemy import select

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


def _push_launch(job_id: int, run_at: dt.datetime,
                 remaining: int) -> None:
    """Odna popytka push + (P1-3) retry cherez 60 s pri redis-blip. Vmesto
    byvshego `except: pass` (tihaya poterya vstrechi): poka ostalis' popytki —
    peresozdayem APScheduler-datejob; posle исcherpaniya — fallback v
    pending_commands (maintenance.drain dostavit, kogda redis ozhivet) i
    gromkiy log."""
    import logging
    log = logging.getLogger("mnema.jobs_launch")
    if fail_if_room_busy(job_id):
        return
    if _queue_push(LAUNCH_QUEUE_KEY,
                   {"job_id": job_id, "run_at": run_at.isoformat()}):
        return
    if remaining > 1:
        log.warning("launch #%s: redis nedostupen, popytka re-push cherez 60 s "
                    "(ostalos' %s)", job_id, remaining - 1)
        from .scheduler import get_scheduler
        try:
            get_scheduler().add_job(
                _push_launch, trigger="date",
                run_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60),
                id=f"launch:{job_id}:retry", replace_existing=True,
                kwargs={"job_id": job_id, "run_at": run_at,
                        "remaining": remaining - 1})
        except Exception:  # noqa: BLE001 — net shtatelera: idём v fallback
            _fallback_command("launch", job_id,
                              "push исчерпан: нет планировщика")
            log.error("launch #%s: net APScheduler — fallback v BDD", job_id)
    else:
        _fallback_command("launch", job_id,
                          f"push исчерпан ({remaining} popytok)")
        log.error("launch #%s: redis nedostupen 30 popytok — pereneseno v "
                  "pending_commands (budet dostavlen pri vosstanovlenii)",
                  job_id)


def enqueue_launch(job_id: int, run_at: dt.datetime) -> bool:
    """Start po vremeni: esli vremya nastupilo — pryamo v redis-list (s retry);
    inache — APScheduler (v api) vyzovet _push_launch v nuzhnoye vremya.

    Pered kazhdym real'nym push — gate komnaty (fail_if_room_busy): esli k
    momentu starta komnata zanyata nashey zhe idushchey zapisyu, job failitsya
    i v ochered' NE popadaet. P1-3: otkaz redis bol'she ne tishiy loss —
    _push_launch peresozdayaet sebya s retry."""
    now = dt.datetime.now(dt.timezone.utc)
    delay = (run_at - now).total_seconds()
    retries = int(os.environ.get("LAUNCH_PUSH_RETRIES", "30") or 30)
    if delay <= 0:
        if fail_if_room_busy(job_id):
            return False
        if _queue_push(LAUNCH_QUEUE_KEY,
                       {"job_id": job_id, "run_at": run_at.isoformat()}):
            return True
        # redis-blip na srazu-starte: ne teryaem, plan retry (esli APScheduler
        # est'; v worker-immediate ego net → togda True no tishye, no log v push)
        from .scheduler import get_scheduler
        try:
            get_scheduler().add_job(
                _push_launch, trigger="date",
                run_date=now + dt.timedelta(seconds=60),
                id=f"launch:{job_id}:retry", replace_existing=True,
                kwargs={"job_id": job_id, "run_at": run_at,
                        "remaining": retries - 1})
        except Exception:  # noqa: BLE001 — net shtatelera: push uzhe logovan
            pass
        return False
    from .scheduler import get_scheduler
    sch = get_scheduler()
    sch.add_job(_push_launch, trigger="date", run_date=run_at,
                id=f"launch:{job_id}", replace_existing=True,
                kwargs={"job_id": job_id, "run_at": run_at,
                        "remaining": retries})
    return True


def _queue_push(key: str, payload: dict) -> bool:
    try:
        from redis import Redis
        from .queue import redis_url
        Redis.from_url(redis_url(), socket_connect_timeout=2).lpush(
            key, json.dumps(payload))
        return True
    except Exception:
        return False


def _fallback_command(cmd: str, job_id: int, reason: str | None) -> None:
    """P3-18: redis lezhit — komanda ne teryaetsya, a zhdyot v BDD;
    drain_pending_commands() otnesiyet yeye kogda redis ozhyet."""
    import logging
    from .db import get_session
    from .models import PendingCommand
    try:
        with get_session() as s:
            dup = s.scalars(select(PendingCommand).where(
                PendingCommand.cmd == cmd,
                PendingCommand.job_id == job_id,
                PendingCommand.status == "pending")).first()
            if dup is None:
                s.add(PendingCommand(cmd=cmd, job_id=job_id, reason=reason))
                s.commit()
        logging.getLogger("mnema.jobs_launch").warning(
            "%s #%s: redis nedostupen — komanda zapisana v BDD (budet povtorena)",
            cmd, job_id)
    except Exception:  # noqa: BLE001 — net ni redis, ni BDD: chestate sebya ne lonim
        logging.getLogger("mnema.jobs_launch").exception(
            "%s #%s: ne udalos' zapisat' fallback-komandu", cmd, job_id)


def request_stop(job_id: int, reason: str | None = None) -> bool:
    payload = {"job_id": job_id}
    if reason:
        payload["reason"] = reason
    if _queue_push(STOP_QUEUE_KEY, payload):
        return True
    _fallback_command("stop", job_id, reason)
    return False


def request_session_healthcheck() -> None:
    _queue_push(HEALTHCHECK_KEY,
                {"ts": dt.datetime.now(dt.timezone.utc).isoformat()})


def request_salvage(job_id: int, reason: str | None = None) -> bool:
    """Postavit' zadachu v Salvage ochered web-bot: web-bot probeduet syryye
    segmenty v /data/jobs/<id>/, sberezhet ih v raw.webm/meeting.wav/meeting.mp3
    i soobschit finished. Ispolzuetsya watchdog-gilya (stalled recording).

    P3-18: pri nedostupnom redis komanda pisetsya v pending_commands i
    budet povtorena maintenance_job — ranshe ona tishye teryalas'."""
    payload = {"job_id": job_id}
    if reason:
        payload["reason"] = reason
    if _queue_push(SALVAGE_QUEUE_KEY, payload):
        return True
    _fallback_command("salvage", job_id, reason)
    return False


def drain_pending_commands(limit: int = 20) -> int:
    """P3-18: povtoryayet stop/salvage, nazhdavshihsya v BDD poka redis byl
    nedostupen. Vozvraschaet chislo otpravlennyh komand. Stale-komandy
    (job uzhe v terminal'nom statuse) gasyatsya bez push-a."""
    from .db import get_session
    from .models import Job, PendingCommand
    sent = 0
    with get_session() as s:
        rows = s.scalars(select(PendingCommand).where(
            PendingCommand.status == "pending").order_by(
                PendingCommand.id).limit(limit)).all()
        for pc in rows:
            job = s.get(Job, pc.job_id)
            if job is None or job.status in ("done", "cancelled", "failed"):
                pc.status = "expired"
                continue
            key = {"stop": STOP_QUEUE_KEY,
                   "salvage": SALVAGE_QUEUE_KEY,
                   "launch": LAUNCH_QUEUE_KEY}[pc.cmd]
            payload = {"job_id": pc.job_id}
            if pc.reason:
                payload["reason"] = pc.reason
            if pc.cmd == "launch":
                # run_at iz proshlogo — web-bot vse ravno zapuskayet srazu
                payload["run_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            pc.attempts += 1
            if _queue_push(key, payload):
                pc.status = "pushed"
                pc.pushed_ts = dt.datetime.now(dt.timezone.utc)
                sent += 1
        s.commit()
    return sent


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
