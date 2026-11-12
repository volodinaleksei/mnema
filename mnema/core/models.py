"""Modeli BDD po TZ 6.2."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float,
                        ForeignKey, Integer, String, Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

JOB_STATUSES = ("scheduled", "launching", "recording", "processing",
                "awaiting_llm", "awaiting_review", "done", "failed",
                "cancelled", "uploading")
SPEAKER_SOURCES = ("voiceprint", "unresolved", "multi_voice", "voice_mix")
JOB_SOURCES = ("zoom", "upload", "split")
UPLOAD_STATUSES = ("pending", "uploading", "complete", "failed")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zoom_meeting_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="zoom")  # zoom | upload | split
    join_link: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    pwd_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    topic: Mapped[str] = mapped_column(String(512), default="")
    start_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_max_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="api")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    join_attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    review_deadline: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # pyannote-turns [[t_start, t_end, label], ...] — границы говорящих для
    # чистых (без наложения) диапазонов эмбеддингов при enroll/backfill
    diar_turns: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # выгрузка в самба-зеркало: {stem, dir, audio, protocol_md, protocol_docx, attempts}
    export_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # разделение одной записи на несколько встреч: потомок ссылается на родителя
    parent_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"),
                                                      nullable=True, index=True)
    # {boundaries: [{t, confidence, topic_before, topic_after}], model, skipped, ts}
    split_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # P3-17: UID события из импортированного .ics — дедуп повторных импортов
    # одного и того же календаря (ICS-UID уникален в пределах календаря)
    ics_uid: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    events: Mapped[list["SessionEvent"]] = relationship(back_populates="job")
    audio_files: Mapped[list["AudioFile"]] = relationship(back_populates="job")
    uploads: Mapped[list["Upload"]] = relationship(back_populates="job")
    segments: Mapped[list["Segment"]] = relationship(back_populates="job")
    protocol: Mapped["Protocol | None"] = relationship(back_populates="job", uselist=False)


class Upload(Base):
    """Zagruzka arhivnoy zapisi (video/audio) na vstreche source=upload.

    Vozymlayemaya (resumable): chunki dokleyivayutsya v vremennyiy file;
    finalize validiruet audio-dorozhku i zapuskaet konveyer."""
    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    path: Mapped[str] = mapped_column(String(1024))
    bytes_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True),
                                                    default=_utcnow, onupdate=_utcnow)

    job: Mapped[Job] = relationship(back_populates="uploads")


class PresetLink(Base):
    """Predopredelennaya ssylka Zoom s parolem i opisaniem (vybor iz menyu v WEB)."""
    __tablename__ = "preset_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255))  # opisanie/dlya menyu
    meeting_link: Mapped[str] = mapped_column(String(1024))
    pwd_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SessionEvent(Base):
    __tablename__ = "session_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    event: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped[Job] = relationship(back_populates="events")


class AudioFile(Base):
    __tablename__ = "audio_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # wav | webm | mp3
    path: Mapped[str] = mapped_column(String(1024))
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="audio_files")


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    speaker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # diar cluster label
    speaker_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    speaker_source: Mapped[str] = mapped_column(String(16), default="unresolved")
    diar_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_track: Mapped[str | None] = mapped_column(String(64), nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    t_start: Mapped[float] = mapped_column(Float)
    t_end: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text, default="")
    lang: Mapped[str | None] = mapped_column(String(8), nullable=True)
    engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ASR-сегменты внутри VAD-строки: [{s, e, t}] — точные тайминги фраз для
    # разбиения строки по границам говорящих (diar turns) при выводе
    asr_json: Mapped[list | None] = mapped_column(JSON, nullable=True)

    job: Mapped[Job] = relationship(back_populates="segments")


class Protocol(Base):
    __tablename__ = "protocols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True, index=True)
    json_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    md_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    docx_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    steno_md_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="protocol")


class VoiceProfile(Base):
    __tablename__ = "voice_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_name: Mapped[str] = mapped_column(String(255))
    employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    # набор эталонных эмбеддингов (по источнику: запись/кластер встречи);
    # матчинг — max cosine по ним (multi-prototype), embedding — среднее
    ref_embeddings: Mapped[list[list[float]] | None] = mapped_column(JSON, nullable=True)
    sample_paths: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    consent_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    match_stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ReviewRequest(Base):
    __tablename__ = "review_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    cluster_label: Mapped[str] = mapped_column(String(64))
    candidate_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("voice_profiles.id"), nullable=True)
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    answered_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answered_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConfirmEndRequest(Base):
    """Запрос ручного подтверждения завершения записи (безопасный выход).

    web-bot не уверен, есть ли звук после планового окончания встречи —
    вместо автовыхода создаёт запрос: письмо с кнопками «Завершить» (GET
    /confirm-end/<token>) и «Посмотреть экран» (noVNC) + скриншот. Запись
    продолжается, пока оператор не ответит или запрос не истекает."""
    __tablename__ = "confirm_end_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending",
                                        index=True)  # pending|confirmed|cancelled|expired
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    responded_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SessionHealth(Base):
    __tablename__ = "session_health"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checked_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ok: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class Notification(Base):
    """Ochered'/zhurnal uedomebleniy (FT-35): zhivёt restartz (v BDD i redis)."""
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)  # job_failed|protocol_ready|...
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    subject: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # strukt. dannye pisma (JSON)
    screenshot_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    to_addr: Mapped[str] = mapped_column(String(255))
    cc_addr: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|sent|digest|suppressed|error
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sent_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PendingCommand(Base):
    """P3-18: fallback-kanal komand (stop/salvage) cherez BDD pri nedostupnom
    redis. Zapisyvaetsya vmesto tishogo loss; maintenance_job (i start api)
    povtaryayet push, kogda redis ozhil. Idempotentnost' na storone web-bot
    (recorders/inFlight) ne pozvolyayet dublyam navredit'."""
    __tablename__ = "pending_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cmd: Mapped[str] = mapped_column(String(32), index=True)  # stop|salvage
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending",
                                        index=True)  # pending|pushed|expired
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    pushed_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
