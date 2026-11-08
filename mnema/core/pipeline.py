"""Orkestraciya konveyera (FT-34) i validnye perekhody statusov (HT-2).

scheduled -> launching -> recording -> processing -> awaiting_llm? ->
awaiting_review? -> done | failed. Retry shagov s backoff; povtor processing
idempotenten (HT-2): segm/protokol peresozydayutsya s nulya.
"""
from __future__ import annotations

import datetime as dt
import os

from sqlalchemy import select

from .db import get_session
from .models import AudioFile, Job, Notification, Protocol, ReviewRequest, SessionEvent
from .notify import NotifyService

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
            if new_status not in ALLOWED_TRANSITIONS.get(cur, set()):
                raise ValueError(
                    f"Недопустимый переход статуса: {cur} -> {new_status}")
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
    ns.enqueue("protocol_ready",
               f"[Mnema] Протокол готов: {topic}",
               f"Встреча: {topic or '(без темы)'}\n"
               f"Протокол: {ns.base_url}/jobs/{job_id}"
               + (f"\nАудиозапись: {audio}" if audio else ""),
               dedup_key=f"protocol_ready:{job_id}",
               data={"job_id": job_id, "topic": topic,
                     "job_url": f"{ns.base_url}/jobs/{job_id}",
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
