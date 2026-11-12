"""Zapusk RQ (redis) ili lokal'nyy "direct" rezhim dlya testov bez redis."""
from __future__ import annotations

import os
import time


def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://redis:6379/0").strip()


# P2-13: kesh bez invalidacii "prikleival" direct-mode navsegda esli redis byl
# nedostupen pri pervom zaprose (ili naoborot): zhivykh workerov ne bylo, a api
# uzhe reshil chto my v direct. Keshiruem na korotkiy TTL (0 = keshi navsegda,
# kak ran'she).
_queues: dict[str, tuple] = {}
_queues_ts: dict[str, float] = {}


def _queues_ttl() -> float:
    try:
        return float(os.environ.get("QUEUE_CACHE_TTL_S", "30") or 30)
    except ValueError:
        return 30.0


def reset_queue_cache() -> None:
    _queues.clear()
    _queues_ts.clear()


def get_queue(name: str):
    """Vozvrachaet (queue, mode); mode='rq'|'direct'. V direct vzyvaetsya inline."""
    ttl = _queues_ttl()
    now = time.monotonic()
    if name in _queues and (ttl <= 0 or now - _queues_ts.get(name, 0.0) < ttl):
        return _queues[name]
    try:
        from redis import Redis
        from rq import Queue
        r = Redis.from_url(redis_url(), socket_connect_timeout=1)
        r.ping()
        q = Queue(name, connection=r, default_timeout=4 * 3600)
        _queues[name] = (q, "rq")
    except Exception:
        _queues[name] = (None, "direct")
    _queues_ts[name] = now
    return _queues[name]


def _find_queued(r, name: str, func_name: str, args):
    """Pending/RDP-задача той же func+args в очереди name -> RQ Job, else None.

    Перебираем deferred+queued (маленькие): заstarted job здесь не найдётся,
    что верно — убивать работающий анализ нельзя."""
    from rq import Queue
    from rq.job import Job
    q = Queue(name, connection=r)
    for jid in list(q.get_job_ids()):
        try:
            j = Job.fetch(jid, connection=r)
        except Exception:  # noqa: BLE001 — гонка с RQ: считаем что нет
            continue
        if j.func_name == func_name and list(j.args or []) == list(args) \
                and not (j.kwargs or {}):
            return j
    return None


def enqueue_job(name: str, func, *args, depends_on=None, dedupe=False, **kwargs):
    """Enqueue v RQ (mozhno depends_on=Job|list); v direct mode — None.

    dedupe=True: esli v ocheredi uzhe stoit zadacha func+args (ne vypolnyaetsya,
    a zhdyot), nayavlennaya STARYM progonom — novaya prosto perenaznachaet eyo
    `dedupe`-meanovoy (zadacha vypolnitsya odin raz, ktogda dooydyot ochered',
    a ne nakanlivayet duplicates, kotoryye derzhat LLM-zamyok). Starting/started
    ne trogaem.

    Vozvrat: RQ Job v rq-rezhime, inache None (vyzyvatel' reshayet' sam)."""
    q, mode = get_queue(name)
    if mode != "rq":
        return None
    if dedupe:
        try:
            existing = _find_queued(q.connection, name, func, args)
            if existing is not None:
                return existing
        except Exception:  # noqa: BLE001 — dedup best-effort: ne lochat' enqueue
            pass
    return q.enqueue(func, *args, depends_on=depends_on, **kwargs)
