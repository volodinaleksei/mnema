"""RQ workera: roli prep/asr/speaker/structure (6.4).

WORKER_QUEUES=prep,asr,speaker,structure (po umolchaniyu vse).

P3-15: prep/VAD (run_orchestrate) vyneseny v otdel'nuyu ochered' prep:
ran'she voshod konveyera (raspadovka+VAD, desyatki minut) stoyal v odnoy
ocheredi s tyazhelym ASR i blokiroval ego (head-of-line), a dolgiy ASR
derzhal slot i ostanavlival start novyh vstrech. 'prep' podklyuchaetsya
vsegda, daie esli WORKER_QUEUES zadan vruchnuyu bez nego (inache jobi,
zapisannye v prep v staroy konfiguracii, nikto ne chyital by).
"""
from __future__ import annotations

import os
import sys

from redis import Redis

from ..core.log_setup import setup_logging
from ..core.queue import redis_url
from . import tasks

log = setup_logging("worker")


QUEUES = {
    "asr": tasks.run_asr_text,
    "speaker": tasks.run_speaker_id,
    "structure": tasks.run_join,
}


def _start_heartbeat() -> None:
    """Molotok zhivosti worker na obshchem /data dlya docker-healthcheck:
    main-protsess (a ne horse) trogaet fayl kazhdy 30 s. Esli konteyner
    zyblitsa/pribit, molotok molchit i healthcheck perekluchaet v unhealthy."""
    import threading
    import time

    path = os.environ.get("WORKER_HEARTBEAT_FILE", "/data/worker.heartbeat")

    def tick() -> None:
        while True:
            try:
                with open(path, "w") as f:
                    f.write(str(int(time.time())))
            except OSError as e:
                log.warning("worker heartbeat write failed: %s", e)
            time.sleep(30)

    threading.Thread(target=tick, name="worker-heartbeat", daemon=True).start()


def _queue_names() -> list[str]:
    names = [q.strip() for q in os.environ.get(
        "WORKER_QUEUES", "prep,asr,speaker,structure").split(",") if q.strip()]
    if "prep" not in names:  # P3-15: predohranenie ot starogo .env
        names.append("prep")
    return names


def run_worker() -> None:
    names = _queue_names()
    r = Redis.from_url(redis_url())
    conc = max(1, int(os.environ.get("WORKER_CONCURRENCY", "2") or 2))
    log.info("rq worker start: queues=%s procs=%s", names, conc)
    _start_heartbeat()
    import multiprocessing as mp
    kids = [mp.Process(target=_worker_loop, args=(names,), daemon=True)
            for _ in range(conc - 1)]
    for k in kids:
        k.start()
    _start_scheduler(r, names)
    _start_kid_supervisor(kids, names)
    try:
        _worker_loop(names)
    finally:
        for k in kids:
            k.terminate()


def _start_kid_supervisor(kids: list, names: list[str]) -> None:
    """M13: esli odin iz horse'ov umer (OOM-killer, RQ-bug), to eshe tihe
    propadaet — ocheredi narastaют, no roditel' ob etom ne znaet. Sledim i
    perezapuskaem umershikh; shutdov (terminatsiya v finally) ostanavlivает
    nadzyatelya, ibo on daemon."""
    import threading
    import time as _time

    def supervise() -> None:
        while True:
            _time.sleep(30)
            for i, k in enumerate(kids):
                if k.is_alive():
                    continue
                log.error("worker horse #%d umer (exit=%s) — perezapuskaem",
                          i, k.exitcode)
                try:
                    new = type(k)(target=_worker_loop, args=(names,), daemon=True)
                    new.start()
                    kids[i] = new
                except Exception as e:  # noqa: BLE001 — ne lonit' nadzyatelya
                    log.error("restart horse #%d failed: %s", i, e)

    threading.Thread(target=supervise, name="rq-kid-supervisor",
                     daemon=True).start()


def _worker_loop(names: list[str]) -> None:
    from rq import Queue, Worker
    r = Redis.from_url(redis_url())
    queues = [Queue(n, connection=r, default_timeout=4 * 3600) for n in names]
    Worker(queues, connection=r).work()


def _start_scheduler(r, names: list[str]) -> None:
    """RedisScheduler v dashneem potoke: bez nego jobs s depends_on (join po
    oboim vetkam) ostatutsya Deferred navsegda."""
    import threading
    try:
        from rq.scheduler import RQScheduler
        sched = RQScheduler(names, connection=r, interval=1)
        threading.Thread(target=sched.start, name="rq-scheduler",
                         daemon=True).start()
        log.info("rq scheduler started")
    except Exception as e:  # noqa: BLE001
        log.error("rq scheduler unavailable (%s) — depends_on jobs will not run", e)


def run_worker_sync() -> None:
    """Single-shot: otdelyt' po odnoy zadahe iz kazhdoi ocheredi (dlya testov)."""
    from rq import Queue, SimpleWorker
    names = _queue_names()
    r = Redis.from_url(redis_url())
    queues = {n: Queue(n, connection=r) for n in names}
    done = 0
    while True:
        progressed = 0
        for q in queues.values():
            if len(q) == 0:
                continue
            worker = SimpleWorker([q], connection=r, job_timeout="4h")
            worker.dequeue_job_and_maintain_ttl(0)
            progressed += 1
        if progressed == 0:
            break
        done += 1
        if done > 500:
            break
    return done


if __name__ == "__main__":
    if "--sync" in sys.argv:
        run_worker_sync()
    else:
        run_worker()
