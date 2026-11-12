"""Orkestraciya konveyera (FT-34) i validnye perekhody statusov (HT-2).

scheduled -> launching -> recording -> processing -> awaiting_llm? ->
awaiting_review? -> done | failed. Retry shagov s backoff; povtor processing
idempotenten (HT-2): segm/protokol peresozydayutsya s nulya.
"""
from __future__ import annotations

import datetime as dt
import logging
import os

from sqlalchemy import select

from .db import get_session
from .models import AudioFile, Job, Notification, Protocol, ReviewRequest, SessionEvent
from .notify import NotifyService

log = logging.getLogger("mnema.pipeline")

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "scheduled": {"launching", "cancelled", "failed", "uploading"},
    "launching": {"recording", "failed", "scheduled", "cancelled"},
    "recording": {"processing", "failed", "cancelled"},
    "uploading": {"processing", "failed", "cancelled"},
    "processing": {"awaiting_llm", "awaiting_review", "done", "failed"},
    "awaiting_llm": {"processing", "done", "failed"},
    "awaiting_review": {"done", "processing", "failed"},
    "done": {"processing"},
    "failed": {"processing", "uploading"},
    "cancelled": set(),
}


class TransitionRace(Exception):
    """Status izmenilsya paralel'nym pisatelem mezhdu proveryushchim chteniem i
    kazhom (uslovnyj UPDATE ne zatronul ni odnoy stroke)."""


def _check_transition(cur: str, new_status: str, attempt: int) -> None:
    """Validatsiya perekhoda s uchetom gonki (P2-8). Perekhod v tot zhe status
    (dva odnovremennyh /finished: odin vyigral mezhdu nashim chteniem i UPDATE,
    ili my vidim ego rezultat pri pervom zhe perechtenii) — eto ne oshibka
    logiki, a shtatnoe "uzhe tam": invarianten TransitionRace -> v api otvet
    'already', a ne ValueError/500. Inye nedopustimye perekhody — ValueError."""
    if cur == new_status:
        raise TransitionRace(f"uzhe v {new_status}")
    if new_status not in ALLOWED_TRANSITIONS.get(cur, set()):
        raise ValueError(f"Недопустимый переход статуса: {cur} -> {new_status}")


def transition(job_id: int, new_status: str, *, error: str | None = None,
               fail_reason: str | None = None, event: str | None = None,
               notify: bool = True,
               screenshot_path: str | None = None) -> Job:
    from sqlalchemy import update
    # TOCTOU-gonku (check-then-act v odnoy transaktcii viden snapshot, no
    # vtoroy kommit pereterayet pervy) zakryvaem uslovnym UPDATE ... WHERE
    # status = :cur: esli paralel'nyy writer smenil status ranshe nas, rowcount
    # = 0 i my podnimaem TransitionRace vmesto tishogo perepryedeleniya.
    for attempt in range(3):
        with get_session() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            cur = job.status
            _check_transition(cur, new_status, attempt)
            sets: dict = {"status": new_status}
            if error:
                sets["error"] = error
            if fail_reason:
                sets["fail_reason"] = fail_reason
            now = dt.datetime.now(dt.timezone.utc)
            if new_status == "recording" and job.started_ts is None:
                sets["started_ts"] = now
            if new_status in ("done", "failed"):
                sets["ended_ts"] = now
            res = s.execute(
                update(Job).where(Job.id == job_id, Job.status == cur)
                .values(**sets))
            if res.rowcount == 0:
                s.rollback()
                continue  # paralel'nyy writer vyigral — perychitat' i provit' snova
            if event:
                s.add(SessionEvent(job_id=job_id, event=event,
                                   detail=f"статус: {new_status}"))
            s.commit()
            s.refresh(job)
            snap = dict(id=job.id, topic=job.topic, mid=job.zoom_meeting_id,
                        attempts=job.join_attempts)
            break
    else:
        raise TransitionRace(f"job {job_id}: status ne dal pereiti v {new_status}")
    if new_status == "failed" and fail_reason and notify:
        NotifyService().enqueue_job_failed(
            job_id=snap["id"], topic=snap["topic"], meeting_id=snap["mid"],
            fail_reason=fail_reason, attempts=snap["attempts"],
            screenshot_path=screenshot_path)
    if new_status == "awaiting_llm":
        _notify_awaiting_llm(job_id)
    if new_status == "done" and notify:
        _notify_protocol_ready(job_id)
    return job


