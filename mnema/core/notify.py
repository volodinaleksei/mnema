"""Единый механизм ходовых уведомлений (FT-35/FT-36).

Канал: SMTP. Очередь писем переживает рестарт (таблица notifications
в БД — это SSOT; redis используется как триггер воркера).
Dedupликация: одинаковый сбой (один job + одна причина) — одно письмо;
повторный сбой категории — cooldown ALERT_COOLDOWN_MIN, остальное в digest.
Escalate: auth_session_missing — сразу, вне digest, копия NOTIFY_EMAIL_ADMIN.

Письма генерируются при отправке: структурированные данные лежат в
Notification.data_json, dispatch рендерит русский multipart/alternative
(core/email_render.py); для job_failed скриншот сбоя — inline cid: через
multipart/related.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from .db import get_session
from .models import Notification
from .tz import app_tz_label, fmt_local
from . import vnc as vnc_svc
from .email_render import RECOMMENDED_ACTION, action_ru, reason_ru, render_email

log = logging.getLogger("mnema.notify")


def screenshot_root() -> Path:
    """Каталог скриншотов сбоев на общем volume api/worker/web-bot."""
    return Path(os.environ.get("DATA_DIR", "/data")) / "failure_screenshots"


def safe_screenshot_path(value: str | None) -> str | None:
    """Принимаем только путь внутри DATA_DIR/failure_screenshots (защита от
    чтения произвольных файлов через API). Иначе None."""
    if not value:
        return None
    root = screenshot_root().resolve()
    try:
        p = Path(value).resolve()
    except OSError:
        return None
    return str(p) if p.is_relative_to(root) else None


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


def parse_notify_events(spec: str | None = None) -> dict[str, str]:
    """FT-36: 'job_failed=email,protocol_ready=email' -> dict kind->channel."""
    raw = (spec if spec is not None else env("NOTIFY_EVENTS",
           "job_failed=email,session_relogin_due=email,protocol_ready=email,"
           "awaiting_llm=email,job_export_failed=email,confirm_end=email,"
           "alert=email"))
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        kind, _, channel = part.partition("=")
        out[kind.strip()] = channel.strip() or "email"
    return out


def should_notify(kind: str) -> bool:
    return parse_notify_events().get(kind) == "email"


def job_failed_subject(topic: str) -> str:
    return f"[Mnema] Не удалось подключиться: {topic or 'встреча без темы'}"


def build_job_failed_body(*, topic: str, meeting_id: str, job_id: int,
                          attempted_at: dt.datetime | None, fail_reason: str,
                          attempts: int, job_url: str) -> str:
    """FT-35b. Пароль в письме НЕ указывается. Компактная русская plain-версия."""
    ts = fmt_local(attempted_at or dt.datetime.now(dt.timezone.utc),
                   "%d.%m.%Y %H:%M") + " (" + app_tz_label() + ")"
    lines = [
        f"Встреча: {topic or '(без темы)'}",
        f"meeting_id: {meeting_id}",
        f"Время попытки: {ts}",
        f"Причина отказа: {reason_ru(fail_reason)} ({fail_reason})",
        f"Попыток: {attempts}",
        f"Рекомендуемое действие: {action_ru(fail_reason)}",
        f"Задача: {job_id}",
        f"Ссылка: {job_url}",
    ]
    return "\n".join(lines) + "\n"


def job_failed_data(*, job_id: int, topic: str, meeting_id: str,
                    fail_reason: str, attempts: int,
                    attempted_at: dt.datetime | None,
                    job_url: str, vnc_url: str | None = None) -> dict:
    data = {
        "job_id": job_id, "topic": topic, "meeting_id": meeting_id,
        "fail_reason": fail_reason, "attempts": attempts,
        "attempted_at": (attempted_at or dt.datetime.now(dt.timezone.utc)).isoformat(),
        "job_url": job_url,
    }
    if vnc_url:
        data["vnc_url"] = vnc_url
    return data


class NotifyService:
    """Enqueue с dedup/cooldown + dispatch по SMTP. Идемпотентен по dedup_key."""

    def __init__(self, sender: Callable[[EmailMessage], None] | None = None,
                 base_url: str | None = None):
        self._sender = sender
        self.base_url = (base_url or env("MNEMA_PUBLIC_URL", "http://localhost:8000")).rstrip("/")

    # --- enqueue -------------------------------------------------------
    def enqueue_job_failed(self, *, job_id: int, topic: str, meeting_id: str,
                           fail_reason: str, attempts: int,
                           attempted_at: dt.datetime | None = None,
                           screenshot_path: str | None = None) -> Notification | None:
        dedup = f"job_failed:{job_id}:{fail_reason}"
        now = dt.datetime.now(dt.timezone.utc)
        cooldown_min = int(env("ALERT_COOLDOWN_MIN", "60") or 60)
        job_url = f"{self.base_url}/jobs/{job_id}"
        vnc_url = vnc_svc.email_link(job_id, self.base_url)
        # FT: nuzhno vmeshatel'stvo cheloveka — ssylka na zhivoy ekrannyy slot;
        # pri protuhshem vkhode (sessiya vstrechi uzhe osvobozhdena) — na setup-ekran
        if not vnc_url and fail_reason == "auth_session_missing":
            vnc_url = vnc_svc.setup_email_link(self.base_url)
        data = job_failed_data(job_id=job_id, topic=topic, meeting_id=meeting_id,
                               fail_reason=fail_reason, attempts=attempts,
                               attempted_at=attempted_at, job_url=job_url,
                               vnc_url=vnc_url)
        shot = safe_screenshot_path(screenshot_path)
        with get_session() as s:
            prev = s.scalars(select(Notification).where(
                Notification.dedup_key == dedup,
            ).order_by(Notification.created_ts.desc())).first()
            if prev is not None:
                # повторный сбой: cooldown; если прошел — новое письмо, иначе digest
                since_last = (now - prev.created_ts.replace(tzinfo=dt.timezone.utc)
                              if prev.created_ts.tzinfo is None
                              else now - prev.created_ts).total_seconds() / 60
                if since_last < cooldown_min:
                    n = Notification(
                        kind="job_failed", dedup_key=dedup,
                        subject=f"[repeat] {job_failed_subject(topic)}",
                        body="повтор сбоя в пределах cooldown — отнесён в digest",
                        data_json=json.dumps(data, ensure_ascii=False),
                        screenshot_path=shot,
                        to_addr="",
                        status="digest")
                    s.add(n)
                    s.commit()
                    return n
            body = build_job_failed_body(
                topic=topic, meeting_id=meeting_id, job_id=job_id,
                attempted_at=attempted_at, fail_reason=fail_reason,
                attempts=attempts, job_url=job_url)
            crit = fail_reason == "auth_session_missing"  # FT-35g
            n = Notification(
                kind="job_failed", dedup_key=dedup,
                subject=job_failed_subject(topic), body=body,
                data_json=json.dumps(data, ensure_ascii=False),
                screenshot_path=shot,
                to_addr=env("NOTIFY_EMAIL_TO"),
                cc_addr=(env("NOTIFY_EMAIL_ADMIN") if crit and env("NOTIFY_EMAIL_ADMIN") else None),
                status="pending")
            s.add(n)
            s.commit()
            s.refresh(n)
            return n

    def enqueue(self, kind: str, subject: str, body: str,
                dedup_key: str | None = None,
                data: dict | None = None,
                screenshot_path: str | None = None) -> Notification | None:
        if not should_notify(kind):
            return None
        with get_session() as s:
            if dedup_key and s.scalars(select(Notification).where(
                    Notification.dedup_key == dedup_key,
                    Notification.status.in_(("pending", "digest")))).first():
                return None
            n = Notification(kind=kind, dedup_key=dedup_key, subject=subject,
                             body=body,
                             data_json=(json.dumps(data, ensure_ascii=False)
                                        if data else None),
                             screenshot_path=safe_screenshot_path(screenshot_path),
                             to_addr=env("NOTIFY_EMAIL_TO"), status="pending")
            s.add(n)
            s.commit()
            s.refresh(n)
            return n

    # --- dispatch ------------------------------------------------------
    def sender(self) -> Callable[[EmailMessage], None]:
        if self._sender is not None:
            return self._sender
        return _smtp_send

    def build_message(self, n: Notification) -> EmailMessage:
        """multipart/alternative (ru plain + html). job_failed/confirm_end +
        существующий скриншот -> related(alternative, image/png cid:failure_screenshot)."""
        subject, html, text = render_email(n)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = env("NOTIFY_FROM", "Mnema <mnema@localhost>")
        msg["To"] = n.to_addr
        if n.cc_addr:
            msg["Cc"] = n.cc_addr
        shot_bytes = None
        if n.kind in ("job_failed", "confirm_end") and n.screenshot_path:
            if os.path.isfile(n.screenshot_path):
                try:
                    shot_bytes = Path(n.screenshot_path).read_bytes()
                except OSError as e:
                    log.warning("screenshot %s is unreadable (%s) — email %s "
                                "goes without image", n.screenshot_path, e, n.id)
            else:
                log.warning("screenshot %s not found — email %s goes without image",
                            n.screenshot_path, n.id)
        if shot_bytes:
            msg.make_related()
            alt = EmailMessage()
            alt.set_content(text)
            alt.add_alternative(html, subtype="html")
            msg.attach(alt)
            img = EmailMessage()
            img.set_content(shot_bytes, maintype="image", subtype="png")
            img["Content-ID"] = "<failure_screenshot>"
            img["Content-Disposition"] = 'inline; filename="failure_screenshot.png"'
            msg.attach(img)
            return msg
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        return msg

    def dispatch_pending(self, limit: int = 20) -> int:
        """Отдаёт письма-pending с retry/backoff (повторный вызов отлагает)."""
        sent = 0
        with get_session() as s:
            rows = s.scalars(select(Notification).where(
                Notification.status == "pending").limit(limit)).all()
        for n in rows:
            msg = self.build_message(n)
            try:
                self.sender()(msg)
            except Exception as e:  # noqa: BLE001
                with get_session() as s:
                    row = s.get(Notification, n.id)
                    row.attempts += 1
                    row.last_error = str(e)[:2000]
                    if row.attempts >= 5:
                        row.status = "error"
                    s.commit()
                continue
            with get_session() as s:
                row = s.get(Notification, n.id)
                row.status = "sent"
                row.attempts += 1
                row.sent_ts = dt.datetime.now(dt.timezone.utc)
                s.commit()
            sent += 1
        return sent

    def build_digest(self) -> Notification | None:
        """FT-35v: пачка digest одним письмом; вызывается по ALERT_DIGEST_CRON."""
        with get_session() as s:
            rows = s.scalars(select(Notification).where(
                Notification.status == "digest").order_by(Notification.created_ts)).all()
            if not rows:
                return None
            items: list[dict] = []
            for r in rows:
                items.append({"subject": r.subject,
                              "body": (r.body or "").strip()})
                r.status = "sent"
                r.sent_ts = dt.datetime.now(dt.timezone.utc)
            body_bits = ["Сводка событий Mnema:", ""]
            for it in items:
                body_bits.append(f"- {it['subject']}")
                if it["body"]:
                    body_bits.append(it["body"])
            digest = Notification(
                kind="digest", subject="[Mnema] Сводка событий",
                body="\n".join(body_bits) + "\n",
                data_json=json.dumps({"items": items}, ensure_ascii=False),
                to_addr=env("NOTIFY_EMAIL_TO"), status="pending")
            s.add(digest)
            s.commit()
            s.refresh(digest)
            return digest


def _smtp_send(msg: EmailMessage) -> None:
    host = env("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP_HOST не задан")
    port = int(env("SMTP_PORT", "587") or 587)
    user = env("SMTP_USER")
    password = _smtp_password()
    with smtplib.SMTP(host, port, timeout=30) as c:
        try:
            c.starttls(context=ssl.create_default_context())
        except smtplib.SMTPNotSupportedError:
            pass
        if user and password:
            c.login(user, password)
        c.send_message(msg)


def _smtp_password() -> str:
    """SMTP_PASSWORD_REF — ссылка на vault; v1: прямой SMTP_PASSWORD."""
    return env("SMTP_PASSWORD")
