"""Testy VNC-sessiy (kontsept "ekran nastroyki + ekrany vstrech").

Redis niedostupen — sostoaniya chitayutsya cherez monkeypatch (`_client`);
komandy proverayutsya po popadaniju v redis-list.
"""
import json
import os

import pytest
from sqlalchemy import StaticPool, create_engine

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive")
os.environ.setdefault("DATA_DIR", "/tmp/test-data")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-vnc.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # zavedomo niet
os.environ["LLM_BASE_URL"] = ""
os.environ["ASR_BASE_URL"] = ""

from mnema.core import vnc as vnc_svc  # noqa: E402

VNC_HASH = vnc_svc.VNC_HASH
VNC_QUEUE = vnc_svc.VNC_QUEUE


class FakeRedis:
    def __init__(self, data=None):
        self.hashes = data or {}
        self.pushed = []

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def lpush(self, key, value):
        self.pushed.append((key, value))
        return 1


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    # v etih testah redis vsegda "niedostupen" (na mashine mozhet zhit 6399)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.delenv("VNC_PUBLIC_PORT_BASE", raising=False)
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    with TestClient(app) as c:
        yield c


def _entry(kind="job", job_id=None, slot=1, status="running", profile=None,
           public_port="omit"):
    d = {"kind": kind, "job_id": job_id, "slot": slot, "display": f":{99 + slot}",
         "ws_port": 6080 + slot, "profile": profile or f"/data/zoom-profiles/job-{job_id}",
         "status": status, "ts": "2026-09-23T00:00:00Z", "error": None}
    if public_port != "omit":
        d["public_port"] = public_port
    return json.dumps(d)


# ------------------------------------------------------------------ vnc core
def test_public_port_base_default(monkeypatch):
    monkeypatch.delenv("VNC_PUBLIC_PORT_BASE", raising=False)
    assert vnc_svc.public_port_base() == 8080


def test_public_port_base_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("VNC_PUBLIC_PORT_BASE", "ne-chislo")
    assert vnc_svc.public_port_base() == 8080


def _raise_conn_error(*a, **kw):
    raise ConnectionError("redis niedostupen (test)")


def test_read_sessions_empty_when_redis_down(monkeypatch):
    monkeypatch.setattr(vnc_svc, "_client", _raise_conn_error)
    assert vnc_svc.read_sessions() == {}


def test_get_for_job_returns_none_when_redis_down(monkeypatch):
    monkeypatch.setattr(vnc_svc, "_client", _raise_conn_error)
    assert vnc_svc.get_for_job(123) is None


def test_send_command_false_when_redis_down(monkeypatch):
    monkeypatch.setattr(vnc_svc, "_client", _raise_conn_error)
    assert vnc_svc.send_command("setup_open") is False


def test_read_sessions_with_fake_redis_adds_public_port(monkeypatch):
    fake = FakeRedis({VNC_HASH: {
        "setup": _entry(kind="setup", slot=0).encode(),  # bayty, kak iz redis-py
        "job-7": _entry(job_id=7, slot=3),
    }})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    sessions = vnc_svc.read_sessions()
    assert set(sessions) == {"setup", "job-7"}
    assert sessions["setup"]["public_port"] == 8080
    assert sessions["job-7"]["public_port"] == 8083
    assert sessions["job-7"]["status"] == "running"