def _notify_awaiting_llm(job_id: int) -> None:
    ns = NotifyService()
    with get_session() as s:
        job = s.get(Job, job_id)
        topic = job.topic
    ns.enqueue("awaiting_llm",
               f"[Mnema] Стенограмма готова, LLM недоступен: {topic}",
               f"Встреча: {topic or '(без темы)'}\n"
               "Стенограмма сформирована, структурирование отложено "
               "(LLM не настроен или недоступен). Донастройте LLM и запустите "
               f"повторную обработку: {ns.base_url}/jobs/{job_id}",
               dedup_key=f"awaiting_llm:{job_id}",
               data={"job_id": job_id, "topic": topic,
                     "job_url": f"{ns.base_url}/jobs/{job_id}"})


def _notify_protocol_ready(job_id: int) -> None:
    ns = NotifyService()
    with get_session() as s:
        job = s.get(Job, job_id)
        proto = s.scalars(select(Protocol).where(Protocol.job_id == job_id)).first()
        topic = job.topic
    audio = f"{ns.base_url}/api/protocols/{proto.id}/audio" if proto else None
    protocol_url = (f"{ns.base_url}/protocols/{proto.id}" if proto
                    else f"{ns.base_url}/jobs/{job_id}")
    ns.enqueue("protocol_ready",
               f"[Mnema] Протокол готов: {topic}",
               f"Встреча: {topic or '(без темы)'}\n"
               f"Протокол: {protocol_url}"
               + (f"\nАудиозапись: {audio}" if audio else ""),
               dedup_key=f"protocol_ready:{job_id}",
               data={"job_id": job_id, "topic": topic,
                     "job_url": f"{ns.base_url}/jobs/{job_id}",
                     "protocol_url": protocol_url,
                     "protocol_id": proto.id if proto else None,
                     "audio_url": audio})


def register_recording_finished(job_id: int, *, webm_path: str | None,
                                duration_s: float | None,
                                events: list[dict] | None = None) -> None:
    """vypolnyaetsya pered transition(recording->processing): pishi audio_files + event-log.
    Idempotenten po kind (salvage mozhet povtorit' finished posle smerti mezhdu
    finalize i otvetom api) — vklad webm pishetsya odin raz."""
    with get_session() as s:
        if webm_path and s.scalars(select(AudioFile).where(
                AudioFile.job_id == job_id, AudioFile.kind == "webm")).first() is None:
            import os as _os
            s.add(AudioFile(job_id=job_id, kind="webm", path=webm_path,
                            bytes=(_os.path.getsize(webm_path)
                                   if _os.path.exists(webm_path) else None),
                            duration_s=duration_s))
        for e in (events or []):
            s.add(SessionEvent(job_id=job_id, event=e.get("event", "media"),
                               detail=str(e.get("detail", ""))[:2000]))
        s.commit()


def resume_after_reviews(job_id: int) -> bool:
    """Vse utochneniya zakryty (otvet operatora ili taym-aut) -> vozvrat v
    processing i zapusk LLM-analiza. Vozvraschaet False, esli ostalis' pending
    review-request ili status ne pozvolyayet vozvrat v processing."""
    if not review_requests_resolved(job_id):
        return False
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None or job.status != "awaiting_review":
            return False
    transition(job_id, "processing", event="reviews_done")
    from .queue import enqueue_job
    enqueue_job("structure", "mnema.worker.tasks.run_structure", job_id,
                dedupe=True)
    return True


def review_timeout_check() -> int:
    """FT-28: neta otvetov po REVIEW_TIMEOUT_HOURS — publikuem s 'Golos N'."""
    hours = int(os.environ.get("REVIEW_TIMEOUT_HOURS", "48") or 48)
    now = dt.datetime.now(dt.timezone.utc)
    done = 0
    with get_session() as s:
        jobs = s.scalars(select(Job).where(Job.status == "awaiting_review")).all()
        ids = []
        for j in jobs:
            deadline = j.review_deadline
            if deadline is None:
                continue
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=dt.timezone.utc)
            if now >= deadline:
                rr = s.scalars(select(ReviewRequest).where(
                    ReviewRequest.job_id == j.id,
                    ReviewRequest.status == "pending")).all()
                for r in rr:
                    r.status = "timeout"
                    r.answered_ts = now
                    r.answered_by = "timeout"
                j.note = "уточнение по тайм-ауту (FT-28): публикуем как определено"
                ids.append(j.id)
        s.commit()
    # 5.1: posle taym-atau LLM-analiz vse ravno vypolnyaetsya (nezavershonnyy
    # protokol — ne rezultat); pri utochnenii "Golos N" imena ostayutsya kak est'.
    for jid in ids:
        try:
            if resume_after_reviews(jid):
                done += 1
        except Exception:  # noqa: BLE001 — ne lonim maintenance
            pass
    return done


