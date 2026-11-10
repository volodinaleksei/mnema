"""Render HTML/text-альтернатив писем уведомлений (русские, Outlook-совместимые).

Стиль — карточка max-width 600px на таблицах с inline-стилями: цветная шапка
статуса, таблица полей, подвал «Автоматическое уведомление системы Mnema».
Шаблоны встроены строками (Jinja2 DictLoader): к путям файлов контейнеров не
привязываемся. autoescape ON.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from jinja2 import DictLoader, Environment
from markupsafe import Markup

FOOTER = "Автоматическое уведомление системы Mnema"

FAIL_REASON_RU = {
    "zoom_requires_rtms": "хозяин встречи разрешает вход только из приложения "
                          "(вход из браузера заблокирован)",
    "auth_session_missing": "сессия Zoom в профиле web-bot истекла — "
                            "нужен повторный вход",
    "waiting_room_stuck": "хозяин не впустил в зал ожидания",
    "not_started": "встреча не началась к дедлайну входа",
    "page_crash": "страница или браузер упали во время записи",
    "timeout": "тайм-аут входа / недоступность Zoom",
    "cancelled": "запись остановлена вручную",
    "llm_invalid_json": "LLM вернул некорректный JSON протокола",
    "recording_lost": "аудиозапись потеряна: на диске нет ни сырых сегментов, "
                      "ни финального файла (web-bot не успел записать)",
    "room_busy": "комната занята: в этом же Zoom-ID уже идёт запись другой "
                 "встречи (второй агент в созвон не заходит)",
}

RECOMMENDED_ACTION = {
    "zoom_requires_rtms": "попросите администратора аккаунта-хозяина разрешить "
                          "вход из браузера (не только из приложения)",
    "auth_session_missing": "перелогиньте Zoom-сессию в профиле web-bot "
                            "(headful login) — под угрозой все будущие встречи",
    "waiting_room_stuck": "договоритесь с хозяином о ручном впуске или "
                          "автопропуске из зала ожидания",
    "not_started": "проверьте ссылку и время начала встречи",
    "page_crash": "проверьте логи контейнера web-bot",
    "timeout": "проверьте сетевую доступность Zoom",
    "room_busy": "предыдущая запись в этой комнате ещё идёт (бот ждёт тишину). "
                 "Дождитесь её завершения и перезапустите встречу, либо "
                 "завершите предыдущую вручную, если созвон на самом деле кончился",
}

_MACROS = """
{% macro page(color, title) -%}
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mnema: {{ title }}</title>
</head>
<body style="margin:0;padding:0;background-color:#eef1f5;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:#eef1f5;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:8px;overflow:hidden;font-family:Arial,Helvetica,sans-serif;">
<tr><td style="background-color:{{ color }};padding:20px 28px;">
<div style="color:#ffffff;font-size:12px;letter-spacing:1px;text-transform:uppercase;">Mnema</div>
<div style="color:#ffffff;font-size:18px;font-weight:bold;margin-top:4px;">{{ title }}</div>
</td></tr>
<tr><td style="padding:24px 28px;color:#2c3e50;font-size:14px;line-height:1.55;">
{{ caller() }}
</td></tr>
<tr><td style="padding:16px 28px;border-top:1px solid #e5e9ef;color:#8a95a5;font-size:12px;">""" + FOOTER + """</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
{%- endmacro %}

{% macro fields(rows) -%}
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
{% for label, value in rows %}<tr>
<td style="padding:5px 14px 5px 0;color:#8a95a5;font-size:13px;white-space:nowrap;vertical-align:top;">{{ label }}</td>
<td style="padding:5px 0;color:#2c3e50;font-size:14px;">{{ value }}</td>
</tr>
{% endfor %}</table>
{%- endmacro %}