def test_read_sessions_public_port_respects_env(monkeypatch):
    monkeypatch.setenv("VNC_PUBLIC_PORT_BASE", "9000")
    fake = FakeRedis({VNC_HASH: {"job-1": _entry(job_id=1, slot=2)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    assert vnc_svc.get_for_job(1)["public_port"] == 9002


def test_read_sessions_trusts_public_port_from_hash(monkeypatch):
    # istochnik istiny js: esli public_port est v hash — NE perezapisyvaem svoim env
    monkeypatch.setenv("VNC_PUBLIC_PORT_BASE", "9000")
    fake = FakeRedis({VNC_HASH: {"job-1": _entry(job_id=1, slot=2, public_port=8082)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    assert vnc_svc.get_for_job(1)["public_port"] == 8082


def test_read_sessions_skips_corrupted_entries(monkeypatch):
    fake = FakeRedis({VNC_HASH: {"job-1": "{not json", "job-2": _entry(job_id=2)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    assert set(vnc_svc.read_sessions()) == {"job-2"}


def test_send_command_pushes_payload_to_queue(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    assert vnc_svc.request_setup_open() is True
    assert vnc_svc.request_open(42) is True
    assert vnc_svc.request_close(42) is True
    assert fake.pushed[0] == (VNC_QUEUE, json.dumps({"action": "setup_open"}))
    assert json.loads(fake.pushed[1][1]) == {"action": "open", "job_id": 42}
    assert json.loads(fake.pushed[2][1]) == {"action": "close", "job_id": 42}


# ---------------------------------------------------------------------- API
def test_api_vnc_sessions_empty_without_redis(client):
    r = client.get("/api/vnc/sessions")
    assert r.status_code == 200
    assert r.json() == {}


def test_api_setup_open_no_redis_no_url(client, monkeypatch):
    # menedzher niedostupen — ssylku na myortvyy pot UI ne otdaёm
    monkeypatch.setattr(vnc_svc, "_client", _raise_conn_error)
    r = client.post("/api/vnc/setup/open")
    assert r.status_code == 200
    j = r.json()
    assert j["queued"] is False and j["session"] is None
    assert j["url"] is None


def test_api_setup_open_running_session_returns_its_url(client, monkeypatch):
    fake = FakeRedis({VNC_HASH: {"setup": _entry(kind="setup", slot=0)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    j = client.post("/api/vnc/setup/open").json()
    assert j["session"]["status"] == "running"
    assert j["url"].endswith(":8080/vnc.html")


def test_api_setup_restart_queues_command(client, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    r = client.post("/api/vnc/setup/restart")
    assert r.status_code == 200
    assert json.loads(fake.pushed[0][1]) == {"action": "setup_restart"}


def test_api_job_vnc_unknown_job_404(client):
    assert client.post("/api/jobs/999999/vnc").status_code == 404


def test_api_job_vnc_upload_job_409(client):
    r = client.post("/api/jobs", json={
        "meeting_link": "https://us02web.zoom.us/j/9814832357?pwd=abc",
        "topic": "T"})
    jid = r.json()["id"]
    from mnema.core.db import get_session
    from mnema.core.models import Job
    with get_session() as s:
        s.get(Job, jid).source = "upload"
        s.commit()
    assert client.post(f"/api/jobs/{jid}/vnc").status_code == 409


def test_api_job_vnc_creates_when_absent(client, monkeypatch):
    jid = client.post("/api/jobs", json={
        "meeting_id": "123456789", "topic": "T"}).json()["id"]
    fake = FakeRedis()
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    r = client.post(f"/api/jobs/{jid}/vnc")
    assert r.status_code == 202
    assert r.json()["queued"] is True
    assert json.loads(fake.pushed[0][1]) == {"action": "open", "job_id": jid}
    # sobytie v zhurnale vstrechi
    events = client.get(f"/api/jobs/{jid}").json()["events"]
    assert any(e["event"] == "vnc_requested" for e in events)


def test_api_job_vnc_returns_existing_session_url(client, monkeypatch):
    jid = client.post("/api/jobs", json={
        "meeting_id": "123456789", "topic": "T"}).json()["id"]
    fake = FakeRedis({VNC_HASH: {f"job-{jid}": _entry(job_id=jid, slot=5)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    r = client.post(f"/api/jobs/{jid}/vnc")
    assert r.status_code == 200
    j = r.json()
    assert j["queued"] is False and j["session"]["slot"] == 5
    assert j["url"].endswith(f":8085/vnc.html")
    assert fake.pushed == []  # zhivuyu sessiyu nie perezapushchayut


def test_api_job_vnc_failed_session_requeues_without_url(client, monkeypatch):
    # W7: na myortvyy pot ssylku nie otdaёm, no stavit' komandu peresozdaniya -\
    # da (202), chto by UI nie otkryval pustyye vklady
    jid = client.post("/api/jobs", json={
        "meeting_id": "123456789", "topic": "T"}).json()["id"]
    fake = FakeRedis({VNC_HASH: {f"job-{jid}": _entry(
        job_id=jid, slot=1, status="failed")}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    r = client.post(f"/api/jobs/{jid}/vnc")
    assert r.status_code == 202
    j = r.json()
    assert j["url"] is None and j["queued"] is True
    assert json.loads(fake.pushed[0][1]) == {"action": "open", "job_id": jid}


def test_api_job_vnc_close_queues(client, monkeypatch):
    jid = client.post("/api/jobs", json={
        "meeting_id": "123456789", "topic": "T"}).json()["id"]
    fake = FakeRedis()
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    r = client.delete(f"/api/jobs/{jid}/vnc")
    assert r.status_code == 200
    assert json.loads(fake.pushed[0][1]) == {"action": "close", "job_id": jid}


def test_web_job_vnc_proxy(client, monkeypatch):
    jid = client.post("/api/jobs", json={
        "meeting_id": "123456789", "topic": "T"}).json()["id"]
    fake = FakeRedis()
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    r = client.post(f"/jobs/{jid}/vnc")
    assert r.status_code in (200, 202)
    assert r.json()["queued"] is True


def test_dashboard_shows_setup_block(client, monkeypatch):
    fake = FakeRedis({VNC_HASH: {"setup": _entry(kind="setup", slot=0)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    html = client.get("/").text
    assert "VNC: экран настройки" in html
    assert "Активна" in html
    assert ":8080/vnc.html" in html


def test_dashboard_setup_inactive_shows_create_button(client):
    html = client.get("/").text
    assert "Создать VNC-сессию настройки" in html


def test_dashboard_active_jobs_have_vnc_column(client, monkeypatch):
    jid = client.post("/api/jobs", json={
        "meeting_id": "123456789", "topic": "Planerka"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    fake = FakeRedis({VNC_HASH: {f"job-{jid}": _entry(job_id=jid, slot=2)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    html = client.get("/").text
    assert "/jobs" in html
    assert ":8082/vnc.html" in html


def test_job_detail_shows_screen_for_active_job(client):
    jid = client.post("/api/jobs", json={
        "meeting_id": "123456789", "topic": "T"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    html = client.get(f"/jobs/{jid}").text
    assert "Экран встречи (VNC)" in html
    assert "Создать/открыть экран встречи" in html


def test_job_detail_no_vnc_for_upload(client):
    jid = client.post("/api/jobs", json={
        "meeting_link": "https://us02web.zoom.us/j/9814832357?pwd=abc",
        "topic": "T"}).json()["id"]
    from mnema.core.db import get_session
    from mnema.core.models import Job
    with get_session() as s:
        j = s.get(Job, jid)
        j.source = "upload"
        s.commit()
    html = client.get(f"/jobs/{jid}").text
    assert "Экран встречи (VNC)" not in html


# ----------------------------------------------------- ssylka noVNC v pish'mah
def test_email_link_live_session(monkeypatch):
    fake = FakeRedis({VNC_HASH: {"job-9": _entry(job_id=9, slot=4)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    monkeypatch.delenv("VNC_PUBLIC_PORT_BASE", raising=False)
    assert vnc_svc.email_link(9, "http://mnema.example:8000") == \
        "http://mnema.example:8084/vnc.html"


def test_email_link_dead_session_or_no_redis_is_none(monkeypatch):
    fake = FakeRedis({VNC_HASH: {"job-9": _entry(job_id=9, slot=1, status="failed")}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    assert vnc_svc.email_link(9, "http://h:8000") is None
    monkeypatch.setattr(vnc_svc, "_client", _raise_conn_error)
    assert vnc_svc.email_link(9, "http://h:8000") is None


def test_email_link_public_port_from_hash_wins(monkeypatch):
    fake = FakeRedis({VNC_HASH: {"job-9": _entry(job_id=9, slot=1, public_port=7777)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    assert vnc_svc.email_link(9, "http://h:8000") == "http://h:7777/vnc.html"


def test_host_from_base_url_variants():
    assert vnc_svc.host_from_base_url("http://mnema.local:8000") == "mnema.local"
    assert vnc_svc.host_from_base_url("https://mnema.local") == "mnema.local"
    assert vnc_svc.host_from_base_url("") == "localhost"
    assert vnc_svc.host_from_base_url(None) == "localhost"


def test_setup_email_link_only_when_setup_alive(monkeypatch):
    alive = json.dumps({"kind": "setup", "slot": 0, "public_port": 8080,
                        "status": "running"})
    fake = FakeRedis({VNC_HASH: {"setup": alive}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    assert vnc_svc.setup_email_link("http://h:8000") == "http://h:8080/vnc.html"
    dead = json.dumps({"kind": "setup", "slot": 0, "public_port": 8080,
                       "status": "failed"})
    fake2 = FakeRedis({VNC_HASH: {"setup": dead}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake2)
    assert vnc_svc.setup_email_link("http://h:8000") is None


# ------------------------------------------------------------ /api/alert+job_id
def test_api_alert_with_job_id_embeds_job_and_vnc_link(client, monkeypatch):
    from mnema.core.db import get_session
    from mnema.core.models import Notification
    jid = client.post("/api/jobs", json={
        "meeting_id": "123456789", "topic": "Капча-тест"}).json()["id"]
    fake = FakeRedis({VNC_HASH: {f"job-{jid}": _entry(job_id=jid, slot=3)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    r = client.post("/api/alert", json={
        "subject": "reCAPTCHA pri vhode", "body": "nuzhen ruchnoy protkhod",
        "dedup_key": "captcha", "job_id": jid})
    assert r.status_code == 200
    nid = r.json()["queued"]
    with get_session() as s:
        n = s.get(Notification, nid)
        data = json.loads(n.data_json)
    assert data["job_id"] == jid and data["topic"] == "Капча-тест"
    assert data["vnc_url"].endswith(":8083/vnc.html")


def test_api_alert_without_job_id_unchanged(client):
    r = client.post("/api/alert", json={"subject": "S", "body": "B"})
    assert r.status_code == 200
    from mnema.core.db import get_session
    from mnema.core.models import Notification
    with get_session() as s:
        n = s.get(Notification, r.json()["queued"])
        data = json.loads(n.data_json)
    assert "job_id" not in data and "vnc_url" not in data


def test_api_alert_unknown_job_id_no_vnc(client, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    r = client.post("/api/alert", json={
        "subject": "S", "body": "B", "dedup_key": "alert:unknown",
        "job_id": 987654})
    assert r.status_code == 200
    from mnema.core.db import get_session
    from mnema.core.models import Notification
    with get_session() as s:
        n = s.get(Notification, r.json()["queued"])
        data = json.loads(n.data_json)
    assert "job_id" not in data and "vnc_url" not in data
