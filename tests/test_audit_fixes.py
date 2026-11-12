"""Testy ispravleniya nahodok audita (P1-1, P1-3, P1-4, P2-6, P2-8, P2-13, P3-17, P3-18).

Kazhdiy test zakreplyayet konkretnuyu nahodku iz spiska «Regress 18 nahodok».
Redis v testah nasted (REDIS_URL na zavedomo pustykh 6399), gde nado — monkeypatch.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import StaticPool, create_engine, select

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive-audit")
os.environ.setdefault("DATA_DIR", "/tmp/test-data-audit")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-audit.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # zavedomo niet
os.environ["LLM_BASE_URL"] = ""
os.environ["ASR_BASE_URL"] = ""


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import mnema.core.models  # noqa: F401 — modeli DO create_all
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    return engine


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    import mnema.core.models  # noqa: F401
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    with TestClient(app) as c:
        yield c


def _mkjob(status="scheduled"):
    from mnema.core.db import get_session
    from mnema.core.models import Job
    with get_session() as s:
        j = Job(topic="t", status=status)
        s.add(j)
        s.commit()
        return j.id


# ---------------------------------------------------------- P2-8: gonka /finished
def test_transition_to_same_status_is_race_not_valueerror(db):
    # dva /finished: vtoroy vidit uze «processing» — eto TransitionRace
    # (otvet 'already'), a ne ValueError/500
    from mnema.core.pipeline import TransitionRace, transition
    jid = _mkjob("recording")
    transition(jid, "processing")
    with pytest.raises(TransitionRace):
        transition(jid, "processing")


def test_transition_invalid_still_valueerror(db):
    # real'no nevalidnyy perekhod — kak ran'she ValueError (409 v api)
    from mnema.core.pipeline import transition
    jid = _mkjob("scheduled")
    with pytest.raises(ValueError):
        transition(jid, "done")


# ------------------------------------------------------- P3-18: fallback komand
def test_request_stop_writes_pending_command_when_redis_down(db, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    from mnema.core import jobs_launch
    assert jobs_launch.request_stop(1, "test") is False
    from mnema.core.db import get_session
    from mnema.core.models import PendingCommand
    with get_session() as s:
        rows = s.scalars(select(PendingCommand)).all()
    assert len(rows) == 1
    assert rows[0].cmd == "stop" and rows[0].job_id == 1


def test_drain_pending_commands_pushes_when_redis_up(db, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")
    jid = _mkjob("recording")
    from mnema.core import jobs_launch

    pushed = []

    class FakeRedis:
        @classmethod
        def from_url(cls, url, **kwargs):
            return cls()

        def ping(self):
            return True

        def lpush(self, key, payload):
            pushed.append(key)
            return 1
    monkeypatch.setattr("redis.Redis", FakeRedis)
    jobs_launch._fallback_command("stop", jid, "raz")
    n = jobs_launch.drain_pending_commands()
    assert n == 1
    assert pushed == [jobs_launch.STOP_QUEUE_KEY]
    from mnema.core.db import get_session
    from mnema.core.models import PendingCommand
    with get_session() as s:
        pc = s.scalars(select(PendingCommand)).first()
    assert pc.status == "pushed" and pc.attempts == 1


def test_drain_expires_commands_for_terminal_jobs(db, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")
    from mnema.core import jobs_launch
    jid = _mkjob("done")
    jobs_launch._fallback_command("stop", jid, None)
    assert jobs_launch.drain_pending_commands() == 0
    from mnema.core.db import get_session
    from mnema.core.models import PendingCommand
    with get_session() as s:
        pc = s.scalars(select(PendingCommand)).first()
    assert pc.status == "expired"


def test_fallback_dedupes_pending_commands(db, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    from mnema.core import jobs_launch
    jobs_launch._fallback_command("stop", 1, None)
    jobs_launch._fallback_command("stop", 1, None)
    from mnema.core.db import get_session
    from mnema.core.models import PendingCommand
    with get_session() as s:
        assert len(s.scalars(select(PendingCommand)).all()) == 1


# --------------------------------------------------------- P3-17: dedup import_ics
def _ics(uid: str) -> str:
    return ("BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            f"UID:{uid}\nSUMMARY:T\n"
            "DTSTART:20261113T140000Z\nDTEND:20261113T150000Z\n"
            "DESCRIPTION:https://zoom.us/j/1234567890\n"
            "END:VEVENT\nEND:VCALENDAR")


def test_import_ics_dedupes_by_uid(db, monkeypatch):
    monkeypatch.setattr("mnema.core.scheduler.schedule_job_for", lambda jid: None)
    from mnema.core.scheduler import import_ics
    assert import_ics(_ics("uid-1@host")) == 1
    assert import_ics(_ics("uid-1@host")) == 0  # dedup
    assert import_ics(_ics("uid-2@host")) == 1


# ------------------------------------------------------------- P2-6: retention
def test_retention_purge_removes_old_terminal_jobs(db, monkeypatch):
    from mnema.core.db import get_session
    from mnema.core.models import Job, SessionEvent
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)
    with get_session() as s:
        j = Job(topic="staryy", status="done", created_ts=old)
        s.add(j)
        s.commit()
        s.add(SessionEvent(job_id=j.id, event="created", ts=old))
        s.commit()
        jid = j.id
    monkeypatch.setenv("RETENTION_DAYS", "180")
    from mnema.core.pipeline import retention_purge
    assert retention_purge() == 1
    with get_session() as s:
        assert s.get(Job, jid) is None


def test_retention_keeps_recent_and_active(db, monkeypatch):
    from mnema.core.db import get_session
    from mnema.core.models import Job
    recent = dt.datetime.now(dt.timezone.utc)
    old = recent - dt.timedelta(days=400)
    with get_session() as s:
        j1 = Job(topic="recent", status="done", created_ts=recent)
        j2 = Job(topic="active", status="recording", created_ts=old)
        s.add_all([j1, j2])
        s.commit()
    monkeypatch.setenv("RETENTION_DAYS", "180")
    from mnema.core.pipeline import retention_purge
    assert retention_purge() == 0


def test_retention_disabled_when_zero(db):
    from mnema.core.db import get_session
    from mnema.core.models import Job
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=900)
    _ = Job.__table__
    with get_session() as s:
        s.add(Job(topic="drevniy", status="done", created_ts=old))
        s.commit()
    import os as _os
    _os.environ["RETENTION_DAYS"] = "0"
    try:
        from mnema.core.pipeline import retention_purge
        assert retention_purge() == 0
    finally:
        _os.environ["RETENTION_DAYS"] = "180"


def test_retention_skips_parents_with_children(db, monkeypatch):
    # roditel so hranimsym rebenkom split ne udalyaetsya (FK whole);
    # rebenok-starshe — da.
    from mnema.core.db import get_session
    from mnema.core.models import Job
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)
    with get_session() as s:
        parent = Job(topic="rodite", status="done", created_ts=old)
        s.add(parent)
        s.flush()
        child = Job(topic="rebenok", status="done", created_ts=old,
                    parent_job_id=parent.id)
        s.add(child)
        s.commit()
        pid, cid = parent.id, child.id
    monkeypatch.setenv("RETENTION_DAYS", "180")
    from mnema.core.pipeline import retention_purge
    assert retention_purge() == 1  # rebenok udalen, roditel propushchen
    with get_session() as s:
        assert s.get(Job, pid) is not None
        assert s.get(Job, cid) is None


# ------------------------------------------------------------ P2-13: TTL kesh
def test_queue_cache_expires_after_ttl(monkeypatch):
    import time
    from mnema.core import queue as q
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    q.reset_queue_cache()
    monkeypatch.setattr(q, "_queues_ttl", lambda: 0.05)
    assert q.get_queue("asr")[1] == "direct"
    time.sleep(0.08)
    # posle TTL zapros idet zanovo (nesmotrya na kesh — no itog tot zhe,
    # klyuchevo: kesh ne vechnyy)
    assert q.get_queue("asr")[1] == "direct"
    # pri ttl=0 keshiuet navsegda (kak ran'she)
    q.reset_queue_cache()
    monkeypatch.setattr(q, "_queues_ttl", lambda: 0)
    assert q.get_queue("asr")[1] == "direct"


def test_queue_cache_zero_ttl_keeps_entry(monkeypatch):
    from mnema.core import queue as q
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    q.reset_queue_cache()
    monkeypatch.setattr(q, "_queues_ttl", lambda: 0)
    first = q.get_queue("asr")
    calls = {"n": 0}
    real_get = q._queues.get

    def spy(name):
        calls["n"] += 1
        return real_get(name)
    second = q.get_queue("asr")
    assert second is first  # pri ttl=0 vtoroy vyzov beretsya iz kesha bez podklyucheniya


# --------------------------------------------------------------- P1-1: sweeper
def test_sweeper_noop_without_jobs_and_without_redis(db, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    from mnema.core.pipeline import stalled_processing_watch
    assert stalled_processing_watch() == {"requeued_failed": 0,
                                          "requeued_full": 0, "failed": 0}


def test_sweeper_skips_fresh_processing(db, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    jid = _mkjob("processing")
    from mnema.core.pipeline import stalled_processing_watch
    out = stalled_processing_watch()
    assert out["failed"] == 0
    from mnema.core.db import get_session
    from mnema.core.models import Job
    with get_session() as s:
        assert s.get(Job, jid).status == "processing"


def test_sweeper_fails_job_after_max_requeues(db, monkeypatch):
    # redis est' (fake), no konveyer pust; stale-dostatochno
    monkeypatch.setenv("PROCESSING_STALL_MIN", "0")
    monkeypatch.setenv("PROCESSING_MAX_REQUEUES", "2")
    from mnema.core import queue as q
    q.reset_queue_cache()
    from mnema.core.db import get_session
    from mnema.core.models import Job, SessionEvent

    class FakeQueue:
        def get_job_ids(self):
            return []

    class FakeConn:
        pass

    monkeypatch.setattr("mnema.core.pipeline._rq_connection", lambda: FakeConn())
    monkeypatch.setattr("mnema.core.pipeline._job_task_running",
                        lambda r, jid: False)
    monkeypatch.setattr("mnema.core.pipeline._requeue_failed", lambda r, jid: 0)
    monkeypatch.setattr(q, "get_queue", lambda name: (None, "rq"))
    monkeypatch.setattr(q, "enqueue_job", lambda *a, **k: None)
    jid = _mkjob("processing")
    with get_session() as s:
        for i in range(2):  # 2 sweepa uze bylo
            s.add(SessionEvent(job_id=jid, event="processing_requeued_full",
                               detail="x", ts=dt.datetime.now(dt.timezone.utc)))
        s.commit()
    from mnema.core.pipeline import stalled_processing_watch
    out = stalled_processing_watch()
    assert out["failed"] == 1
    with get_session() as s:
        j = s.get(Job, jid)
        assert j.status == "failed"
        assert j.fail_reason == "stalled_processing"


def test_sweeper_requeues_full_when_conveyor_empty(db, monkeypatch):
    monkeypatch.setenv("PROCESSING_STALL_MIN", "0")
    monkeypatch.setenv("PROCESSING_MAX_REQUEUES", "5")
    from mnema.core import queue as q
    enq = []
    monkeypatch.setattr("mnema.core.pipeline._rq_connection", lambda: object())
    monkeypatch.setattr("mnema.core.pipeline._job_task_running",
                        lambda r, jid: False)
    monkeypatch.setattr("mnema.core.pipeline._requeue_failed", lambda r, jid: 0)
    monkeypatch.setattr(q, "get_queue", lambda name: (None, "rq"))
    monkeypatch.setattr(q, "enqueue_job",
                        lambda queue, fn, jid, **k: enq.append((queue, fn, jid)))
    jid = _mkjob("processing")
    from mnema.core.pipeline import stalled_processing_watch
    out = stalled_processing_watch()
    assert out["requeued_full"] == 1
    assert enq and enq[0][0] == "prep"  # P3-15: entrance v prep-ochered'


# ----------------------------------------------------- P1-3: retry launch push
def test_schedule_job_for_push_retry_on_redis_down(db, monkeypatch):
    # P1-3: pri nastuplenii vremeni push ne teryaetsya pri redis-blip,
    # a peresozdayotsya s retry-cherez-60s
    from mnema.core import jobs_launch
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    added: list[dict] = []

    class FakeScheduler:
        def add_job(self, fn, trigger=None, run_date=None, id=None,
                    replace_existing=False, kwargs=None):
            added.append({"id": id, "kwargs": kwargs, "fn": fn})
    monkeypatch.setattr("mnema.core.scheduler.get_scheduler", lambda: FakeScheduler())
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
    assert jobs_launch.enqueue_launch(42, future) is True
    assert any(i["id"] == "launch:42" for i in added)
    # simulyuem srabatyvanie APScheduler: vyzovaem postavlennuyu fn s ee kwargs
    item = next(i for i in added if i["id"] == "launch:42")
    item["fn"](**item["kwargs"])
    # redis niz — vmesto propuska zadacha perezadana s retry-kwargs
    retries = [i for i in added if i["id"] == "launch:42:retry"]
    assert len(retries) == 1
    assert retries[0]["kwargs"]["remaining"] == 29


def test_schedule_job_for_past_direct_push_failure_no_raise(db, monkeypatch):
    # prossrochenny start + redis nizu — ne padayet (vozvrat False)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    from mnema.core import jobs_launch
    jid = _mkjob("scheduled")
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    assert jobs_launch.enqueue_launch(jid, past) is False


def test_push_launch_fallback_after_retries_exhausted(db, monkeypatch):
    # P1-3: posle исcherpaniya popytok — ne tishiy loss, a fallback v BDD
    from mnema.core import jobs_launch
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    jid = _mkjob("scheduled")
    jobs_launch._push_launch(jid, dt.datetime.now(dt.timezone.utc), 1)
    from mnema.core.db import get_session
    from mnema.core.models import PendingCommand
    with get_session() as s:
        pcs = s.scalars(select(PendingCommand)).all()
    assert len(pcs) == 1 and pcs[0].cmd == "launch" and pcs[0].job_id == jid


def test_drain_delivers_launch_command_when_redis_up(db, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")
    from mnema.core import jobs_launch
    pushed = []

    class FakeRedis:
        @classmethod
        def from_url(cls, url, **kwargs):
            return cls()

        def ping(self):
            return True

        def lpush(self, key, payload):
            pushed.append(key)
            return 1
    monkeypatch.setattr("redis.Redis", FakeRedis)
    jid = _mkjob("scheduled")
    jobs_launch._fallback_command("launch", jid, None)
    assert jobs_launch.drain_pending_commands() == 1
    assert pushed == [jobs_launch.LAUNCH_QUEUE_KEY]


def test_push_launch_retries_via_scheduler_on_blip(db, monkeypatch):
    from mnema.core import jobs_launch
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    added: list[dict] = []

    class FakeScheduler:
        def add_job(self, fn, trigger=None, run_date=None, id=None,
                    replace_existing=False, kwargs=None):
            added.append({"id": id, "kwargs": kwargs})
    monkeypatch.setattr("mnema.core.scheduler.get_scheduler", lambda: FakeScheduler())
    jid = _mkjob("scheduled")
    jobs_launch._push_launch(jid, dt.datetime.now(dt.timezone.utc), 5)
    assert added and added[0]["id"] == f"launch:{jid}:retry"
    assert added[0]["kwargs"]["remaining"] == 4


# ---------------------------------------------------------------- P1-4: auth
def test_auth_disabled_by_default(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200


def test_auth_required_when_token_set(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "sekrit")
    r = client.get("/api/jobs")
    assert r.status_code == 401
    r2 = client.get("/api/jobs", headers={"X-API-Key": "wrong"})
    assert r2.status_code == 401
    r3 = client.get("/api/jobs", headers={"X-API-Key": "sekrit"})
    assert r3.status_code == 200
    r4 = client.get("/api/jobs", headers={"Authorization": "Bearer sekrit"})
    assert r4.status_code == 200


def test_auth_health_exempt(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "sekrit")
    import redis as _redis
    monkeypatch.setattr(_redis.Redis, "ping", lambda self, *a, **k: True)
    assert client.get("/health").status_code == 200


def test_auth_login_sets_cookie(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "sekrit")
    r = client.get("/auth/login?token=sekrit", follow_redirects=False)
    assert r.status_code == 302
    assert "mnema_token=sekrit" in r.headers.get("set-cookie", "")
    r2 = client.get("/auth/login?token=bad")
    assert r2.status_code == 401


def test_web_redirects_to_login_when_token_set(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "sekrit")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers.get("location", "")


def test_confirm_end_exempt_with_own_token(client, monkeypatch):
    # samodostatochnyy unikal'nyy token — ne otkryvaem perimetr, no i ne lonim
    monkeypatch.setenv("API_TOKEN", "sekrit")
    r = client.get("/confirm-end/nonexistent-token")
    # ne 401 (izyato iz-perimetre); naprimer 404/410/302 — ne vkhod
    assert r.status_code != 401


# -------------------------------------------------- P2-11: reserve free-space
def test_check_free_space_uses_concurrent_reserve(monkeypatch, tmp_path):
    from mnema.core import uploads
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(uploads, "_free_bytes", lambda p: 1000)
    monkeypatch.setenv("UPLOAD_CONCURRENT_RESERVE", "2")
    with pytest.raises(ValueError):
        uploads._check_free_space(600)  # 600*2 > 1000
    uploads._check_free_space(400)  # 400*2 <= 1000 — prohodit


# ------------------------------------------------ P2-9: notify configurable max
def test_notify_max_attempts_configurable(db, monkeypatch):
    monkeypatch.setenv("NOTIFY_MAX_ATTEMPTS", "2")
    from mnema.core.notify import NotifyService
    n = NotifyService(sender=lambda m: (_ for _ in ()).throw(
        RuntimeError("smtp")))
    rec = n.enqueue("job_failed", "s", "b", dedup_key="k1")
    n.dispatch_pending(limit=1)
    from mnema.core.db import get_session
    from mnema.core.models import Notification
    with get_session() as s:
        row = s.get(Notification, rec.id)
        assert row.attempts == 1 and row.status == "pending"
    n.dispatch_pending(limit=1)
    with get_session() as s:
        row = s.get(Notification, rec.id)
        assert row.status == "error"  # posle 2 popytok


def test_notify_dispatch_async_does_not_block(db, monkeypatch):
    from mnema.core.notify import NotifyService
    sent = []
    n = NotifyService(sender=lambda m: sent.append(m))
    n.enqueue("job_failed", "s", "b", dedup_key="k2")
    n.dispatch_pending_async(limit=1)  # ne dolzhno lonit' vyzyvayushchiy
    import time
    for _ in range(50):
        time.sleep(0.05)
        if sent:
            break
    assert len(sent) == 1  # pismo poshlo v fone


# ------------------------------------------------ P3-15: prep ochered' v workere
def test_worker_queue_names_always_include_prep(monkeypatch):
    monkeypatch.setenv("WORKER_QUEUES", "asr,speaker,structure")
    import importlib
    import mnema.worker.main as wm
    names = wm._queue_names()
    assert "prep" in names


def test_worker_queue_names_default_prep_first(monkeypatch):
    monkeypatch.delenv("WORKER_QUEUES", raising=False)
    import mnema.worker.main as wm
    assert wm._queue_names() == ["prep", "asr", "speaker", "structure"]