{% macro button(label, url, color='#2e6fd8') -%}
{% if url %}<a href="{{ url }}" style="display:inline-block;background-color:{{ color }};color:#ffffff;font-size:14px;text-decoration:none;padding:10px 18px;border-radius:6px;margin:18px 10px 0 0;">{{ label }}</a>{% endif %}
{%- endmacro %}
"""

_JOB_FAILED_HTML = """
{% from "_macros.html" import page, fields, button %}
{% call page('#c0392b', 'Не удалось подключиться к совещанию') %}
<p style="margin:0 0 14px;">Mnema не смогла подключиться к совещанию — запись не началась.</p>
{{ fields(rows) }}
<div style="margin-top:18px;background-color:#fdf3f2;border:1px solid #f0c8c4;border-radius:6px;padding:12px 14px;color:#2c3e50;">
<span style="color:#c0392b;font-weight:bold;">Рекомендуемое действие:</span> {{ action }}
</div>
{{ button('Открыть задачу', job_url) }}
{% if vnc_url %}{{ button('Посмотреть экран совещания (noVNC)', vnc_url, '#c0392b') }}{% endif %}
{% if screenshot_ok %}
<div style="margin-top:20px;">
<div style="color:#8a95a5;font-size:12px;margin-bottom:6px;">Скриншот страницы в момент сбоя:</div>
<img src="cid:failure_screenshot" alt="Скриншот страницы в момент сбоя" width="544" style="max-width:100%;border:1px solid #d8dee7;border-radius:6px;display:block;">
</div>
{% endif %}
{% endcall %}
"""

_JOB_FAILED_TXT = """Mnema: не удалось подключиться к совещанию.

Встреча: {{ topic }}
meeting_id: {{ meeting_id }}
Время попытки: {{ attempted_ts }}
Причина отказа: {{ reason_ru }} ({{ code }})
Попыток: {{ attempts }}
Рекомендуемое действие: {{ action }}
Задача: {{ job_id }}
Ссылка: {{ job_url }}{% if vnc_url %}
Экран совещания (noVNC): {{ vnc_url }}{% endif %}{% if screenshot_ok %}
Скриншот страницы в момент сбоя — во вложении.{% endif %}
"""

_PROTOCOL_READY_HTML = """
{% from "_macros.html" import page, fields, button %}
{% call page('#27ae60', 'Протокол готов') %}
<p style="margin:0 0 14px;">Mnema записала совещание и сформировала протокол.</p>
{{ fields([('Встреча', topic)]) }}
{{ button('Протокол', job_url) }}
{{ button('Аудиозапись', audio_url, '#7f8c8d') }}
{% endcall %}
"""

_AWAITING_LLM_HTML = """
{% from "_macros.html" import page, fields, button %}
{% call page('#e67e22', 'Стенограмма готова, LLM недоступен') %}
<p style="margin:0 0 14px;">Стенограмма совещания сформирована, но структурирование
LLM отложено: LLM не настроен или недоступен.</p>
{{ fields([('Встреча', topic)]) }}
<div style="margin-top:18px;background-color:#fdf6ee;border:1px solid #eed9bd;border-radius:6px;padding:12px 14px;color:#2c3e50;">
<span style="color:#b3661a;font-weight:bold;">Что сделать:</span>
 донастройте LLM и запустите повторную обработку задачи.
</div>
{{ button('Открыть задачу', job_url) }}
{% endcall %}
"""

_CONFIRM_END_HTML = """
{% from "_macros.html" import page, fields, button %}
{% call page('#e67e22', 'Завершить запись встречи?') %}
<p style="margin:0 0 14px;">{{ message | nl2br }}</p>
{% if rows %}{{ fields(rows) }}{% endif %}
<div style="margin-top:14px;">
{{ button('Завершить запись', confirm_url, '#c0392b') }}
{{ button('Посмотреть экран совещания', vnc_url, '#2e6fd8') }}
</div>
<div style="margin-top:18px;background-color:#fdf6ee;border:1px solid #eed9bd;border-radius:6px;padding:12px 14px;color:#2c3e50;">
<span style="color:#b3661a;font-weight:bold;">Важно:</span>
запись продолжается, пока вы не нажмёте «Завершить». Если звук в совещании есть —
просто закройте письмо.
</div>
{% if screenshot_ok %}
<div style="margin-top:20px;">
<div style="color:#8a95a5;font-size:12px;margin-bottom:6px;">Скриншот экрана в момент вопроса:</div>
<img src="cid:failure_screenshot" alt="Скриншот экрана в момент вопроса" width="544" style="max-width:100%;border:1px solid #d8dee7;border-radius:6px;display:block;">
</div>
{% endif %}
{% endcall %}
"""

_ALERT_HTML = """
{% from "_macros.html" import page, fields, button %}
{% call page('#5d7290', 'Внимание') %}
<p style="margin:0 0 14px;">{{ message | nl2br }}</p>
{% if rows %}{{ fields(rows) }}{% endif %}
{{ button('Открыть задачу', job_url) }}
{{ button('Посмотреть экран совещания (noVNC)', vnc_url, '#c0392b') }}
{% endcall %}
"""

_DIGEST_HTML = """
{% from "_macros.html" import page %}
{% call page('#4a5b6e', 'Сводка событий Mnema') %}
{% for item in items %}
<div style="{% if not loop.first %}margin-top:16px;padding-top:16px;border-top:1px solid #e5e9ef;{% endif %}">
<div style="font-weight:bold;color:#2c3e50;font-size:14px;margin-bottom:4px;">{{ item.subject }}</div>
{% if item.body %}<div style="color:#5a6675;font-size:13px;">{{ item.body | nl2br }}</div>{% endif %}
</div>
{% endfor %}
{% endcall %}
"""

_FALLBACK_HTML = """
{% from "_macros.html" import page %}
{% call page('#5d7290', title) %}
<p style="margin:0;">{{ body | nl2br }}</p>
{% endcall %}
"""

_GENERIC_TXT = """{{ title }}

