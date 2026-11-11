import datetime as dt
import json
from email.message import EmailMessage

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mnema.core.db import Base, set_engine_for_test
from mnema.core import notify
from mnema.core.notify import (NotifyService, build_job_failed_body,
                               job_failed_subject, parse_notify_events,
                               safe_screenshot_path, should_notify)


@pytest.fixture()
def session(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    yield sessionmaker(bind=engine)


@pytest.fixture()
def env_base(monkeypatch):
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "ops@example.ru")
    monkeypatch.setenv("NOTIFY_EMAIL_ADMIN", "admin@example.ru")
    monkeypatch.setenv("ALERT_COOLDOWN_MIN", "60")
    monkeypatch.setenv("DATA_DIR", "/tmp/mnema-test-data")
    monkeypatch.setenv("NOTIFY_EVENTS",
                       "job_failed=email,session_relogin_due=email,"
                       "protocol_ready=email,awaiting_llm=email")


def _walk(msg, content_type: str) -> list:
    return [p for p in msg.walk() if p.get_content_type() == content_type]


def test_parse_notify_events_default():
    ev = parse_notify_events("job_failed=email,protocol_ready=email")
    assert ev["job_failed"] == "email" and ev["protocol_ready"] == "email"


def test_default_events_include_alert(monkeypatch):
    monkeypatch.delenv("NOTIFY_EVENTS", raising=False)
    assert should_notify("alert")


def test_fallback_default_matches_registry(monkeypatch):
    """Defolty-zaas v parse_notify_events ne dolzhen rashoditsya s reestrom:
    pri pustom NOTIFY_EVENTS pis'ma ne trebom droptsya (sm. job_export_failed)."""
    from mnema.core.registry import KEY_INDEX
    monkeypatch.delenv("NOTIFY_EVENTS", raising=False)
    registry_default = parse_notify_events(KEY_INDEX["NOTIFY_EVENTS"].default)
    fallback = parse_notify_events(None)
    assert fallback == registry_default


def test_enqueue_alert_with_default_events(monkeypatch, session):
    monkeypatch.delenv("NOTIFY_EVENTS", raising=False)
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "ops@example.ru")
    sent = []
    ns = NotifyService(sender=lambda m: sent.append(m))
    n = ns.enqueue("alert", "[Mnema] Автоподключение зависло", "текст",
                   dedup_key="join_stuck:25")
    assert n is not None and n.status == "pending"
    assert ns.dispatch_pending() == 1
    assert sent and "Автоподключение зависло" in sent[0]["Subject"]


def test_should_notify_respects_events(monkeypatch, env_base):
    assert should_notify("job_failed")
    monkeypatch.setenv("NOTIFY_EVENTS", "protocol_ready=email")
    assert not should_notify("job_failed")


def test_build_body_has_required_fields():
    body = build_job_failed_body(
        topic="Планёрка", meeting_id="9814832357", job_id=7,
        attempted_at=dt.datetime(2026, 9, 17, 10, 0, tzinfo=dt.timezone.utc),
        fail_reason="auth_session_missing", attempts=1,
        job_url="http://h/jobs/7")
    for token in ("Планёрка", "9814832357", "auth_session_missing",
                  "http://h/jobs/7", "Задача: 7", "Причина отказа",
                  "Рекомендуемое действие"):
        assert token in body
    assert "pwd" not in body.lower()


def test_subject_format():
    assert job_failed_subject("X") == "[Mnema] Не удалось подключиться: X"