def review_requests_resolved(job_id: int) -> bool:
    with get_session() as s:
        unresolved = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == job_id,
            ReviewRequest.status == "pending")).first()
        return unresolved is None


def _heartbeat_age_s() -> float | None:
    """Vozrast molotka web-bot (fayl na obshchem /data); None — fayla net."""
    import os
    from pathlib import Path
    hb = os.environ.get("WEBBOT_HEARTBEAT_FILE", "/data/web-bot.heartbeat")
    try:
        ts = float(Path(hb).read_text().strip())
    except (OSError, ValueError):
        return None
    return max(0.0, dt.datetime.now(dt.timezone.utc).timestamp() - ts / 1000.0)


def stalled_recording_watch() -> int:
    """Watchdog zalishchayushchikh v `recording`: job bez novykh sobytiy dolshe
    STALLED_RECORDING_MIN — oznachayet, chto zapisi bol'she net ni v rekordere,
    ni v API (web-bot umer mezhdu finalize i POST /finished, /finished ne
    doshyel, ili recorder molchit pri zhivoj stranice). Syryye segmenty mogut
    lezat v /data/jobs/<id> — stavim job v salvage-ochered' web-bot: tot
    peresoberet ih v final'nye fayly i soobschit finished (akaktivnuyu v tom
    zhe processe zadachu sam propustit).

    Vklad 'salvage_requested' pisetsya v zhurnal tol'ko pri uspeshnom enqueue
    (pri nedostupnom redis marku ne pishem — taymer tishiny ne sbrosyetsya,
    sleduyushchiy protih maintenance poprovit snova) i peremelaya taymer
    tishiny, poproskolu sleduyushchaya popitka vozmozhna ne ranye etogo zhe
    poroga — bez spam-a kazhdye 5 minut. Vozvraschaet chislo postavlennyh
    salvage."""
    import os
    minutes = int(os.environ.get("STALLED_RECORDING_MIN", "20") or 20)
    now = dt.datetime.now(dt.timezone.utc)
    with get_session() as s:
        ids = [j.id for j in s.scalars(
            select(Job).where(Job.status == "recording"))]
    from . import jobs_launch
    requested = 0
    for jid in ids:
        with get_session() as s:
            job = s.get(Job, jid)
            if job is None or job.status != "recording":
                continue
            last_ev = s.scalars(select(SessionEvent).where(
                SessionEvent.job_id == jid).order_by(SessionEvent.ts.desc())
                .limit(1)).first()
            base = job.started_ts or job.start_at or now
            if base.tzinfo is None:
                base = base.replace(tzinfo=dt.timezone.utc)
            lt = None
            if last_ev is not None:
                lt = last_ev.ts
                if lt.tzinfo is None:
                    lt = lt.replace(tzinfo=dt.timezone.utc)
            quiet_min = (now - (lt or base)).total_seconds() / 60.0
            if quiet_min < minutes:
                continue
            hb_age = _heartbeat_age_s()
            if not jobs_launch.request_salvage(jid, "stalled_recording"):
                continue  # redis nedostupen — marku ne pishem, taymer ne sbros
            s.add(SessionEvent(
                job_id=jid, event="salvage_requested",
                detail=f"watchdog: событий нет {int(quiet_min)} мин; heartbeat "
                       + ("нет данных" if hb_age is None else f"{int(hb_age)} с")))
            s.commit()
            requested += 1
    return requested


def rerun_llm_analysis(job_id: int) -> bool:
    """Ruchnoy perezapusk LLM-analiza (knopka v WEB): steno.md s itogovymi
    imenami spikero + LLM strukturirovanie. Dostupno iz done/awaiting_llm.
    Vozvraschaet False, esli status ne pozvolyayet perezapusk."""
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise KeyError(job_id)
        st = job.status
    if st not in ("done", "awaiting_llm"):
        return False
    transition(job_id, "processing", event="llm_rerun")
    from .queue import enqueue_job
    # dedupe: pendaushchayasya staraya popytka tova zhe joba zhdyot swoey
    # ocheredi k LLM — vtoraya tushhe stavit nelzya (derzhalaby zamyok dvoyne)
    enqueue_job("structure", "mnema.worker.tasks.run_structure", job_id,
                dedupe=True)
    return True


# ---------------------------------------------------------------- P1-1: sweeper
CONVEYOR_QUEUES = ("prep", "asr", "speaker", "structure")
# func, kotoryye derzhat job v konveyere (args[0] == job_id)
CONVEYOR_FUNCS = ("run_orchestrate", "run_asr_text", "run_speaker_id",
                  "run_join", "run_structure", "run_asr_pipeline")


