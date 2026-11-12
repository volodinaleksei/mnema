"""Recovery otlozhennyh zapuskov posle restarta api (in-memory APScheduler)."""
import datetime as dt
import json
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


def _mk_job(status: str, start_at=None):
    from mnema.core.db import get_session
    from mnema.core.models import Job
    with get_session() as s:
        j = Job(topic="t", status=status, start_at=start_at)
        s.add(j)
        s.commit()
        return j.id


def _mk_naive_utc(dt_value: dt.datetime) -> dt.datetime:
    """SQLite (DateTime(timezone=True)) otdaet sohrannyy aware UTC naivno,
    v tom vide kak ego real'no vозvraschaet БД."""
    return(dt_value.replace(tzinfo=None))


@pytest.mark.parametrize("status", ["scheduled", "launching", "recording",
                                    "processing", "done", "failed", "cancelled"])
def test_reschedule_only_scheduled(db, monkeypatch, status):
    from mnema.core import scheduler
    pushed: list[tuple[int, dt.datetime]] = []
    monkeypatch.setattr(scheduler, "schedule_job_for",
                        lambda jid: pushed.append((jid, None)))
    ids = {status: _mk_job(status)}
    n = scheduler.reschedule_missed_jobs()
    if status == "scheduled":
        assert n == 1
        assert pushed == [(ids[status], None)]
    else:
        assert n == 0
        assert pushed == []


def test_reschedule_empty(db):
    from mnema.core import scheduler
    assert scheduler.reschedule_missed_jobs() == 0


def _ics(start: str, end: str) -> str:
    return ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1@x\nSUMMARY:T\n"
            f"DTSTART:{start}\nDTEND:{end}\n"
            "DESCRIPTION:https://zoom.us/j/123456789\nEND:VEVENT\nEND:VCALENDAR")


def test_parse_ics_floating_times_use_app_tz(monkeypatch):
    """Floating (bez Z i TZID) DTSTART/DTEND = lokal'noe vremya (MSK):
    14:00-15:00 -> start 11:00 UTC, dlitel'nost' 60 min, a ne 3+1=4 chasa."""
    from mnema.core import scheduler
    monkeypatch.setenv("APP_TZ", "Europe/Moscow")
    evs = scheduler.parse_ics(_ics("20261001T140000", "20261001T150000"))
    assert len(evs) == 1
    assert evs[0]["start_at"] == dt.datetime(2026, 10, 1, 11, 0,
                                             tzinfo=dt.timezone.utc)
    assert evs[0]["duration_max_min"] == 60


def test_parse_ics_utc_times_passthrough(monkeypatch):
    from mnema.core import scheduler
    monkeypatch.setenv("APP_TZ", "Europe/Moscow")
    evs = scheduler.parse_ics(_ics("20261001T140000Z", "20261001T150000Z"))
    assert evs[0]["start_at"] == dt.datetime(2026, 10, 1, 14, 0,
                                             tzinfo=dt.timezone.utc)
    assert evs[0]["duration_max_min"] == 60


def test_reschedule_survives_single_failure(db, monkeypatch):
    from mnema.core import scheduler
    ok_id = _mk_job("scheduled")
    boom_id = _mk_job("scheduled")

    def flaky(jid):
        if jid == boom_id:
            raise RuntimeError("boom")
    monkeypatch.setattr(scheduler, "schedule_job_for", flaky)
    # padeniye odnoy job ne dolzhno lonit' obrabotku ostal'nyh
    assert scheduler.reschedule_missed_jobs() == 2


def test_enqueue_launch_past_goes_to_redis(monkeypatch):
    """Prossrochennyy start_at -> pryamoy lpush v mnema:launch (bez APScheduler)."""
    from mnema.core import jobs_launch

    class FakeRedis:
        pushes: list[tuple[str, str]] = []

        @classmethod
        def from_url(cls, url, **kwargs):
            return cls()

        def lpush(self, key, payload):
            FakeRedis.pushes.append((key, payload))

    monkeypatch.setattr("redis.Redis", FakeRedis)
    jobs_launch.enqueue_launch(42, dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5))
    assert len(FakeRedis.pushes) == 1
    key, payload = FakeRedis.pushes[0]
    assert key == jobs_launch.LAUNCH_QUEUE_KEY
    assert json.loads(payload)["job_id"] == 42


