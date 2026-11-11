"""Skvoznoy test konveyera (FT-34) na sintezirovannom audio bez vneshney seti.

Tsepochka: raw.webm -> wav/mp3 (ffmpeg) -> VAD (energy fallback) -> diar off ->
ASR stub (niedostupen -> pustoy tekst) -> segmenty -> review requests (ne
raspoznany golos) -> steno.md -> LLM not_configured -> awaiting_llm + Protocol.
"""
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, StaticPool

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    import mnema.core.models  # registraciya tablits v metadannyh
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("DIARIZER", "off")          # bez pyannote
    monkeypatch.setenv("VAD", "energy")             # sintez. ton — ne rech' dlya silero
    monkeypatch.setenv("ASR_BASE_URL", "")         # not_configured
    monkeypatch.setenv("LLM_BASE_URL", "")         # not_configured
    monkeypatch.setenv("HF_TOKEN", "")
    return tmp_path


def _make_webm(path: Path, seconds: int = 6):
    """'Rechepodobnyy' signal: 60% tihi + 40% ton (energy-VAD dolzhen nayti 2+ segm)."""
    import math
    import struct as st
    import wave
    sr = 16000
    burst, gap = 0.8, 1.2
    n_burst, n_gap = int(burst * sr), int(gap * sr)
    frames = []
    total = 0
    while total < int(seconds * sr):
        for i in range(n_burst):
            frames.append(int(8000 * math.sin(2 * math.pi * 220 * i / sr)))
        for _ in range(n_gap):
            frames.append(0)
        total += n_burst + n_gap
    wav = path.with_suffix(".src.wav")
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(st.pack(f"<{len(frames)}h", *frames))
    subprocess.run(["ffmpeg", "-y", "-i", str(wav), "-ac", "1", "-ar", "48000",
                    "-c:a", "libopus", str(path)], check=True, capture_output=True)
    wav.unlink(missing_ok=True)


def _mkjob():
    from mnema.core.jobs_api import create_job
    job = create_job(meeting_id="444333222", topic="Ochered'")
    # dovedem do processing (kak api/finished delayut)
    from mnema.core.pipeline import transition
    transition(job.id, "launching")
    transition(job.id, "recording")
    transition(job.id, "processing")
    return job


def test_pipeline_full_offline(env):
    from mnema.worker.tasks import _job_paths, run_asr_pipeline
    job = _mkjob()
    p = _job_paths(job.id)
    _make_webm(p["webm"])

    run_asr_pipeline(job.id)

    # wav/mp3 sozdany (FT-6/FT-41)
    assert p["wav"].exists() and p["wav"].stat().st_size > 44
    assert p["mp3"].exists()
    # segmenty sozdany (VAD nasel 'rech')
    from sqlalchemy import select
    from mnema.core.db import get_session
    from mnema.core.models import (AudioFile, Job, Protocol, ReviewRequest,
                                   Segment)
    with get_session() as s:
        segs = s.scalars(select(Segment).where(Segment.job_id == job.id)).all()
        assert segs, "VAD ne nashel segmentov"
        for seg in segs:
            assert seg.speaker_source == "unresolved"
        rrs = s.scalars(select(ReviewRequest).where(ReviewRequest.job_id == job.id)).all()
        assert rrs, "net review-request dlya neraspoznannyh golosov"
        j = s.get(Job, job.id)
        # golosa ne raspznany -> stop v awaiting_review DO LLM-analiza; steno.md yest'
        assert j.status == "awaiting_review"
        proto = s.scalars(select(Protocol).where(Protocol.job_id == job.id)).first()
        assert proto is None  # LLM eshche ne zapuskalsya
        assert Path(p["steno"]).exists()
        afs = s.scalars(select(AudioFile).where(AudioFile.job_id == job.id)).all()
        kinds = {a.kind for a in afs}
        assert {"wav", "mp3"} <= kinds


def test_pipeline_idempotent_rerun(env):
    """HT-2: povtor processing ne udvoyaet segmenty/review."""
    from mnema.worker.tasks import _job_paths, run_asr_pipeline
    job = _mkjob()
    p = _job_paths(job.id)
    _make_webm(p["webm"])
    run_asr_pipeline(job.id)
    from sqlalchemy import select
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, Segment
    with get_session() as s:
        n1 = len(s.scalars(select(Segment).where(Segment.job_id == job.id)).all())
        # vozvrat v processing iz awaiting_llm (dopustimo) i povtor
        from mnema.core.pipeline import transition
        transition(job.id, "processing")
    run_asr_pipeline(job.id)
    with get_session() as s:
        n2 = len(s.scalars(select(Segment).where(Segment.job_id == job.id)).all())
        rrs = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == job.id,
            ReviewRequest.status == "pending")).all()
        assert n1 == n2, "segmenty udvoilis' pri povtore"
        clusters = {r.cluster_label for r in rrs}
        assert len(clusters) == len(rrs), "dubl review-request po klasteru"


def test_steno_format(env):
    from mnema.worker.tasks import _job_paths, run_asr_pipeline, _write_steno
    job = _mkjob()
    p = _job_paths(job.id)
    _make_webm(p["webm"])
    run_asr_pipeline(job.id)
    text = p["steno"].read_text(encoding="utf-8")
    assert text.startswith("# Протокол (транскрипт)")
    assert "Голос" in text  # imena iz golosovogo mehanizma (unresolved -> Голос N)


def test_transition_failed_notifies_with_data_and_screenshot(env, monkeypatch):
    """Perekhod v failed sozdaet Notification s data_json i putem skrinshota."""
    import json

    monkeypatch.setenv("NOTIFY_EMAIL_TO", "ops@example.ru")
    from mnema.core.db import get_session
    from mnema.core.models import Notification
    from mnema.core.pipeline import transition

    job = _mkjob()
    shot_dir = env / "failure_screenshots"
    shot_dir.mkdir()
    shot = shot_dir / f"job_{job.id}.png"
    shot.write_bytes(b"\x89PNG fake")
    transition(job.id, "failed", fail_reason="timeout", error="boom",
               screenshot_path=str(shot))
    with get_session() as s:
        n = s.query(Notification).filter_by(kind="job_failed").one()
        assert n.status == "pending"
        assert "Не удалось подключиться" in n.subject
        assert n.screenshot_path == str(shot.resolve())
        data = json.loads(n.data_json)
        assert data["fail_reason"] == "timeout"
        assert data["job_id"] == job.id


def test_transition_done_notifies_with_protocol_url(env, monkeypatch):
    """Perekhod v done: knopka 'Protokol' sylaetsya na /protocols/{id}, ne na /jobs/."""
    import json

    monkeypatch.setenv("NOTIFY_EMAIL_TO", "ops@example.ru")
    monkeypatch.setenv("MNEMA_PUBLIC_URL", "http://example.internal:8000")
    from sqlalchemy import select
    from mnema.core.db import get_session
    from mnema.core.models import Notification, Protocol
    from mnema.core.pipeline import transition

    job = _mkjob()
    with get_session() as s:
        s.add(Protocol(job_id=job.id, json_data={"summary": "ok"}))
        s.commit()
        pid = s.scalars(select(Protocol).where(Protocol.job_id == job.id)).first().id
    transition(job.id, "done")
    with get_session() as s:
        n = s.query(Notification).filter_by(kind="protocol_ready").one()
        data = json.loads(n.data_json)
        assert data["protocol_url"] == f"http://example.internal:8000/protocols/{pid}"
        assert data["job_url"] == f"http://example.internal:8000/jobs/{job.id}"
        assert f"http://example.internal:8000/protocols/{pid}" in n.body
