"""VNC-sessii (kontsept "ekran nastroyki + ekrany vstrech").

Menedzher sessiy zhivet v web-bot (web-bot/src/vnc_manager.js): on podnimayet
Xvfb/x11vnc/websockify i publikuet sostoyanie v redis-hash `mnema:vnc_sessions`
(polya "setup" | "job-<id>"). Eta modul — tonnkiy client api:

  * chitaet sostoyanie dlya UI (panel': setup-ekran; stranica vstrechi:
    individual'nyy ekran s ssylkoy na noVNC dlya prolivaniya capchi §12);
  * otpraulyaet komandy v redis-list `mnema:vnc`
    ({action: setup_open|setup_restart|open|close, job_id?}).

Bez redis vse operacii — no-op (kak v ostal'nyh ocheredyah): chtenie {},
otpravka False.
"""
from __future__ import annotations

import json
import os

VNC_HASH = "mnema:vnc_sessions"
VNC_QUEUE = "mnema:vnc"


# Vneshniy (proskol'zennyj docker-compose) port noVNC = base + slot,
# vnutrenniy = 6080+slot (8080<->6080).
def public_port_base() -> int:
    try:
        return int(os.environ.get("VNC_PUBLIC_PORT_BASE", "8080").strip() or 8080)
    except ValueError:
        return 8080


def _client(timeout: float = 1.0):
    from redis import Redis
    from .queue import redis_url
    return Redis.from_url(redis_url(), socket_connect_timeout=timeout)


def read_sessions() -> dict[str, dict]:
    """Vse sostoaniya VNC-sessiy: key -> descriptor (+public_port). Pusto/esli net — {}."""
    try:
        raw = _client().hgetall(VNC_HASH)
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for k, v in raw.items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else v
        try:
            d = json.loads(val)
        except (ValueError, TypeError):
            continue
        # istochnik istiny public_port — js (menedzher): ne perezapisyvaem ego
        # molcha svoim base (raz"ezd env mezhdu konteynerami otherwise silenten).
        # Fallback tol'ko esli polya sovssem net (staroje sodержimoe heisha).
        if "public_port" not in d or d.get("public_port") is None:
            d["public_port"] = public_port_base() + int(d.get("slot") or 0)
        out[key] = d
    return out


def get_session(key: str) -> dict | None:
    return read_sessions().get(key)


def get_setup() -> dict | None:
    return get_session("setup")


def get_for_job(job_id: int) -> dict | None:
    return get_session(f"job-{job_id}")


LIVE_STATUSES = ("ready", "running")


def host_from_base_url(base_url: str | None) -> str:
    """Хост из http(s)://host:port/... (для писем: внешний хост noVNC)."""
    host = (base_url or "").split("//", 1)[-1].split("/", 1)[0]
    host = host.rsplit(":", 1)[0] if ":" in host.split("@")[-1] else host
    return host or "localhost"


def email_link(job_id: int, base_url: str | None = None) -> str | None:
    """noVNC-ссылка для письма: только на живую сессию встречи (ready/running);
    нет сессии / redis недоступен -> None (письмо уходит без ссылки)."""
    sess = get_for_job(job_id)
    if not sess or sess.get("status") not in LIVE_STATUSES:
        return None
    port = sess.get("public_port") or (public_port_base() + int(sess.get("slot") or 0))
    return f"http://{host_from_base_url(base_url)}:{port}/vnc.html"


def setup_email_link(base_url: str | None = None) -> str | None:
    """Ссылка на setup-экран (перелогин Zoom-сессии) — для auth_session_missing,
    где сессия встречи к моменту письма уже освобождена."""
    sess = get_setup()
    if not sess or sess.get("status") not in LIVE_STATUSES:
        return None
    port = sess.get("public_port") or public_port_base()
    return f"http://{host_from_base_url(base_url)}:{port}/vnc.html"


def send_command(action: str, job_id: int | None = None) -> bool:
    """Postavit' komandu menedzheru VNC v redis-list. False esli redis niedostupen."""
    try:
        payload: dict = {"action": action}
        if job_id is not None:
            payload["job_id"] = job_id
        _client().lpush(VNC_QUEUE, json.dumps(payload))
        return True
    except Exception:
        return False


def request_setup_open() -> bool:
    return send_command("setup_open")


def request_setup_restart() -> bool:
    return send_command("setup_restart")


def request_open(job_id: int) -> bool:
    return send_command("open", job_id)


def request_close(job_id: int) -> bool:
    return send_command("close", job_id)