def _rq_connection():
    from redis import Redis
    from .queue import redis_url
    r = Redis.from_url(redis_url(), socket_connect_timeout=2)
    r.ping()
    return r


def _job_task_running(r, job_id: int) -> bool:
    """Est' li活vaya/ozhidayushchaya RQ-zadacha konveyera dlya etogo joba
    (queued|deferred|started). Fail-open pri sboyakh redis."""
    from rq import Queue
    from rq.job import Job
    from rq.registry import StartedJobRegistry
    for name in CONVEYOR_QUEUES:
        try:
            q = Queue(name, connection=r)
            ids = list(q.get_job_ids())
        except Exception:  # noqa: BLE001
            continue
        try:
            reg = StartedJobRegistry(name, connection=r)
            ids += list(reg.get_job_ids())
        except Exception:  # noqa: BLE001
            pass
        for jid in ids:
            try:
                j = Job.fetch(jid, connection=r)
            except Exception:  # noqa: BLE001
                continue
            if any(f in (j.func_name or "") for f in CONVEYOR_FUNCS) \
                    and list(j.args or [])[:1] == [job_id]:
                return True
    return False


def _requeue_failed(r, job_id: int) -> int:
    """P3-14: FailedJobRegistry nikto ne chital — upavshie RQ-zadachi lezhali
    tam vечno, a job visel v processing. Nahodim failed-zadachu konveyera po
    job_id i stavim v ochered' snova (odna popytka; povtornyy pad vnov'
    popadet syuda, do limita pererekhud)."""
    from rq.job import Job
    from rq.registry import FailedJobRegistry
    requeued = 0
    for name in CONVEYOR_QUEUES:
        try:
            reg = FailedJobRegistry(name, connection=r)
            ids = list(reg.get_job_ids())
        except Exception:  # noqa: BLE001
            continue
        for jid in ids:
            try:
                j = Job.fetch(jid, connection=r)
            except Exception:  # noqa: BLE001
                reg.remove(jid)  # syrom no: zadacha ischezla — chistaem ref
                continue
            if any(f in (j.func_name or "") for f in CONVEYOR_FUNCS) \
                    and list(j.args or [])[:1] == [job_id]:
                try:
                    reg.requeue(jid)
                    requeued += 1
                except Exception:  # noqa: BLE001
                    log.exception("failed registry: ne udalos' requeue %s", jid)
    return requeued