def test_enqueue_first_is_pending(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    assert n.status == "pending"


def test_enqueue_saves_data_json_and_screenshot(env_base, session, monkeypatch,
                                                tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    shot_dir = tmp_path / "failure_screenshots"
    shot_dir.mkdir()
    shot = shot_dir / "job_5.png"
    shot.write_bytes(b"\x89PNG fake")
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=5, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1,
                              screenshot_path=str(shot))
    assert n.screenshot_path == str(shot.resolve())
    data = json.loads(n.data_json)
    assert data["job_id"] == 5 and data["fail_reason"] == "timeout"
    assert data["meeting_id"] == "111"


def test_screenshot_path_outside_root_rejected(env_base, session, monkeypatch,
                                               tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=6, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1,
                              screenshot_path="/etc/passwd")
    assert n.screenshot_path is None


def test_safe_screenshot_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    root = tmp_path / "failure_screenshots"
    root.mkdir()
    ok = root / "job_1.png"
    ok.write_bytes(b"x")
    assert safe_screenshot_path(str(ok)) == str(ok.resolve())
    assert safe_screenshot_path(str(tmp_path / "evil.png")) is None
    assert safe_screenshot_path("/etc/passwd") is None
    assert safe_screenshot_path(None) is None
    # выход из корня через ..
    assert safe_screenshot_path(str(root / ".." / ".." / "secret.png")) is None


def test_duplicate_same_reason_within_cooldown_goes_to_digest(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    a = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    b = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    assert a.status == "pending"
    assert b.status == "digest"


def test_different_reason_not_duplicate(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    a = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    b = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="not_started", attempts=1)
    assert a.status == "pending" and b.status == "pending"


def test_auth_session_missing_escalates_with_cc(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=2, topic="T", meeting_id="222",
                              fail_reason="auth_session_missing", attempts=1)
    assert n.status == "pending"
    assert n.cc_addr == "admin@example.ru"


def test_after_cooldown_new_pending(env_base, session, monkeypatch):
    monkeypatch.setenv("ALERT_COOLDOWN_MIN", "0")
    ns = NotifyService(sender=lambda m: None)
    ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                          fail_reason="timeout", attempts=1)
    b = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    assert b.status == "pending"


def test_build_message_multipart_alternative(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    msg = ns.build_message(n)
    assert msg.get_content_type() == "multipart/alternative"
    assert _walk(msg, "text/plain") and _walk(msg, "text/html")
    html = _walk(msg, "text/html")[0].get_content()
    assert "Не удалось подключиться к совещанию" in html
    assert "111" in html


def test_build_message_job_failed_with_screenshot_related(env_base, session,
                                                          monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    shot_dir = tmp_path / "failure_screenshots"
    shot_dir.mkdir()
    shot = shot_dir / "job_3.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n fake png bytes")
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=3, topic="T", meeting_id="333",
                              fail_reason="timeout", attempts=2,
                              screenshot_path=str(shot))
    msg = ns.build_message(n)
    assert msg.get_content_subtype() == "related"
    imgs = _walk(msg, "image/png")
    assert len(imgs) == 1
    assert imgs[0]["Content-ID"] == "<failure_screenshot>"
    html = _walk(msg, "text/html")[0].get_content()
    assert "cid:failure_screenshot" in html
    assert "multipart/alternative" in [p.get_content_type() for p in msg.walk()]


def test_build_message_missing_screenshot_falls_back(env_base, session,
                                                     monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    ghost = tmp_path / "failure_screenshots" / "job_4.png"  # не создаём
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=4, topic="T", meeting_id="444",
                              fail_reason="timeout", attempts=1,
                              screenshot_path=str(ghost))
    assert n.screenshot_path  # путь сохранён
    msg = ns.build_message(n)
    assert msg.get_content_type() == "multipart/alternative"  # без related
    assert not _walk(msg, "image/png")
    html = _walk(msg, "text/html")[0].get_content()
    assert "cid:failure_screenshot" not in html


def test_build_message_legacy_row_without_data_json(env_base, session):
    """Риск из плана: старая pending-строка без data_json не роняет dispatch."""
    from mnema.core.models import Notification
    with session() as s:
        n = Notification(kind="job_failed", subject="[Mnema] Старое письмо",
                         body="legacy plain body", data_json=None,
                         to_addr="ops@example.ru", status="pending")
        s.add(n)
        s.commit()
        n_id = n.id
    ns = NotifyService(sender=lambda m: None)
    sent: list[EmailMessage] = []
    ns._sender = lambda m: sent.append(m)
    assert ns.dispatch_pending() == 1
    msg = sent[0]
    assert msg.get_content_type() == "multipart/alternative"
    assert "legacy plain body" in _walk(msg, "text/html")[0].get_content()


def test_dispatch_renders_russian_html(env_base, session):
    sent: list[EmailMessage] = []
    ns = NotifyService(sender=lambda m: sent.append(m))
    ns.enqueue_job_failed(job_id=1, topic="Тема", meeting_id="111",
                          fail_reason="timeout", attempts=1)
    cnt = ns.dispatch_pending()
    assert cnt == 1
    html = _walk(sent[0], "text/html")[0].get_content()
    assert "Не удалось подключиться" in html and "111" in html
    from mnema.core.models import Notification
    with session() as s:
        assert s.query(Notification).filter_by(status="sent").count() == 1


def test_dispatch_retry_keeps_pending(env_base, session):
    def boom(m):
        raise RuntimeError("smtp down")
    ns = NotifyService(sender=boom)
    ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                          fail_reason="timeout", attempts=1)
    assert ns.dispatch_pending() == 0
    from mnema.core.models import Notification
    with session() as s:
        n = s.query(Notification).first()
        assert n.status == "pending" and n.attempts == 1
        assert "smtp down" in n.last_error