def test_schedule_job_for_past_enqueues_immediately(db, monkeypatch):
    from mnema.core import scheduler, jobs_launch
    called: list[tuple[int, dt.datetime]] = []
    monkeypatch.setattr(jobs_launch, "enqueue_launch",
                        lambda jid, run_at: called.append((jid, run_at)) or True)
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    jid = _mk_job("scheduled", past)
    scheduler.schedule_job_for(jid)
    assert len(called) == 1
    assert called[0][0] == jid
    assert called[0][1] <= dt.datetime.now(dt.timezone.utc)


def test_schedule_job_for_future_respects_prejoin(db, monkeypatch):
    from mnema.core import scheduler, jobs_launch
    called: list[tuple[int, dt.datetime]] = []
    monkeypatch.setenv("BOT_PREJOIN_SECONDS", "120")
    monkeypatch.setattr(jobs_launch, "enqueue_launch",
                        lambda jid, run_at: called.append((jid, run_at)) or True)
    expected = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    jid = _mk_job("scheduled", _mk_naive_utc(expected))
    scheduler.schedule_job_for(jid)
    assert len(called) == 1
    delta = abs((called[0][1] - (expected - dt.timedelta(seconds=120))).total_seconds())
    assert delta < 1  # naivnyy datetime iz SQLite = UTC, bez povornogo SDVIGA


def test_schedule_job_for_default_prejoin_is_five_minutes(db, monkeypatch):
    """Bez BOT_PREJOIN_SECONDS bot podklyuchaetsya za 300 s do start_at."""
    from mnema.core import scheduler, jobs_launch
    called: list[tuple[int, dt.datetime]] = []
    monkeypatch.delenv("BOT_PREJOIN_SECONDS", raising=False)
    monkeypatch.setattr(jobs_launch, "enqueue_launch",
                        lambda jid, run_at: called.append((jid, run_at)) or True)
    expected = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    jid = _mk_job("scheduled", _mk_naive_utc(expected))
    scheduler.schedule_job_for(jid)
    assert len(called) == 1
    delta = abs((called[0][1] - (expected - dt.timedelta(seconds=300))).total_seconds())
    assert delta < 1


def test_schedule_job_for_naive_utc_not_shifted_by_app_tz(db, monkeypatch):
    """Regressiya vstrechi 56: 14:00 MSK -> run_at 13:58 UTC, a ne na 3 chasa ranshe."""
    from mnema.core import scheduler, jobs_launch
    called: list[tuple[int, dt.datetime]] = []
    monkeypatch.setenv("APP_TZ", "Europe/Moscow")
    monkeypatch.setenv("BOT_PREJOIN_SECONDS", "120")
    monkeypatch.setattr(jobs_launch, "enqueue_launch",
                        lambda jid, run_at: called.append((jid, run_at)) or True)
    start = dt.datetime(2099, 1, 1, 14, 0, tzinfo=dt.timezone.utc)  # hranitsya kak UTC
    jid = _mk_job("scheduled", start.replace(tzinfo=None))
    scheduler.schedule_job_for(jid)
    assert len(called) == 1
    assert called[0][1] == start - dt.timedelta(seconds=120)


def test_reschedule_end_to_end_past_job_pushes_launch(db, monkeypatch):
    """Prossrochennaya scheduled job posle «restarta» popadaet v ochered' zapuska."""
    from mnema.core import scheduler, jobs_launch

    pushed: list[dict] = []

    class FakeRedis:
        @classmethod
        def from_url(cls, url, **kwargs):
            return cls()

        def lpush(self, key, payload):
            pushed.append({"key": key, **json.loads(payload)})

    monkeypatch.setattr("redis.Redis", FakeRedis)
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)
    jid = _mk_job("scheduled", past)
    assert scheduler.reschedule_missed_jobs() == 1
    assert len(pushed) == 1
    assert pushed[0]["key"] == jobs_launch.LAUNCH_QUEUE_KEY
    assert pushed[0]["job_id"] == jid