{{ message }}
"""


def _nl2br(value) -> Markup:
    from markupsafe import escape
    return Markup(escape(str(value)).replace("\n", "<br>"))


_env = Environment(
    loader=DictLoader({
        "_macros.html": _MACROS,
        "job_failed.html": _JOB_FAILED_HTML,
        "protocol_ready.html": _PROTOCOL_READY_HTML,
        "awaiting_llm.html": _AWAITING_LLM_HTML,
        "confirm_end.html": _CONFIRM_END_HTML,
        "alert.html": _ALERT_HTML,
        "digest.html": _DIGEST_HTML,
        "fallback.html": _FALLBACK_HTML,
        "job_failed.txt": _JOB_FAILED_TXT,
        "generic.txt": _GENERIC_TXT,
    }),
    autoescape=lambda name: bool(name) and name.endswith(".html"),
)
_env.filters["nl2br"] = _nl2br


def parse_notification_data(n) -> dict | None:
    """Notification-like -> dict из data_json; None/битый JSON -> None."""
    raw = getattr(n, "data_json", None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def screenshot_exists(n) -> bool:
    """job_failed/confirm_end + screenshot_path указывает на существующий файл."""
    p = getattr(n, "screenshot_path", None)
    return bool(p) and getattr(n, "kind", "") in ("job_failed", "confirm_end") \
        and os.path.isfile(p)


def fmt_attempt_ts(value) -> str:
    from .tz import app_tz_label, fmt_local
    if isinstance(value, str) and value:
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return value
    if value is None:
        return "—"
    return f"{fmt_local(value, '%d.%m.%Y %H:%M')} ({app_tz_label()})"


def reason_ru(code: str | None) -> str:
    return FAIL_REASON_RU.get(code or "", code or "неизвестная причина")


def action_ru(code: str | None) -> str:
    return RECOMMENDED_ACTION.get(code or "",
                                  "смотрите детали задачи в системе Mnema")


def _topic(data: dict) -> str:
    return data.get("topic") or "(без темы)"


def render_email(n) -> tuple[str, str, str]:
    """Notification-like -> (subject, html, text_alt).

    Старые строки без data_json / битый JSON / неизвестный kind — фолбэк:
    нейтральная карточка по body (риск из плана: dispatch не падает).
    """
    kind = getattr(n, "kind", "") or ""
    subject = getattr(n, "subject", "") or "Уведомление Mnema"
    body = getattr(n, "body", "") or ""
    data = parse_notification_data(n) or {}
    title = (subject.replace("[Mnema] ", "", 1).replace("[repeat] ", "", 1)
             .strip() or "Уведомление Mnema")
    try:
        if kind == "job_failed" and data:
            code = data.get("fail_reason") or ""
            ctx = dict(
                topic=_topic(data),
                meeting_id=data.get("meeting_id") or "—",
                attempted_ts=fmt_attempt_ts(data.get("attempted_at")),
                reason_ru=reason_ru(code),
                code=code or "—",
                attempts=data.get("attempts", "—"),
                action=action_ru(code),
                job_id=data.get("job_id", ""),
                job_url=data.get("job_url") or "",
                vnc_url=data.get("vnc_url") or "",
                screenshot_ok=screenshot_exists(n),
            )
            html = _env.get_template("job_failed.html").render(
                rows=[
                    ("Встреча", ctx["topic"]),
                    ("meeting_id", ctx["meeting_id"]),
                    ("Время попытки", ctx["attempted_ts"]),
                    ("Причина отказа", f"{ctx['reason_ru']} ({ctx['code']})"),
                    ("Попыток", str(ctx["attempts"])),
                ],
                action=ctx["action"], job_url=ctx["job_url"],
                vnc_url=ctx["vnc_url"],
                screenshot_ok=ctx["screenshot_ok"])
            text = _env.get_template("job_failed.txt").render(**ctx)
            return subject, html, text
        if kind == "protocol_ready":
            html = _env.get_template("protocol_ready.html").render(
                topic=_topic(data),
                job_url=data.get("job_url") or "",
                audio_url=data.get("audio_url") or "")
            msg = (f"Встреча: {_topic(data)}\n"
                   f"Протокол: {data.get('job_url') or '—'}")
            if data.get("audio_url"):
                msg += f"\nАудиозапись: {data['audio_url']}"
            text = _env.get_template("generic.txt").render(title="Протокол готов",
                                                           message=msg)
            return subject, html, text
        if kind == "awaiting_llm":
            html = _env.get_template("awaiting_llm.html").render(
                topic=_topic(data), job_url=data.get("job_url") or "")
            text = _env.get_template("generic.txt").render(
                title="Стенограмма готова, LLM недоступен",
                message=f"Встреча: {_topic(data)}\n"
                        "Стенограмма сформирована, структурирование отложено.\n"
                        "Донастройте LLM и запустите повторную обработку: "
                        f"{data.get('job_url') or '—'}")
            return subject, html, text
        if kind == "confirm_end":
            message = (data.get("message")
                       or "Плановое время окончания встречи прошло, а уровень звука "
                          "измерить не удалось — непонятно, идёт ли совещание. "
                          "Запись продолжается.")
            rows = [("Встреча", _topic(data))]
            if data.get("reason"):
                rows.append(("Почему спрашиваем", data.get("reason")))
            html = _env.get_template("confirm_end.html").render(
                message=message, rows=rows,
                confirm_url=data.get("confirm_url") or "",
                vnc_url=data.get("vnc_url") or "",
                screenshot_ok=screenshot_exists(n))
            text_bits = [f"Встреча: {_topic(data)}", message]
            if data.get("reason"):
                text_bits.append(f"Причина: {data['reason']}")
            if data.get("confirm_url"):
                text_bits.append(f"Завершить запись: {data['confirm_url']}")
            if data.get("vnc_url"):
                text_bits.append(f"Экран совещания: {data['vnc_url']}")
            text_bits.append("Запись продолжается, пока вы не подтвердите завершение.")
            if screenshot_exists(n):
                text_bits.append("Скриншот экрана — во вложении.")
            return subject, html, "\n".join(text_bits) + "\n"
        if kind in ("alert", "job_export_failed"):
            message = data.get("message") or body
            rows = []
            if data.get("topic"):
                rows.append(("Встреча", data.get("topic")))
            if data.get("job_id"):
                rows.append(("Задача", str(data.get("job_id"))))
            html = _env.get_template("alert.html").render(
                message=message, rows=rows, job_url=data.get("job_url") or "",
                vnc_url=data.get("vnc_url") or "")
            text = f"{title}\n\n{message}\n"
            if data.get("vnc_url"):
                text += f"Экран совещания (noVNC): {data['vnc_url']}\n"
            return subject, html, text
        if kind == "digest":
            items = data.get("items") or []
            if items:
                html = _env.get_template("digest.html").render(items=items)
                bits = ["Сводка событий Mnema", ""]
                for it in items:
                    bits.append(f"- {it.get('subject', '')}")
                    if it.get("body"):
                        bits.append(str(it["body"]).rstrip())
                return subject, html, "\n".join(bits) + "\n"
    except Exception:  # noqa: BLE001 — письмо должно уйти даже битым шаблоном
        pass
    html = _env.get_template("fallback.html").render(title=title, body=body)
    return subject, html, f"{title}\n\n{body}\n"