def test_digest_collects_and_clears(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    for _ in range(3):
        ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    d = ns.build_digest()
    assert d is not None and d.status == "pending"
    assert "Сводка событий" in d.subject
    from mnema.core.models import Notification
    with session() as s:
        assert s.query(Notification).filter_by(status="digest").count() == 0


def test_digest_renders_russian_list(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    for _ in range(2):
        ns.enqueue_job_failed(job_id=1, topic="Т", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    d = ns.build_digest()
    msg = ns.build_message(d)
    html = _walk(msg, "text/html")[0].get_content()
    assert "Сводка событий Mnema" in html
    text = _walk(msg, "text/plain")[0].get_content()
    assert "Сводка событий Mnema" in text


def test_render_escapes_html_injection(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=1, topic="<script>alert(1)</script>",
                              meeting_id="111", fail_reason="timeout", attempts=1)
    ns._sender = lambda m: None
    msg = ns.build_message(n)
    html = _walk(msg, "text/html")[0].get_content()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_every_kind_has_russian_fields():
    from mnema.core.email_render import render_email

    class N:
        def __init__(self, kind, subject, data, body="", screenshot_path=None):
            self.kind, self.subject, self.body = kind, subject, body
            self.data_json = json.dumps(data, ensure_ascii=False)
            self.screenshot_path = screenshot_path

    _, html, text = render_email(N("protocol_ready", "[Mnema] Протокол готов: В",
                                   {"topic": "В", "job_url": "http://h/jobs/1",
                                    "protocol_url": "http://h/protocols/33",
                                    "audio_url": "http://h/a"}))
    assert "Протокол готов" in html and "Открыть протокол" in html
    assert "Протокол готов" in text
    assert 'href="http://h/protocols/33"' in html
    assert "http://h/protocols/33" in text
    # кнопка «Протокол» ведёт на протокол, а не на задачу
    proto_btn = html.split('Открыть протокол')[0].rsplit("<a href=", 1)[1]
    assert "/protocols/33" in proto_btn and "/jobs/1" not in proto_btn
    _, html, text = render_email(N("awaiting_llm", "[Mnema] Стенограмма: В",
                                   {"topic": "В", "job_url": "http://h/jobs/2"}))
    assert "LLM" in html and "Стенограмма" in html and "Стенограмма" in text
    _, html, _ = render_email(N("alert", "[Mnema] reCAPTCHA",
                                {"message": "Нужна ручная капча"},
                                body="Нужна ручная капча"))
    assert "Внимание" in html and "капча" in html
    _, html, _ = render_email(N("job_export_failed", "[Mnema] Не выгружено: В",
                                {"job_id": 9, "topic": "В",
                                 "message": "Шара недоступна"}))
    assert "Шара недоступна" in html
    # неизвестный kind -> фолбэк по body, без падения
    _, html, text = render_email(N("weird_kind", "[Mnema] Что-то", {},
                                   body="текст тела"))
    assert "текст тела" in html and "Что-то" in text


def test_protocol_ready_link_falls_back_without_protocol_url():
    from mnema.core.email_render import render_email

    class N:
        def __init__(self, kind, subject, data):
            self.kind, self.subject, self.body = kind, subject, ""
            self.data_json = json.dumps(data, ensure_ascii=False)
            self.screenshot_path = None

    # старое уведомление без protocol_url, но с protocol_id -> ссылка на протокол
    _, html, _ = render_email(N("protocol_ready", "s",
                                {"job_url": "http://h/jobs/41",
                                 "protocol_id": 33}))
    assert 'href="http://h/protocols/33"' in html
    # совсем старое без протокола -> фолбэк на задачу, без падения
    _, html, _ = render_email(N("protocol_ready", "s",
                                {"job_url": "http://h/jobs/41"}))
    assert 'href="http://h/jobs/41"' in html


def test_no_pwd_anywhere_in_email(env_base, session):
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=1, topic="Сов secret",
                              meeting_id="333", fail_reason="not_started", attempts=2)
    assert "pwd" not in n.subject and "pwd" not in n.body.lower()
    msg = ns.build_message(n)
    assert "pwd" not in msg.as_string().lower()


# ------------------------------------------------- VNC-ssylka v pish'mah sboya


class FakeVncRedis:
    def __init__(self, data=None):
        self.hashes = data or {}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


def _vnc_entry(slot=1, status="running", public_port=None):
    d = {"kind": "job", "job_id": 1, "slot": slot, "display": f":{99 + slot}",
         "ws_port": 6080 + slot, "profile": "/data/zoom-profiles/job-1",
         "status": status, "ts": "2026-11-10T00:00:00Z", "error": None}
    if public_port is not None:
        d["public_port"] = public_port
    return json.dumps(d)


def test_job_failed_includes_live_vnc_link(env_base, session, monkeypatch):
    from mnema.core import vnc as vnc_svc
    fake = FakeVncRedis({"mnema:vnc_sessions": {"job-1": _vnc_entry(slot=1)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    ns = NotifyService(sender=lambda m: None, base_url="http://mnema.example:8000")
    n = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    data = json.loads(n.data_json)
    assert data["vnc_url"] == "http://mnema.example:8081/vnc.html"


def test_job_failed_no_vnc_link_when_session_dead(env_base, session, monkeypatch):
    from mnema.core import vnc as vnc_svc
    fake = FakeVncRedis({"mnema:vnc_sessions": {
        "job-1": _vnc_entry(slot=1, status="failed")}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    assert "vnc_url" not in json.loads(n.data_json)


def test_job_failed_auth_missing_falls_back_to_setup_link(env_base, session,
                                                          monkeypatch):
    from mnema.core import vnc as vnc_svc
    setup = json.dumps({"kind": "setup", "job_id": None, "slot": 0,
                        "display": ":99", "ws_port": 6080,
                        "profile": "/data/zoom-profile", "public_port": 8080,
                        "status": "running", "ts": "2026-11-10T00:00:00Z"})
    fake = FakeVncRedis({"mnema:vnc_sessions": {"setup": setup}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    ns = NotifyService(sender=lambda m: None, base_url="http://mnema.example:8000")
    n = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="auth_session_missing", attempts=1)
    data = json.loads(n.data_json)
    assert data["vnc_url"] == "http://mnema.example:8080/vnc.html"


def test_job_failed_email_has_clickable_vnc_button(env_base, session, monkeypatch):
    from mnema.core import vnc as vnc_svc
    fake = FakeVncRedis({"mnema:vnc_sessions": {"job-1": _vnc_entry(slot=2)}})
    monkeypatch.setattr(vnc_svc, "_client", lambda timeout=1.0: fake)
    ns = NotifyService(sender=lambda m: None, base_url="http://mnema.example:8000")
    n = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="waiting_room_stuck", attempts=1)
    msg = ns.build_message(n)
    html = _walk(msg, "text/html")[0].get_content()
    text = _walk(msg, "text/plain")[0].get_content()
    assert 'href="http://mnema.example:8082/vnc.html"' in html
    assert "noVNC" in html
    assert "http://mnema.example:8082/vnc.html" in text


def test_job_failed_email_without_vnc_no_button(env_base, session, monkeypatch):
    from mnema.core import vnc as vnc_svc
    monkeypatch.setattr(vnc_svc, "_client",
                        lambda timeout=1.0: FakeVncRedis())
    ns = NotifyService(sender=lambda m: None)
    n = ns.enqueue_job_failed(job_id=1, topic="T", meeting_id="111",
                              fail_reason="timeout", attempts=1)
    html = _walk(ns.build_message(n), "text/html")[0].get_content()
    assert "noVNC" not in html
    assert "vnc.html" not in html