def stalled_processing_watch() -> dict:
    """P1-1: zalishchie `processing`. V pute shod pered nim (web-bot umer —
    sm. stalled_recording_watch), no esli ASR/Diar/LLM-jezhik umer vmeste s
    worker-konteynerom (OOM, deploy poseredine obraabotki), job visel v
    processing beskonechno: ochered' pustaya, nikto ego ne tronet.

    Algoritm (kazhdiy maintenance tick):
      1. job v processing bez novykh sobytiy dolshe PROCESSING_STALL_MIN;
      2. esli v FailedJobRegistry est' upavshaya zadacha etogo joba — requeue
         (eto vozzhivyayet bez poteri progressa vetok);
      3. inache, esli v ocheredyah/started nichego net — polnyy re-enqueue
         run_orchestrate (prep/VAD/segm idempotentny, HT-2);
      4. posle MAX_REQUEUES perekhudov — failed (stalled_processing), s
         uedomleniem: bessmyslenno gonyat' konveyer, kotoryy padayet kazhdyy
          raz. Vozvraschat dict s chislami deystviy."""
    minutes = int(os.environ.get("PROCESSING_STALL_MIN", "120") or 120)
    max_requeues = int(os.environ.get("PROCESSING_MAX_REQUEUES", "3") or 3)
    now = dt.datetime.now(dt.timezone.utc)
    out = {"requeued_failed": 0, "requeued_full": 0, "failed": 0}
    with get_session() as s:
        ids = [j.id for j in s.scalars(
            select(Job).where(Job.status == "processing"))]
    if not ids:
        return out
    try:
        r = _rq_connection()
    except Exception:  # noqa: BLE001 — redis net: proveryat' nechem, zhdem tik
        return out
    from .queue import enqueue_job, get_queue
    _, mode = get_queue("prep")
    if mode != "rq":
        return out
    actions: list[tuple[int, str]] = []  # (job_id, "fail"|"failed"|"full")
    for jid in ids:
        with get_session() as s:
            job = s.get(Job, jid)
            if job is None or job.status != "processing":
                continue
            last_ev = s.scalars(select(SessionEvent).where(
                SessionEvent.job_id == jid).order_by(
                SessionEvent.ts.desc()).limit(1)).first()
            lt = last_ev.ts if last_ev is not None else None
            if lt is not None and lt.tzinfo is None:
                lt = lt.replace(tzinfo=dt.timezone.utc)
            quiet_min = (now - lt).total_seconds() / 60.0 if lt else None
            if quiet_min is not None and quiet_min < minutes:
                continue
            n_sweeps = len(s.scalars(select(SessionEvent).where(
                SessionEvent.job_id == jid,
                SessionEvent.event.in_(("processing_requeued_failed",
                                        "processing_requeued_full")))).all())
        if _job_task_running(r, jid):
            continue  # konveyer zhiv (dolgiy ASR/LLM) — ne vmeshivaemsya
        if n_sweeps >= max_requeues:
            actions.append((jid, "fail"))
            continue
        if _requeue_failed(r, jid):
            actions.append((jid, "failed"))
            continue
        actions.append((jid, "full"))
    # deystviya s prirostochnymi perekhodami — vne chitaushchey sessii
    # (transition otkryvaet svoyu; SQLite ne lyubit overlapping transaksii)
    for jid, kind in actions:
        if kind == "fail":
            try:
                transition(jid, "failed", fail_reason="stalled_processing",
                           error="обработка зависла: перепостановки не помогли",
                           event="processing_swept_out")
                out["failed"] += 1
            except (ValueError, TransitionRace):
                pass  # paralel'nyy writer uzhe zavyershil — otstupaem
            continue
        if kind == "failed":
            event, detail = ("processing_requeued_failed",
                             "sweeper: upavshaya RQ-zadacha ponyata v ochered'")
            out["requeued_failed"] += 1
        else:
            enqueue_job("prep", "mnema.worker.tasks.run_orchestrate", jid,
                        dedupe=True)
            event, detail = ("processing_requeued_full",
                             "sweeper: konveyer pust, polnyy re-enqueue")
            out["requeued_full"] += 1
        with get_session() as s:
            s.add(SessionEvent(job_id=jid, event=event, detail=detail))
            s.commit()
    return out


def retention_purge() -> int:
    """P2-6: RETENTION_DAYS — chistka terminal'nyh jobov starshe N dney
    (done/failed/cancelled) BDD + faily /data/jobs/<id>. 0 = vyklucheno.
    Pak 50 za tick, chtoby ne blokirovat' maintenance dolgoy transakciey.
    Roditeli so соhranёnnymi det'mi-split propuskayutsya (FK whole)."""
    import os
    import shutil
    from pathlib import Path
    days = int(os.environ.get("RETENTION_DAYS", "180") or 0)
    if days <= 0:
        return 0
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    from .models import (AudioFile, ConfirmEndRequest, PendingCommand,
                         Protocol, ReviewRequest, Segment, Upload)
    purged = 0
    with get_session() as s:
        jobs = s.scalars(select(Job).where(
            Job.status.in_(("done", "failed", "cancelled")),
            Job.created_ts < cutoff).limit(50)).all()
        for j in jobs:
            has_child = s.scalars(select(Job).where(
                Job.parent_job_id == j.id).limit(1)).first()
            if has_child is not None:
                continue
            data_dir = Path(os.environ.get("DATA_DIR", "/data"))
            for a in s.scalars(select(AudioFile).where(
                    AudioFile.job_id == j.id)):
                try:
                    Path(a.path).unlink(missing_ok=True)
                except OSError:
                    pass
                s.delete(a)
            for p in s.scalars(select(Protocol).where(
                    Protocol.job_id == j.id)):
                for path in (p.md_path, p.docx_path, p.steno_md_path):
                    if path:
                        try:
                            Path(path).unlink(missing_ok=True)
                        except OSError:
                            pass
                s.delete(p)
            for up in s.scalars(select(Upload).where(Upload.job_id == j.id)):
                try:
                    Path(up.path).unlink(missing_ok=True)
                except OSError:
                    pass
                s.delete(up)
            for coll in (Segment, SessionEvent, ReviewRequest,
                         ConfirmEndRequest, PendingCommand):
                for row in s.scalars(select(coll).where(
                        coll.job_id == j.id)):
                    s.delete(row)
            job_dir = data_dir / "jobs" / str(j.id)
            try:
                shutil.rmtree(job_dir, ignore_errors=True)
            except OSError:
                pass
            s.delete(j)
            purged += 1
        s.commit()
    if purged:
        log.info("retention: udaleno jobov starshe %s dney: %s", days, purged)
    return purged
