"""Integracionnye testy api + konveyera cherez TestClient (FF-21, 22, 34, 41).

Redis niedostupen v testah — enqueue padayet v no-op (queue mode=direct);
statusy menyaemsya cherez vnutrennie POST (kak web-bot).
"""
import datetime as dt
import os
import wave
import struct
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive")
os.environ.setdefault("DATA_DIR", "/tmp/test-data")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # zavedomo niet
# capability: pustye => not_configured (bez live-probe)
os.environ["LLM_BASE_URL"] = ""
os.environ["ASR_BASE_URL"] = ""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    with TestClient(app) as c:
        yield c


def test_health_shape(client):
    r = client.get("/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok"
    assert j["capabilities"]["llm"]["state"] == "not_configured"
    assert j["capabilities"]["asr"]["state"] == "not_configured"
    assert "zoom_session" in j and "queue" in j


def test_create_job_from_invite_link(client):
    r = client.post("/api/jobs", json={
        "meeting_link": "https://us02web.zoom.us/j/9814832357?pwd=abc",
        "topic": "Planerka"})
    assert r.status_code == 200
    jid = r.json()["id"]
    assert r.json()["status"] == "scheduled"
    g = client.get(f"/api/jobs/{jid}").json()
    assert g["zoom_meeting_id"] == "9814832357"
    # pwd НЕ v public otvete
    assert "pwd" not in g
    # no v nutrennem join-info
    ji = client.get(f"/api/jobs/{jid}/join-info").json()
    assert ji["meeting_id"] == "9814832357" and ji["pwd"] == "abc"


def test_job_lifecycle_events(client):
    jid = client.post("/api/jobs", json={
        "meeting_id": "123456789", "topic": "T"}).json()["id"]
    # web-bot: launching -> recording
    assert client.post(f"/api/jobs/{jid}/status", json={"status": "launching"}).status_code == 200
    assert client.post(f"/api/jobs/{jid}/status", json={"status": "recording"}).status_code == 200
    # sobytie
    client.post(f"/api/jobs/{jid}/events", json={"event": "joined", "detail": "ok"})
    g = client.get(f"/api/jobs/{jid}").json()
    assert any(e["event"] == "joined" for e in g["events"])


def test_invalid_status_transition_rejected(client):
    jid = client.post("/api/jobs", json={"meeting_id": "222333444"}).json()["id"]
    r = client.post(f"/api/jobs/{jid}/status", json={"status": "done"})
    assert r.status_code >= 400  # scheduled -> done nie dopustimo


def test_cancel_scheduled(client):
    jid = client.post("/api/jobs", json={"meeting_id": "555666777"}).json()["id"]
    r = client.delete(f"/api/jobs/{jid}")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


def test_rename_job_updates_topic(client):
    jid = client.post("/api/jobs", json={"meeting_id": "111222333",
                                         "topic": "Staroe"}).json()["id"]
    r = client.post(f"/jobs/{jid}/rename", data={"topic": "Novoe imya"})
    assert r.status_code == 200
    assert client.get(f"/api/jobs/{jid}").json()["topic"] == "Novoe imya"


def test_rename_job_empty_topic_rejected(client):
    jid = client.post("/api/jobs", json={"meeting_id": "111222334",
                                         "topic": "Staroe"}).json()["id"]
    r = client.post(f"/jobs/{jid}/rename", data={"topic": "   "})
    assert r.status_code == 422


def test_rename_job_not_found(client):
    r = client.post("/jobs/999999/rename", data={"topic": "Net"})
    assert r.status_code == 404


def test_delete_active_recording_rejected(client):
    jid = client.post("/api/jobs", json={"meeting_id": "111222335"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    client.post(f"/api/jobs/{jid}/status", json={"status": "recording"})
    r = client.post(f"/jobs/{jid}/delete", follow_redirects=False)
    assert r.status_code == 409
    assert client.get(f"/api/jobs/{jid}").status_code == 200


def test_delete_cancelled_job_removes_it(client):
    jid = client.post("/api/jobs", json={"meeting_id": "111222336",
                                         "topic": "Udalit"}).json()["id"]
    client.delete(f"/api/jobs/{jid}")  # cancelled
    r = client.post(f"/jobs/{jid}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs"
    assert client.get(f"/api/jobs/{jid}").status_code == 404


def test_delete_scheduled_job_removes_it(client):
    jid = client.post("/api/jobs", json={"meeting_id": "111222337",
                                         "topic": "Ne nachata"}).json()["id"]
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "scheduled"
    r = client.post(f"/jobs/{jid}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs"
    assert client.get(f"/api/jobs/{jid}").status_code == 404


def test_delete_launching_job_rejected(client):
    jid = client.post("/api/jobs", json={"meeting_id": "111222338"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    r = client.post(f"/jobs/{jid}/delete", follow_redirects=False)
    assert r.status_code == 409
    assert client.get(f"/api/jobs/{jid}").status_code == 200


def test_delete_job_not_found(client):
    assert client.post("/jobs/999999/delete").status_code == 404


def test_update_scheduled_job_params(client):
    jid = client.post("/api/jobs", json={
        "meeting_id": "111222401", "topic": "Staraia"}).json()["id"]
    new_start = "2030-01-01T10:00"  # datetime-local = lokal'noe vremyo
    r = client.post(f"/jobs/{jid}/update", follow_redirects=False, data={
        "topic": "Novaa", "meeting_link": "https://zoom.us/j/999888777"
                                        "?pwd=abcDEF123",
        "start_at": new_start, "end_at": "2030-01-01T11:30"})
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{jid}"
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["topic"] == "Novaa"
    assert j["zoom_meeting_id"] == "999888777"
    assert j["start_at"].startswith("2030-01-01T07:00")  # UTC = MSK-3
    from mnema.core.secrets_store import get_pwd
    from mnema.core.db import get_session
    from mnema.core.models import Job
    with get_session() as s:
        job = s.get(Job, jid)
        ref = job.pwd_ref
        assert job.end_at.strftime("%Y-%m-%dT%H:%M") == "2030-01-01T08:30"
    assert get_pwd(ref) == "abcDEF123"


def test_update_job_empty_topic_rejected(client):
    jid = client.post("/api/jobs", json={"meeting_id": "111222402"}).json()["id"]
    r = client.post(f"/jobs/{jid}/update", data={"topic": "   "})
    assert r.status_code == 422


def test_update_job_end_before_start_rejected(client):
    jid = client.post("/api/jobs", json={"meeting_id": "111222403"}).json()["id"]
    r = client.post(f"/jobs/{jid}/update", data={
        "topic": "T", "start_at": "2030-01-01T10:30",
        "end_at": "2030-01-01T10:00"})
    assert r.status_code == 422


def test_update_job_bad_link_rejected(client):
    jid = client.post("/api/jobs", json={"meeting_id": "111222404"}).json()["id"]
    r = client.post(f"/jobs/{jid}/update",
                    data={"topic": "T", "meeting_link": "net-linka"})
    assert r.status_code == 422


def test_update_recording_job_rejected(client):
    jid = client.post("/api/jobs", json={"meeting_id": "111222405"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    client.post(f"/api/jobs/{jid}/status", json={"status": "recording"})
    r = client.post(f"/jobs/{jid}/update", data={"topic": "Nyelzya"})
    assert r.status_code == 422


def test_update_job_not_found(client):
    r = client.post("/jobs/999999/update", data={"topic": "Net"})
    assert r.status_code == 404


def test_leave_job_not_found(client):
    r = client.post("/api/jobs/999999/leave")
    assert r.status_code == 404


def test_leave_rejected_for_wrong_status(client):
    jid = client.post("/api/jobs", json={"meeting_id": "321321321"}).json()["id"]
    r = client.post(f"/api/jobs/{jid}/leave")  # scheduled -> ne dolzhno razreshatsya
    assert r.status_code == 409


def test_leave_recording_queues_stop_and_logs_event(client, monkeypatch):
    from mnema.core.db import get_session
    from mnema.core.models import SessionEvent
    stops = []
    monkeypatch.setattr("mnema.core.jobs_launch.request_stop",
                        lambda job_id, reason=None: stops.append((job_id, reason)) or True)
    jid = client.post("/api/jobs", json={"meeting_id": "654654654"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    client.post(f"/api/jobs/{jid}/status", json={"status": "recording"})
    r = client.post(f"/api/jobs/{jid}/leave")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "status": "recording", "stop_queued": True}
    assert stops == [(jid, "manual_leave")]
    with get_session() as s:
        evs = s.scalars(__import__("sqlalchemy").select(SessionEvent)).all()
        assert any(e.event == "leave_requested" for e in evs)


def test_web_leave_endpoint(client, monkeypatch):
    stops = []
    monkeypatch.setattr("mnema.core.jobs_launch.request_stop",
                        lambda job_id, reason=None: stops.append(job_id) or True)
    jid = client.post("/api/jobs", json={"meeting_id": "987987987"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    r = client.post(f"/jobs/{jid}/leave")
    assert r.status_code == 200 and r.json()["stop_queued"] is True
    assert stops == [jid]


def test_health_bad_request_bad_format(client):
    # sozdaem job s audio bez fila -> 404 na razdache
    r = client.get("/api/jobs/999999/audio")
    assert r.status_code == 404


def _make_wav(path: Path, seconds: float = 2.0, sr: int = 16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        frames = struct.pack(f"<{int(sr*seconds)}h", *([100] * int(sr*seconds)))
        w.writeframes(frames)


def test_audio_range_request(tmp_path, monkeypatch):
    from mnema.core.db import Base, set_engine_for_test
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    with TestClient(app) as c:
        # hand-in a job + wav + protocol to hit audio route via job
        jid = c.post("/api/jobs", json={"meeting_id": "700700700"}).json()["id"]
        wavfile = tmp_path / "meeting.wav"
        _make_wav(wavfile)
        from mnema.core.db import get_session
        from mnema.core.models import AudioFile, Job
        with get_session() as s:
            s.add(AudioFile(job_id=jid, kind="mp3", path=str(wavfile), bytes=wavfile.stat().st_size))
            s.commit()
        # plain
        r = c.get(f"/api/jobs/{jid}/audio?format=mp3")
        assert r.status_code == 200
        # range
        r2 = c.get(f"/api/jobs/{jid}/audio?format=mp3", headers={"range": "bytes=0-15"})
        assert r2.status_code == 206
        assert r2.headers["content-range"].startswith("bytes 0-15/")
        assert len(r2.content) == 16


def test_ics_import_creates_jobs(client):
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1@x
DTSTART:20261001T100000Z
DTEND:20261001T110000Z
SUMMARY:Sprint review
DESCRIPTION:Join: https://us05web.zoom.us/j/999888777?pwd=zz9
END:VEVENT
END:VCALENDAR"""
    r = client.post("/jobs/import-ics", files={"file": ("x.ics", ics, "text/calendar")})
    assert r.status_code == 200 and r.json()["imported"] == 1
    jobs = client.get("/api/jobs").json()
    assert any(j["zoom_meeting_id"] == "999888777" for j in jobs)


def test_voice_profile_gate_consent(client):
    # bez soglasiya -> 400
    r = client.post("/api/voice-profiles", json={"employee_name": "X", "consent": False})
    assert r.status_code == 400
    r2 = client.post("/api/voice-profiles", json={"employee_name": "X", "consent": True})
    assert r2.status_code == 200
    vid = r2.json()["id"]
    lst = client.get("/api/voice-profiles").json()
    assert any(v["id"] == vid for v in lst)


def test_web_pages_render(client):
    for path in ["/", "/jobs", "/protocols", "/reviews", "/voices"]:
        r = client.get(path)
        assert r.status_code == 200, path


def test_jobs_page_renamed_and_start_now_option(client):
    r = client.get("/jobs")
    assert "Запланировать встречу" in r.text
    assert "Запланировать задачу" not in r.text
    assert 'name="start_now"' in r.text
    assert 'name="end_at"' in r.text
    assert 'step="300"' in r.text


def test_web_job_create_start_now_ignores_start_at(client):
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    r = client.post("/jobs/create", data={
        "meeting_link": "https://us05web.zoom.us/j/123123123",
        "topic": "Idushchaya", "start_now": "1", "start_at": future},
        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/jobs/")
    jobs = client.get("/api/jobs").json()
    j = next(x for x in jobs if x["zoom_meeting_id"] == "123123123")
    started = dt.datetime.fromisoformat(j["start_at"])
    if started.tzinfo is None:
        started = started.replace(tzinfo=dt.timezone.utc)
    assert abs((started - dt.datetime.now(dt.timezone.utc)).total_seconds()) < 300


def test_web_job_create_schedule_keeps_start_at(client):
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).replace(microsecond=0)
    r = client.post("/jobs/create", data={
        "meeting_link": "https://us05web.zoom.us/j/456456456",
        "topic": "Pozzhe", "start_at": future.isoformat()},
        follow_redirects=False)
    assert r.status_code == 303
    jobs = client.get("/api/jobs").json()
    j = next(x for x in jobs if x["zoom_meeting_id"] == "456456456")
    started = dt.datetime.fromisoformat(j["start_at"]).replace(tzinfo=dt.timezone.utc)
    assert started == future


def test_web_job_create_invalid_link_redirects_with_error(client):
    r = client.post("/jobs/create", data={
        "meeting_link": "https://example.com/not-zoom",
        "topic": "Bad", "start_now": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs?error=link"
    jobs = client.get("/api/jobs").json()
    assert all(x.get("topic") != "Bad" for x in jobs)


def test_web_job_create_start_at_is_moscow_time(client):
    """datetime-local bez offset = vremya operatora (MSC), ne UTC."""
    naive = "2026-10-01T16:00"
    r = client.post("/jobs/create", data={
        "meeting_link": "https://us05web.zoom.us/j/777777771",
        "topic": "MskTime", "start_at": naive}, follow_redirects=False)
    assert r.status_code == 303
    j = next(x for x in client.get("/api/jobs").json()
             if x["zoom_meeting_id"] == "777777771")
    started = dt.datetime.fromisoformat(j["start_at"]).replace(tzinfo=dt.timezone.utc)
    assert started == dt.datetime(2026, 10, 1, 13, 0, tzinfo=dt.timezone.utc)


def test_web_job_create_end_at_is_moscow_time(client):
    naive = "2026-10-01T17:30"
    r = client.post("/jobs/create", data={
        "meeting_link": "https://us05web.zoom.us/j/777777772",
        "topic": "EndMsk", "start_at": "2026-10-01T16:00", "end_at": naive},
        follow_redirects=False)
    assert r.status_code == 303
    j = next(x for x in client.get("/api/jobs").json()
             if x["zoom_meeting_id"] == "777777772")
    ji = client.get(f"/api/jobs/{j['id']}/join-info")
    assert ji.status_code == 200
    end_at = ji.json()["end_at"]
    assert end_at is not None
    end = dt.datetime.fromisoformat(end_at)
    if end.tzinfo is None:
        end = end.replace(tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 10, 1, 14, 30, tzinfo=dt.timezone.utc)


def test_web_job_create_end_before_start_rejected(client):
    r = client.post("/jobs/create", data={
        "meeting_link": "https://us05web.zoom.us/j/777777773",
        "topic": "BadEnd", "start_at": "2026-10-01T17:00", "end_at": "2026-10-01T16:00"},
        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs?error=end_before_start"
    jobs = client.get("/api/jobs").json()
    assert all(x.get("topic") != "BadEnd" for x in jobs)


def test_web_job_create_end_at_without_start_allowed(client):
    r = client.post("/jobs/create", data={
        "meeting_link": "https://us05web.zoom.us/j/777777774",
        "topic": "EndOnly", "end_at": "2026-10-01T16:00"},
        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/jobs/")


def test_api_job_split_fields_and_detail_block(client):
    """payload /api/jobs/{jid}: parent_job_id, child_job_ids, split; страница
    встречи показывает родителя и детей после разделения."""
    from mnema.core.db import get_session
    from mnema.core.models import Job
    r = client.post("/api/jobs", json={"meeting_id": "555000111", "topic": "Двойник"})
    jid = r.json()["id"]
    with get_session() as s:
        parent = s.get(Job, jid)
        parent.split_json = {"boundaries": [{"t": 2760.0, "confidence": 0.9,
                                             "topic_before": "Двойник",
                                             "topic_after": "Хвост"}],
                             "model": "m", "skipped": None, "ts": "2026-10-08"}
        child = Job(source="split", parent_job_id=jid, topic="Хвост",
                    status="processing", created_by="split",
                    split_json={"parent": jid, "boundary_t": 2760.0,
                                "ts": "2026-10-08"})
        s.add(child)
        s.commit()
        cid = child.id
    g = client.get(f"/api/jobs/{jid}").json()
    assert g["parent_job_id"] is None
    assert g["child_job_ids"] == [cid]
    assert g["split"]["boundaries"][0]["t"] == 2760.0
    gc = client.get(f"/api/jobs/{cid}").json()
    assert gc["parent_job_id"] == jid
    page = client.get(f"/jobs/{jid}")
    assert page.status_code == 200
    assert "Запись разделена" in page.text and f"/jobs/{cid}" in page.text
    page_c = client.get(f"/jobs/{cid}")
    assert "Запись разделена" in page_c.text
    assert f"Часть разделённой встречи #{jid}" in page_c.text


def test_web_job_detail_shows_planned_end(client):
    r = client.post("/jobs/create", data={
        "meeting_link": "https://us05web.zoom.us/j/777777775",
        "topic": "ShowEnd", "start_at": "2026-10-01T16:00", "end_at": "2026-10-01T17:30"},
        follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"])
    assert page.status_code == 200
    assert "Плановое окончание (МСК)" in page.text
    assert "17:30" in page.text


def test_web_job_detail_hides_planned_end_when_absent(client):
    r = client.post("/jobs/create", data={
        "meeting_link": "https://us05web.zoom.us/j/777777776",
        "topic": "NoEnd", "start_at": "2026-10-01T16:00"},
        follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"])
    assert page.status_code == 200
    assert "Плановое окончание (МСК)" not in page.text


def test_web_job_create_invalid_link_empty(client):
    r = client.post("/jobs/create", data={"meeting_link": "", "start_now": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs?error=link"


def test_web_jobs_shows_link_error_banner(client):
    r = client.get("/jobs?error=link")
    assert r.status_code == 200
    assert "Некорректная ссылка Zoom" in r.text


def test_web_job_create_plain_meeting_id_accepted(client):
    r = client.post("/jobs/create", data={
        "meeting_link": "123456789", "start_now": "1", "topic": "OnlyId"},
        follow_redirects=False)
    assert r.status_code == 303
    jobs = client.get("/api/jobs").json()
    assert any(x["zoom_meeting_id"] == "123456789" for x in jobs)


def test_reviews_page_with_pending(client):
    """Regres: .limit() na scalars lomali stranicy /reviews pri nepustoich ochenereyah."""
    from mnema.core.db import get_session
    from mnema.core.models import Job, ReviewRequest, Segment
    jid = client.post("/api/jobs", json={"meeting_id": "810810810"}).json()["id"]
    with get_session() as s:
        s.add(ReviewRequest(job_id=jid, cluster_label="SPEAKER_UNKNOWN", similarity=0.51))
        s.add(Segment(job_id=jid, speaker_id="SPEAKER_UNKNOWN", diar_label="SPEAKER_UNKNOWN",
                      t_start=0.0, t_end=2.0, text="test", speaker_source="unresolved",
                      needs_review=True))
        s.commit()
    r = client.get("/reviews")
    assert r.status_code == 200
    assert "SPEAKER_UNKNOWN" in r.text
    api = client.get("/api/reviews?status=pending").json()
    assert api and api[0]["cluster_label"] == "SPEAKER_UNKNOWN"


def test_reviews_page_filtered_by_job(client):
    """Ссылка из встречи ведёт на уточнения только этой встречи."""
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest
    jid1 = client.post("/api/jobs", json={"meeting_id": "111000111"}).json()["id"]
    jid2 = client.post("/api/jobs", json={"meeting_id": "222000222"}).json()["id"]
    with get_session() as s:
        s.add(ReviewRequest(job_id=jid1, cluster_label="CLUSTER_ONE"))
        s.add(ReviewRequest(job_id=jid2, cluster_label="CLUSTER_TWO"))
        s.commit()
    r = client.get(f"/reviews?job_id={jid1}")
    assert r.status_code == 200
    assert "CLUSTER_ONE" in r.text
    assert "CLUSTER_TWO" not in r.text
    allr = client.get("/reviews")
    assert "CLUSTER_ONE" in allr.text and "CLUSTER_TWO" in allr.text


def _make_review_fixture(client):
    """Job v awaiting_review + pending review + segmenty + meeting.wav (3 s)."""
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, Segment
    jid = client.post("/api/jobs", json={"meeting_id": "900900900"}).json()["id"]
    for st in ("launching", "recording", "processing", "awaiting_review"):
        assert client.post(f"/api/jobs/{jid}/status", json={"status": st}).status_code == 200
    import os
    wav_dir = Path(os.environ["DATA_DIR"]) / "jobs" / str(jid)
    wav_dir.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_dir / "meeting.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<51200h", *([100] * 51200)))
    with get_session() as s:
        rid_row = ReviewRequest(job_id=jid, cluster_label="SPEAKER_00", similarity=0.2)
        s.add(rid_row)
        s.add(Segment(job_id=jid, speaker_id="SPEAKER_00", diar_label="SPEAKER_00",
                      t_start=0.0, t_end=3.0, text="привет", speaker_source="unresolved",
                      needs_review=True))
        s.commit()
        return jid, rid_row.id


def test_reviews_profile_dropdown_sorted(client):
    """Сотрудники в выпадающем списке очереди уточнений идут по ФИО."""
    import re
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, VoiceProfile
    jid = client.post("/api/jobs", json={"meeting_id": "700700700"}).json()["id"]
    with get_session() as s:
        s.add(ReviewRequest(job_id=jid, cluster_label="SPEAKER_X"))
        for name in ["Яковлев П. П.", "иванов И. И.", "Сидоров А. Б."]:
            s.add(VoiceProfile(employee_name=name))
        s.commit()
    html = client.get("/reviews").text
    names = re.findall(r'<option value="\d+">([^<]+)</option>', html)
    assert names == ["иванов И. И.", "Сидоров А. Б.", "Яковлев П. П."]
    assert html.index('— создать нового') < html.index('иванов')


def test_review_create_new_makes_voice_profile(client):
    """Regres: 'Создать нового' v /reviews bez flaga create_new ne sozdaval profil'."""
    from sqlalchemy import select
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, Segment
    jid, rid = _make_review_fixture(client)
    r = client.post(f"/api/reviews/{rid}",
                    json={"employee_name": "Иванов И. И.", "create_new": True})
    assert r.status_code == 200
    lst = client.get("/api/voice-profiles").json()
    assert any(p["employee_name"] == "Иванов И. И." for p in lst)
    with get_session() as s:
        rev = s.get(ReviewRequest, rid)
        assert rev.status == "confirmed" and rev.candidate_profile_id is not None
        seg = s.scalars(select(Segment).where(Segment.job_id == jid)).first()
        assert seg.speaker_name == "Иванов И. И."
        assert seg.needs_review is False


def test_review_create_new_embeds_cluster(client):
    """Novyĭ profil' poluchaet embedding klastera, inache on nikogda ne smatsitsya."""
    from sqlalchemy import select
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    jid, rid = _make_review_fixture(client)
    r = client.post(f"/api/reviews/{rid}",
                    json={"employee_name": "Петрова А. П.", "create_new": True})
    assert r.status_code == 200
    with get_session() as s:
        v = s.scalars(select(VoiceProfile).where(
            VoiceProfile.employee_name == "Петрова А. П.")).first()
        assert v is not None
        assert v.embedding, "embedding klastera ne vypolsen"


def test_review_name_only_without_create_new_makes_no_profile(client):
    """Staroe povedenie: bez create_new imenuetsegmty, no profil' v reestr ne popadaet."""
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    from sqlalchemy import select
    before = len(client.get("/api/voice-profiles").json())
    jid, rid = _make_review_fixture(client)
    r = client.post(f"/api/reviews/{rid}", json={"employee_name": "Сидоров В. В."})
    assert r.status_code == 200
    assert len(client.get("/api/voice-profiles").json()) == before
    with get_session() as s:
        assert s.scalars(select(VoiceProfile).where(
            VoiceProfile.employee_name == "Сидоров В. В.")).first() is None


def test_review_create_new_reuses_profile_with_same_name(client):
    """Povtornoe 'Создать novo' s tem zhe FIO ne sozdaet dubl v reestre."""
    jid1, rid1 = _make_review_fixture(client)
    client.post(f"/api/reviews/{rid1}",
                json={"employee_name": "Дубль Д. Д.", "create_new": True})
    cnt1 = sum(1 for p in client.get("/api/voice-profiles").json()
               if p["employee_name"] == "Дубль Д. Д.")
    jid2, rid2 = _make_review_fixture(client)
    client.post(f"/api/reviews/{rid2}",
                json={"employee_name": "Дубль Д. Д.", "create_new": True})
    cnt2 = sum(1 for p in client.get("/api/voice-profiles").json()
               if p["employee_name"] == "Дубль Д. Д.")
    assert cnt1 == 1 and cnt2 == 1


def _make_two_segment_cluster(client):
    """Job v awaiting_review + pending review + dva segmenta klastera."""
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, Segment
    jid = client.post("/api/jobs", json={"meeting_id": "710710710"}).json()["id"]
    for st in ("launching", "recording", "processing", "awaiting_review"):
        client.post(f"/api/jobs/{jid}/status", json={"status": st})
    with get_session() as s:
        r = ReviewRequest(job_id=jid, cluster_label="SPEAKER_00", similarity=0.2)
        s.add(r)
        s.commit()
        rid = r.id
        seg1 = Segment(job_id=jid, speaker_id="SPEAKER_00", diar_label="SPEAKER_00",
                       t_start=0.0, t_end=3.0, text="остался", speaker_source="unresolved",
                       needs_review=True)
        seg2 = Segment(job_id=jid, speaker_id="SPEAKER_00", diar_label="SPEAKER_00",
                       t_start=5.0, t_end=8.0, text="чужой", speaker_source="unresolved",
                       needs_review=True)
        s.add_all([seg1, seg2])
        s.commit()
        return jid, rid, seg1.id, seg2.id


def test_exclude_segment_moves_it_to_new_cluster(client):
    """Удаление фрагмента из кластера: сегмент уходит в отдельный кластер + своя pending заявка."""
    from mnema.core.db import get_session
    from sqlalchemy import select
    from mnema.core.models import Segment, ReviewRequest
    jid, rid, _s1, s2 = _make_two_segment_cluster(client)
    r = client.delete(f"/api/reviews/{rid}/segments/{s2}")
    assert r.status_code == 200
    new_label = r.json()["cluster_label"]
    assert new_label.startswith("SPEAKER_00-x")
    with get_session() as s:
        seg = s.get(Segment, s2)
        assert seg.speaker_id == new_label
        assert seg.needs_review is True and seg.speaker_name is None
        remaining = s.scalars(select(Segment).where(
            Segment.job_id == jid, Segment.speaker_id == "SPEAKER_00")).all()
        assert [x.id for x in remaining] and s2 not in [x.id for x in remaining]
        nr = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == jid,
            ReviewRequest.cluster_label == new_label,
            ReviewRequest.status == "pending")).first()
        assert nr is not None


def test_exclude_segment_answer_applies_only_to_remaining(client):
    """После удаления сегмента ответ по review переименоывает только остальные."""
    from mnema.core.db import get_session
    from sqlalchemy import select
    from mnema.core.models import Segment
    jid, rid, s1, s2 = _make_two_segment_cluster(client)
    assert client.delete(f"/api/reviews/{rid}/segments/{s2}").status_code == 200
    r = client.post(f"/api/reviews/{rid}", json={"employee_name": "Иванов И. И."})
    assert r.status_code == 200
    with get_session() as s:
        seg1 = s.get(Segment, s1)
        seg2 = s.get(Segment, s2)
        assert seg1.speaker_name == "Иванов И. И." and seg1.needs_review is False
        assert seg2.speaker_name is None and seg2.needs_review is True


def test_exclude_segment_not_found_wrong_job_or_cluster(client):
    jid, rid, s1, _s2 = _make_two_segment_cluster(client)
    assert client.delete(f"/api/reviews/{rid}/segments/999999").status_code == 404
    other = jid + 1000
    assert client.delete(f"/api/reviews/{other}/segments/{s1}").status_code == 404


def test_exclude_segment_confirmed_review_rejected(client):
    """Нельзя вырвать фрагмент из уже отвеченного review-request."""
    jid, rid, _s1, s2 = _make_two_segment_cluster(client)
    client.post(f"/api/reviews/{rid}", json={"employee_name": "Orlov O. O."})
    assert client.delete(f"/api/reviews/{rid}/segments/{s2}").status_code == 409


def test_exclude_segment_twice_creates_one_duplicate_request(client):
    """Повторное удаление фрагмента из уже выделенного кластера не создаёт second дублей заявок."""
    from mnema.core.db import get_session
    from sqlalchemy import func, select
    from mnema.core.models import ReviewRequest
    jid, rid, _s1, s2 = _make_two_segment_cluster(client)
    first = client.delete(f"/api/reviews/{rid}/segments/{s2}").json()["cluster_label"]
    new = client.get("/api/reviews?status=pending").json()
    rid2 = next(x["id"] for x in new if x["cluster_label"] == first)
    r = client.delete(f"/api/reviews/{rid2}/segments/{s2}")
    assert r.status_code == 200
    new_label = r.json()["cluster_label"]
    assert r.json()["review_closed"] is True
    with get_session() as s:
        cnt = s.scalar(select(func.count()).select_from(ReviewRequest).where(
            ReviewRequest.job_id == jid,
            ReviewRequest.cluster_label == first,
            ReviewRequest.status == "pending"))
        assert cnt == 0
        pending_new = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == jid,
            ReviewRequest.cluster_label == new_label,
            ReviewRequest.status == "pending")).all()
        assert len(pending_new) == 1


def test_reviews_page_shows_all_cluster_segments(client):
    """Regres: v /reviews klaster rezalsya po limit(5) bez poryadka — posle
    udaleniya fragmenta vniz vyskakivali raneye skrytye fragmenty."""
    from mnema.core.db import get_session
    from sqlalchemy import select
    from mnema.core.models import ReviewRequest, Segment
    jid = client.post("/api/jobs", json={"meeting_id": "620620620"}).json()["id"]
    with get_session() as s:
        s.add(ReviewRequest(job_id=jid, cluster_label="SPEAKER_00", similarity=0.2))
        for i in range(7):
            s.add(Segment(job_id=jid, speaker_id="SPEAKER_00", diar_label="SPEAKER_00",
                          t_start=float(i * 10), t_end=float(i * 10 + 5),
                          text=f"frag{i}", speaker_source="unresolved",
                          needs_review=True))
        s.commit()
        ids = {seg.text: seg.id for seg in s.scalars(select(Segment).where(
            Segment.job_id == jid)).all()}
    page = client.get("/reviews").text
    for text in ids:
        assert text in page
    rid = client.get("/api/reviews?status=pending").json()[0]["id"]
    assert client.delete(f"/api/reviews/{rid}/segments/{ids['frag2']}").status_code == 200
    page2 = client.get("/reviews").text
    for text in [t for t in ids if t != "frag2"]:
        assert text in page2
    assert page2.count("frag2") == 1


def test_delete_last_segment_closes_empty_review(client, monkeypatch):
    """Regres: udalenie edinstvennogo fragmenta zakryvaet pustuyu zayavku
    (inache zayavka bez etalonnyh fragmentov naveschno visit v ocheredi)."""
    from mnema.core.db import get_session
    from sqlalchemy import select
    from mnema.core.models import Job, ReviewRequest
    monkeypatch.setattr("mnema.core.queue.enqueue_job", lambda *a, **k: None)
    jid, rid, sid = _make_single_segment_cluster(client)
    r = client.delete(f"/api/reviews/{rid}/segments/{sid}")
    assert r.status_code == 200
    assert r.json()["review_closed"] is True
    with get_session() as s:
        assert s.get(ReviewRequest, rid).status == "superseded"
        # fragmentysh v novom podklaster — zhov zhdyot utochneniya po nemu
        assert s.get(Job, jid).status == "awaiting_review"
        assert len(s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == jid,
            ReviewRequest.status == "pending")).all()) == 1


def _make_single_segment_cluster(client):
    """Job v awaiting_review + pending review + odin segment (posle exclusions)."""
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, Segment
    jid = client.post("/api/jobs", json={"meeting_id": "720720720"}).json()["id"]
    for st in ("launching", "recording", "processing", "awaiting_review"):
        client.post(f"/api/jobs/{jid}/status", json={"status": st})
    with get_session() as s:
        r = ReviewRequest(job_id=jid, cluster_label="SPEAKER_01", similarity=0.1)
        s.add(r)
        s.commit()
        seg = Segment(job_id=jid, speaker_id="SPEAKER_01", diar_label="SPEAKER_01",
                      t_start=0.0, t_end=3.0, text="dva golosa",
                      speaker_source="unresolved", needs_review=True)
        s.add(seg)
        s.commit()
        return jid, r.id, seg.id


def test_exclude_from_analysis_marks_segment(client):
    """POST .../exclude: segment excluded, needs_review snyat, klaster zhiv."""
    from mnema.core.db import get_session
    from mnema.core.models import Segment
    jid, rid, _s1, s2 = _make_two_segment_cluster(client)
    r = client.post(f"/api/reviews/{rid}/segments/{s2}/exclude")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "cluster_closed": False, "job_done": False}
    with get_session() as s:
        seg = s.get(Segment, s2)
        assert seg.excluded is True
        assert seg.needs_review is False
        assert seg.speaker_source == "excluded"


def test_exclude_from_analysis_closes_last_review_and_resumes(client, monkeypatch):
    """Posledniy fragment klastera iskluchen — zakryvaet review i vozobnovlyaet LLM."""
    from mnema.core.db import get_session
    from mnema.core.models import Job, ReviewRequest, Segment
    calls = []
    monkeypatch.setattr("mnema.core.queue.enqueue_job",
                        lambda *a, **k: calls.append(a))
    jid, rid, sid = _make_single_segment_cluster(client)
    r = client.post(f"/api/reviews/{rid}/segments/{sid}/exclude")
    assert r.status_code == 200
    assert r.json()["cluster_closed"] is True and r.json()["job_done"] is True
    with get_session() as s:
        assert s.get(Segment, sid).excluded is True
        rev = s.get(ReviewRequest, rid)
        assert rev.status == "excluded"
        assert s.get(Job, jid).status == "processing"
    assert ("structure", "mnema.worker.tasks.run_structure", jid) in calls


def test_exclude_from_analysis_not_found_and_confirmed(client):
    jid, rid, s1, _s2 = _make_two_segment_cluster(client)
    assert client.post(f"/api/reviews/{rid}/segments/999999/exclude").status_code == 404
    client.post(f"/api/reviews/{rid}", json={"employee_name": "Petrov P. P."})
    assert client.post(f"/api/reviews/{rid}/segments/{s1}/exclude").status_code == 409


def test_excluded_segments_skip_steno_and_protocol(client):
    """Isklyuchennyy segment ne popadaet v steno.md i protokol'nyy transkript."""
    from mnema.core.db import get_session
    from mnema.core.models import Segment
    from sqlalchemy import select
    from mnema.worker import tasks
    jid, rid, _s1, s2 = _make_two_segment_cluster(client)
    with get_session() as s:
        s.get(Segment, s2).text = "чужой"
        s.commit()
    client.post(f"/api/reviews/{rid}/segments/{s2}/exclude")
    p = tasks._job_paths(jid)
    tasks._write_steno(jid, p)
    steno = p["steno"].read_text(encoding="utf-8")
    assert "остался" in steno and "чужой" not in steno
    with get_session() as s:
        segs = s.scalars(select(Segment).where(
            Segment.job_id == jid, Segment.excluded.is_(False)
        ).order_by(Segment.t_start)).all()
    lines = tasks._transcript_lines(segs)
    assert "чужой" not in lines and "остался" in lines


# ---- mix-voices: 'mikss dvukh golosov' vmesto raspoznavaniya ----------------

def test_mix_segment_marks_voice_mix_keeps_cluster_pending(client):
    """POST .../mix: speaker_source=voice_mix, needs_review snyat, segment ne
    excluded; v klastere eshchyo est' utochnyaemyy fragment — review zhiv."""
    from mnema.core.db import get_session
    from mnema.core.models import Segment
    jid, rid, _s1, s2 = _make_two_segment_cluster(client)
    r = client.post(f"/api/reviews/{rid}/segments/{s2}/mix")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "cluster_closed": False, "job_done": False}
    with get_session() as s:
        seg = s.get(Segment, s2)
        assert seg.speaker_source == "voice_mix"
        assert seg.needs_review is False
        assert seg.excluded is False


def test_mix_segment_clears_hint_name(client):
    """Mix fragmentsa s hint-imenem: imya snyato — pod nim dva cheloveka."""
    from mnema.core.db import get_session
    from mnema.core.models import Segment
    jid, rid, _s1, s2 = _make_two_segment_cluster(client)
    with get_session() as s:
        seg = s.get(Segment, s2)
        seg.speaker_name = "Sidorov S. S."  # kak posle verdicta hint
        s.commit()
    assert client.post(f"/api/reviews/{rid}/segments/{s2}/mix").status_code == 200
    with get_session() as s:
        seg = s.get(Segment, s2)
        assert seg.speaker_source == "voice_mix"
        assert seg.speaker_name is None


def test_mix_last_pending_segment_closes_review_as_mixed_and_resumes(client,
                                                                     monkeypatch):
    """Posledniy utochnyaemyy fragment pomечён mikssom — review zakryvaetsya
    (status=mixed), analiz vozobnovlyaetsya."""
    from mnema.core.db import get_session
    from mnema.core.models import Job, ReviewRequest, Segment
    calls = []
    monkeypatch.setattr("mnema.core.queue.enqueue_job",
                        lambda *a, **k: calls.append(a))
    jid, rid, sid = _make_single_segment_cluster(client)
    r = client.post(f"/api/reviews/{rid}/segments/{sid}/mix")
    assert r.status_code == 200
    assert r.json()["cluster_closed"] is True and r.json()["job_done"] is True
    with get_session() as s:
        seg = s.get(Segment, sid)
        assert seg.speaker_source == "voice_mix" and seg.needs_review is False
        assert s.get(ReviewRequest, rid).status == "mixed"
        assert s.get(Job, jid).status == "processing"
    assert ("structure", "mnema.worker.tasks.run_structure", jid) in calls


def test_mix_segment_not_found_and_confirmed(client):
    jid, rid, s1, _s2 = _make_two_segment_cluster(client)
    assert client.post(f"/api/reviews/{rid}/segments/999999/mix").status_code == 404
    client.post(f"/api/reviews/{rid}", json={"employee_name": "Sidorov S. S."})
    assert client.post(f"/api/reviews/{rid}/segments/{s1}/mix").status_code == 409


def test_mix_excluded_segment_rejected(client):
    jid, rid, s1, _s2 = _make_two_segment_cluster(client)
    client.post(f"/api/reviews/{rid}/segments/{s1}/exclude")
    assert client.post(f"/api/reviews/{rid}/segments/{s1}/mix").status_code == 409


def test_mixed_segment_stays_in_steno_with_two_voices_tag(client):
    """Mikss-ne-golos ostyotsya v stenograme i vhode LLM s pometskoy dvukh
    golosov (v otlichie ot excluded)."""
    from mnema.core.db import get_session
    from mnema.core.models import Segment
    from sqlalchemy import select
    from mnema.worker import tasks
    jid, rid, _s1, s2 = _make_two_segment_cluster(client)
    client.post(f"/api/reviews/{rid}/segments/{s2}/mix")
    p = tasks._job_paths(jid)
    tasks._write_steno(jid, p)
    steno = p["steno"].read_text(encoding="utf-8")
    assert "чужой" in steno and "dva golosa v fragmente" in steno
    with get_session() as s:
        segs = s.scalars(select(Segment).where(
            Segment.job_id == jid, Segment.excluded.is_(False)
        ).order_by(Segment.t_start)).all()
    lines = tasks._transcript_lines(segs)
    assert "[два голоса]" in lines and "чужой" in lines


def _to_status(client, jid, chain):
    for st in chain:
        assert client.post(f"/api/jobs/{jid}/status",
                           json={"status": st}).status_code == 200


def test_rerun_llm_from_done_requeues_analysis(client, monkeypatch):
    """done -> rerun-llm: zhov vozvraschaetsya v processing i stavitsya run_structure."""
    from mnema.core.db import get_session
    from mnema.core.models import SessionEvent
    enq = []
    monkeypatch.setattr("mnema.core.queue.enqueue_job",
                        lambda name, func, *a, **kw: enq.append((name, func, a)) or object())
    jid = client.post("/api/jobs", json={"meeting_id": "111222333"}).json()["id"]
    _to_status(client, jid, ["launching", "recording", "processing", "done"])
    r = client.post(f"/jobs/{jid}/rerun-llm")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "status": "processing"}
    assert enq == [("structure", "mnema.worker.tasks.run_structure", (jid,))]
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "processing"
    with get_session() as s:
        evs = s.scalars(__import__("sqlalchemy").select(SessionEvent)).all()
        assert any(e.event == "llm_rerun" for e in evs)


def test_rerun_llm_from_awaiting_llm(client, monkeypatch):
    enq = []
    monkeypatch.setattr("mnema.core.queue.enqueue_job",
                        lambda name, func, *a, **kw: enq.append((name, func, a)) or object())
    jid = client.post("/api/jobs", json={"meeting_id": "222333445"}).json()["id"]
    _to_status(client, jid, ["launching", "recording", "processing", "awaiting_llm"])
    r = client.post(f"/jobs/{jid}/rerun-llm")
    assert r.status_code == 200
    assert enq and enq[0][0] == "structure"


def test_rerun_llm_rejected_wrong_status(client):
    jid = client.post("/api/jobs", json={"meeting_id": "333444555"}).json()["id"]
    r = client.post(f"/jobs/{jid}/rerun-llm")  # scheduled
    assert r.status_code == 409


def test_rerun_llm_not_found(client):
    assert client.post("/jobs/999999/rerun-llm").status_code == 404


def test_done_detail_page_shows_rerun_button(client):
    jid = client.post("/api/jobs", json={"meeting_id": "444555666"}).json()["id"]
    _to_status(client, jid, ["launching", "recording", "processing", "done"])
    r = client.get(f"/jobs/{jid}")
    assert "Перезапустить анализ" in r.text
    assert "rerun-llm" in r.text


def test_running_detail_page_has_no_rerun_button(client):
    jid = client.post("/api/jobs", json={"meeting_id": "555666778"}).json()["id"]
    r = client.get(f"/jobs/{jid}")
    assert "Перезапустить анализ" not in r.text


# ---- protocol detail: source_ts + fresh names in export (FF: markdown fix) --

def _make_done_protocol(client, meeting_id):
    """Job done + Protocol s zaranee zapisannym md (kak iz _build_protocol):
    v faile 'Golos SPEAKER_00', v BDB imena utochneny."""
    from mnema.core.db import get_session
    from mnema.core.models import Job, Protocol, Segment
    jid = client.post("/api/jobs", json={"meeting_id": meeting_id,
                                         "topic": "Soveshchanie"}).json()["id"]
    _to_status(client, jid, ["launching", "recording", "processing", "done"])
    with get_session() as s:
        job = s.get(Job, jid)
        s.add_all([
            Segment(job_id=jid, speaker_id="SPEAKER_00", diar_label="SPEAKER_00",
                    t_start=125.0, t_end=130.0, text="pervaya replika",
                    speaker_name="Ivanov I. I.", speaker_source="voiceprint"),
            Segment(job_id=jid, speaker_id="SPEAKER_00", diar_label="SPEAKER_00",
                    t_start=3725.0, t_end=3730.0, text="vtoraya replika",
                    speaker_name="Ivanov I. I.", speaker_source="voiceprint"),
        ])
        proto = Protocol(job_id=jid, json_data={
            "topic": "Soveshchanie",
            "action_items": [{"task": "Sdelat X", "owner": "Ivanov I. I.",
                              "due": "piatnica", "source_ts": "01:02:05"}],
        })
        s.add(proto)
        s.flush()
        pid = proto.id
        md_path = Path(tempfile.mkdtemp()) / "protocol.md"
        job_dir = str(md_path)
        proto.md_path = job_dir
        s.commit()
    md_path.write_text("# Протокол: Soveshchanie\n\n## Стенограмма\n\n"
                       "`00:02:05` **Голос SPEAKER_00**: pervaya replika\n",
                       encoding="utf-8")
    return jid, pid, md_path


def test_protocol_md_download_refreshes_speaker_names(client):
    _jid, pid, md_path = _make_done_protocol(client, "810810810")
    r = client.get(f"/api/protocols/{pid}?format=md")
    assert r.status_code == 200
    assert "Ivanov I. I." in r.text
    assert "Голос SPEAKER_00" not in r.text
    # fail na diske toze perezapisan
    assert "Ivanov I. I." in md_path.read_text(encoding="utf-8")


def test_protocol_md_keeps_source_ts_with_names(client):
    _jid, pid, _md = _make_done_protocol(client, "820820820")
    text = client.get(f"/api/protocols/{pid}?format=md").text
    assert "01:02:05" in text  # Source kolonka sokhranena


def test_patch_segments_refreshes_protocol_md(client):
    from mnema.core.db import get_session
    from mnema.core.models import Segment
    jid, pid, md_path = _make_done_protocol(client, "830830830")
    with get_session() as s:
        seg = s.scalars(__import__("sqlalchemy").select(Segment).where(
            Segment.job_id == jid)).first()
        sid = seg.id
    r = client.patch(f"/api/protocols/{pid}/segments",
                     json={"segments": [{"id": sid, "speaker_name": "Petrov P. P."}]})
    assert r.status_code == 200 and r.json()["updated"] >= 1
    txt = md_path.read_text(encoding="utf-8")
    assert "Petrov P. P." in txt
    assert "Голос SPEAKER_00" not in txt


def test_protocol_detail_page_has_source_links_and_playat(client):
    _jid, pid, _md = _make_done_protocol(client, "840840841")
    r = client.get(f"/protocols/{pid}")
    assert r.status_code == 200
    assert "Основание" in r.text          # kolonka Source v tablice zadach
    assert "playAt(1,2,5)" in r.text     # ssylka source_ts 01:02:05
    assert "playAt(0,2,5)" in r.text     # ssylka na segment 125 s
    assert "playAt(1,2,5)" in r.text and "pervaya replika" in r.text


def test_protocol_detail_page_bad_source_ts_not_a_link(client):
    from mnema.core.db import get_session
    from mnema.core.models import Protocol
    _jid, pid, _md = _make_done_protocol(client, "850850850")
    with get_session() as s:
        p = s.get(Protocol, pid)
        data = dict(p.json_data)
        data["action_items"] = [{"task": "Y", "source_ts": "??"}]
        p.json_data = data
        s.commit()
    r = client.get(f"/protocols/{pid}")
    assert "??</a>" not in r.text  # ne validnoe vremya — ne ssylka


def test_protocol_download_missing_still_404(client):
    assert client.get("/api/protocols/999999?format=md").status_code == 404


# ---- koalescencia: bez pustyh reperok i povtorov odnogo golosa -----------

def _make_messy_protocol(client, meeting_id):
    """Protocol s artefaktami: pustye segmenty + podryad idushchie 'Golos 2'."""
    from mnema.core.db import get_session
    from mnema.core.models import Protocol, Segment
    jid = client.post("/api/jobs", json={"meeting_id": meeting_id,
                                         "topic": "Letuchka"}).json()["id"]
    _to_status(client, jid, ["launching", "recording", "processing", "done"])
    with get_session() as s:
        s.add(Segment(job_id=jid, speaker_id="SPEAKER_01",
                      t_start=0.0, t_end=10.0, text="slovo"))
        s.add(Segment(job_id=jid, speaker_id="SPEAKER_01", t_start=10.0, t_end=20.0,
                      text=""))  # ASR ne rasponal — pustaya reperika
        s.add(Segment(job_id=jid, speaker_id="SPEAKER_01", t_start=20.0, t_end=30.0,
                      text="eshcho slovo"))
        s.add(Segment(job_id=jid, speaker_id="SPEAKER_02", t_start=30.0, t_end=40.0,
                      text="replika drugogo"))
        proto = Protocol(job_id=jid, json_data={"topic": "Letuchka"})
        s.add(proto)
        s.flush()
        pid = proto.id
        md_path = Path(tempfile.mkdtemp()) / "protocol.md"
        proto.md_path = str(md_path)
        s.commit()
    return jid, pid


def test_protocol_md_drops_empty_and_merges_same_speaker(client):
    _jid, pid = _make_messy_protocol(client, "860860860")
    text = client.get(f"/api/protocols/{pid}?format=md").text
    assert text.count("Голос SPEAKER_01") == 1  # bylo 2 nenulye + 1 pustaya
    assert "slovo eshcho slovo" in text         # skleena odna reperika
    assert "replika drugogo" in text


def test_protocol_page_drops_empty_and_merges_same_speaker(client):
    _jid, pid = _make_messy_protocol(client, "870870870")
    r = client.get(f"/protocols/{pid}")
    assert r.status_code == 200
    assert r.text.count("Голос SPEAKER_01") == 1
    assert "slovo eshcho slovo" in r.text


# ---- multi_voice: 'dva golosa' vmesto 'nuzhno utochnenie' ------------------

def _make_two_voices_protocol(client, meeting_id):
    """Protocol s podtverzhdannym klasterom + multi_voice fragment (kak job 31:
    otvet operatora est', no fragment s nalozheniem dvuh golosov ostayotsya)."""
    from mnema.core.db import get_session
    from mnema.core.models import Protocol, Segment
    jid = client.post("/api/jobs", json={"meeting_id": meeting_id,
                                         "topic": "Planerka"}).json()["id"]
    _to_status(client, jid, ["launching", "recording", "processing", "done"])
    with get_session() as s:
        s.add(Segment(job_id=jid, speaker_id="SPEAKER_01", diar_label="SPEAKER_01",
                      t_start=0.0, t_end=10.0, text="fragment s dvumya golosami",
                      speaker_source="multi_voice", needs_review=True))
        proto = Protocol(job_id=jid, json_data={"topic": "Planerka"})
        s.add(proto)
        s.flush()
        pid = proto.id
        md_path = Path(tempfile.mkdtemp()) / "protocol.md"
        proto.md_path = str(md_path)
        s.commit()
    return jid, pid


def test_protocol_page_two_voices_badge_not_needs_review(client):
    _jid, pid = _make_two_voices_protocol(client, "880880880")
    r = client.get(f"/protocols/{pid}")
    assert r.status_code == 200
    assert "Два голоса в фрагменте" in r.text
    assert "Нужно уточнение" not in r.text


def test_protocol_md_two_voices_flag_not_trebuetsya_utochneniya(client):
    _jid, pid = _make_two_voices_protocol(client, "890890890")
    text = client.get(f"/api/protocols/{pid}?format=md").text
    assert "[два голоса]" in text
    assert "[треб.уточнения]" not in text


def _touch(path):
    Path(path).write_bytes(b"\x1a\x45\xdf\xa3" + b"webm-fake" * 10)
    return str(path)


def test_finished_idempotent_no_duplicate_rows(client, tmp_path):
    """Povtorny POST /finished (retry po seti) ne dadet 409 i ne udvoit
    audio_files: vtoryj otvet already=true, storok po kazhdomu kind — odna."""
    jid = client.post("/api/jobs", json={"meeting_id": "551155111"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    client.post(f"/api/jobs/{jid}/status", json={"status": "recording"})
    webm = _touch(tmp_path / "raw.webm")
    wav = _touch(tmp_path / "meeting.wav")
    body = {"webm_path": webm, "wav_path": wav, "duration_s": 12.5, "sizes": {}}
    assert client.post(f"/api/jobs/{jid}/finished", json=body).status_code == 200
    r2 = client.post(f"/api/jobs/{jid}/finished", json=body)
    assert r2.status_code == 200 and r2.json().get("already") is True
    from mnema.core.db import get_session
    from mnema.core.models import AudioFile
    from sqlalchemy import select
    with get_session() as s:
        rows = s.scalars(select(AudioFile).where(AudioFile.job_id == jid)).all()
        assert sorted(r.kind for r in rows) == ["wav", "webm"]


def test_finished_posle_failed_vozvrashchaet_v_processing(client, tmp_path):
    """Salvage upavshej zdachi: /finished iz failed -> processing +
    sobytie recording_recovered (dannye na diske — ASR prohodim)."""
    jid = client.post("/api/jobs", json={"meeting_id": "552255222"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    client.post(f"/api/jobs/{jid}/status", json={"status": "recording"})
    client.post(f"/api/jobs/{jid}/status",
                json={"status": "failed", "fail_reason": "recording_lost"})
    webm = _touch(tmp_path / "raw2.webm")
    r = client.post(f"/api/jobs/{jid}/finished",
                    json={"webm_path": webm, "duration_s": 30.0, "sizes": {}})
    assert r.status_code == 200
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "processing"
    events = [e["event"] for e in client.get(f"/api/jobs/{jid}").json()["events"]]
    assert "recording_recovered" in events


def test_finished_done_status_ne_vozvraschaet_v_processing(client, tmp_path):
    """Iz done (ne failed) — prosto already:true, bez transition."""
    jid = client.post("/api/jobs", json={"meeting_id": "553355333"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    client.post(f"/api/jobs/{jid}/status", json={"status": "recording"})
    webm = _touch(tmp_path / "raw3.webm")
    client.post(f"/api/jobs/{jid}/finished",
                json={"webm_path": webm, "duration_s": 10.0, "sizes": {}})
    client.post(f"/api/jobs/{jid}/status", json={"status": "done"})
    r = client.post(f"/api/jobs/{jid}/finished",
                    json={"webm_path": webm, "duration_s": 10.0, "sizes": {}})
    assert r.status_code == 200 and r.json().get("already") is True
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "done"
