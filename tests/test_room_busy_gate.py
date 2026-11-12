"""Gate komnaty: novaya vstrecha NE zakhodit v Zoom-ID, gde uzhe idet zapis'.

Zanyatost' = sushchestvuet drugoy job s tem zhe zoom_meeting_id v statuse
launching/recording. Zapusk novoy otlozhdennoy/semrazu job gasitsya failom
s fail_reason=room_busy.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, StaticPool

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    import mnema.core.models  # registraciya tablits
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    return engine


def _mk_job(mid, status, start_at=None):
    from mnema.core.db import get_session
    from mnema.core.models import Job
    with get_session() as s:
        j = Job(topic="t", zoom_meeting_id=mid, status=status, start_at=start_at)
        s.add(j)
        s.commit()
        return j.id


class FakeRedis:
    def __init__(self):
        self.pushes = []

    @classmethod
    def from_url(cls, url, **kwargs):
        return cls._inst

    def lpush(self, key, payload):
        FakeRedis._inst.pushes.append((key, payload))


@pytest.fixture()
def fake_redis(monkeypatch):
    FakeRedis._inst = FakeRedis()
    monkeypatch.setattr("redis.Redis", FakeRedis)
    return FakeRedis._inst


def test_find_room_busy_detects_recording_same_room(db):
    from mnema.core import jobs_launch
    _mk_job("111", "recording")
    new_id = _mk_job("111", "scheduled")
    assert jobs_launch.find_room_busy(new_id) is not None


def test_find_room_busy_ignores_other_room(db):
    from mnema.core import jobs_launch
    _mk_job("111", "recording")
    other = _mk_job("222", "scheduled")
    assert jobs_launch.find_room_busy(other) is None


def test_find_room_busy_ignores_finished_room(db):
    from mnema.core import jobs_launch
    _mk_job("111", "done")
    _mk_job("111", "failed")
    _mk_job("111", "cancelled")
    new_id = _mk_job("111", "scheduled")
    assert jobs_launch.find_room_busy(new_id) is None


def test_find_room_busy_detects_launching(db):
    from mnema.core import jobs_launch
    _mk_job("111", "launching")
    new_id = _mk_job("111", "scheduled")
    busy = jobs_launch.find_room_busy(new_id)
    assert busy is not None and busy[1] == "launching"


def test_find_room_busy_ignores_itself(db):
    """Job v recording ne schitaet sam sebya zanyatym (svoj zhe sozvon)."""
    from mnema.core import jobs_launch
    jid = _mk_job("111", "recording")
    assert jobs_launch.find_room_busy(jid) is None


def test_find_room_busy_no_meeting_id(db):
    from mnema.core import jobs_launch
    jid = _mk_job(None, "scheduled")
    assert jobs_launch.find_room_busy(jid) is None


def test_fail_if_room_busy_marks_failed(db, fake_redis):
    from mnema.core import jobs_launch
    from mnema.core.db import get_session
    from mnema.core.models import Job
    _mk_job("111", "recording")
    new_id = _mk_job("111", "scheduled")
    assert jobs_launch.fail_if_room_busy(new_id) is True
    with get_session() as s:
        assert s.get(Job, new_id).status == "failed"
        assert s.get(Job, new_id).fail_reason == "room_busy"


def test_fail_if_room_busy_free_room_noop(db, fake_redis):
    from mnema.core import jobs_launch
    from mnema.core.db import get_session
    from mnema.core.models import Job
    new_id = _mk_job("222", "scheduled")
    assert jobs_launch.fail_if_room_busy(new_id) is False
    with get_session() as s:
        assert s.get(Job, new_id).status == "scheduled"


def test_fail_if_room_busy_swallows_db_error(db, fake_redis, monkeypatch):
    """Sboy BDD pri perehode v failed: isklyuchenie ne lonit scheduler,
    no zapusk vsyo ravno gasitsya (True, bez push)."""
    from mnema.core import jobs_launch

    def boom(*a, **k):
        raise RuntimeError("BDD nedostupna")

    monkeypatch.setattr("mnema.core.pipeline.transition", boom)
    _mk_job("111", "recording")
    new_id = _mk_job("111", "scheduled")
    assert jobs_launch.fail_if_room_busy(new_id) is True
    assert [k for (k, _) in fake_redis.pushes] == []


def test_fail_if_room_busy_kill_switch_off(db, fake_redis, monkeypatch):
    monkeypatch.setenv("ROOM_BUSY_GATE", "0")
    from mnema.core import jobs_launch
    from mnema.core.db import get_session
    from mnema.core.models import Job
    _mk_job("111", "recording")
    new_id = _mk_job("111", "scheduled")
    assert jobs_launch.fail_if_room_busy(new_id) is False
    with get_session() as s:
        assert s.get(Job, new_id).status == "scheduled"


def test_enqueue_launch_immediate_blocked_when_busy(db, fake_redis):
    """Prossrochennyj start: komnata zanyata -> NE push, job failed."""
    from mnema.core import jobs_launch
    from mnema.core.db import get_session
    from mnema.core.models import Job
    _mk_job("111", "recording")
    new_id = _mk_job("111", "scheduled")
    ok = jobs_launch.enqueue_launch(
        new_id, dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
    assert ok is False
    assert [k for (k, _) in fake_redis.pushes
            if k == jobs_launch.LAUNCH_QUEUE_KEY] == []
    with get_session() as s:
        assert s.get(Job, new_id).status == "failed"


def test_enqueue_launch_immediate_pushes_when_free(db, fake_redis):
    from mnema.core import jobs_launch
    new_id = _mk_job("222", "scheduled")
    ok = jobs_launch.enqueue_launch(
        new_id, dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
    assert ok is True
    assert len(fake_redis.pushes) == 1


def test_enqueue_launch_future_gate_runs_at_push(db, fake_redis, monkeypatch):
    """Otlozhennyj start: APScheduler v nuzhnoye vremya vyzyvaet _push, kotoryy
    SNova proveryaet komnatu. Modelliruem nastuplenie vremeni: k momentu push
    komnata zanyata -> ne popadaet v ochered' i failed."""
    from mnema.core import jobs_launch
    from mnema.core.db import get_session
    from mnema.core.models import Job

    scheduled = {}

    class FakeScheduler:
        def add_job(self, fn, **kw):
            scheduled["fn"] = fn
            scheduled["kw"] = kw

    monkeypatch.setattr("mnema.core.scheduler.get_scheduler", lambda: FakeScheduler())
    _mk_job("111", "recording")
    new_id = _mk_job("111", "scheduled")
    run_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    ok = jobs_launch.enqueue_launch(new_id, run_at)
    assert ok is True
    # v momentu planirovaniya push esche ne byl (otlozhennyj)
    assert fake_redis.pushes == []
    # pri nastuplenii vremeni vyzyvaetsya _push (s kwargs kak APScheduler) —
    # on gasit zapusk
    scheduled["fn"](**scheduled["kw"].get("kwargs", {}))
    assert [k for (k, _) in fake_redis.pushes
            if k == jobs_launch.LAUNCH_QUEUE_KEY] == []
    with get_session() as s:
        assert s.get(Job, new_id).status == "failed"
