"""Testy zagruzki arhivnyh zapisey soveshchaniy (video/audio) na vstreche.

Resumable-chunks: sozdanie, dokleyka, 409 pri raskhozhdenii offset, resume
chelerez /offset, finalize s validaciey audio, limity i otkhmena.
Redis niedostupen - enqueue v direct-mode no-op (kak v test_api_integration).
"""
import datetime as dt
import os
import struct
import wave

import pytest
from sqlalchemy import StaticPool, create_engine

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive")
os.environ.setdefault("DATA_DIR", "/tmp/test-data")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-uploads.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
os.environ["LLM_BASE_URL"] = ""
os.environ["ASR_BASE_URL"] = ""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    with TestClient(app) as c:
        yield c


def _wav_bytes(seconds: float = 1.0, sr: int = 16000) -> bytes:
    from io import BytesIO
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack(f"<{int(sr * seconds)}h", *([100] * int(sr * seconds))))
    return buf.getvalue()


def test_create_upload_makes_archive_job(client, monkeypatch):
    monkeypatch.setenv("UPLOAD_MAX_GB", "1")
    r = client.post("/api/uploads", json={"filename": "planerka.mp4",
                                          "bytes_total": 1234,
                                          "topic": "Planerka 12.09"})
    assert r.status_code == 200
    jid, uid = r.json()["job_id"], r.json()["upload_id"]
    g = client.get(f"/api/jobs/{jid}").json()
    assert g["status"] == "uploading"
    assert g["zoom_meeting_id"] is None


def test_upload_rejects_bad_extension(client):
    r = client.post("/api/uploads", json={"filename": "virus.exe",
                                          "topic": "T", "bytes_total": 10})
    assert r.status_code == 400


def test_upload_rejects_missing_topic(client):
    r = client.post("/api/uploads", json={"filename": "a.mp4", "bytes_total": 10})
    assert r.status_code == 400


def test_upload_rejects_too_large(client, monkeypatch):
    monkeypatch.setenv("UPLOAD_MAX_GB", "0.0001")
    r = client.post("/api/uploads", json={"filename": "big.mkv",
                                          "topic": "T",
                                          "bytes_total": 700 * 1024 * 1024})
    assert r.status_code == 413


def test_chunk_happy_path_and_offset_resume(client):
    data = _wav_bytes(1.0)
    r = client.post("/api/uploads", json={"filename": "m.wav",
                                          "topic": "T",
                                          "bytes_total": len(data)})
    uid = r.json()["upload_id"]
    half = len(data) // 2
    r1 = client.post(f"/api/uploads/{uid}/chunk?offset=0", content=data[:half])
    assert r1.status_code == 200 and r1.json()["offset"] == half
    # client "oborvalsya" - sprashivaem offset i prodolzhaem
    off = client.get(f"/api/uploads/{uid}/offset").json()["offset"]
    assert off == half
    r2 = client.post(f"/api/uploads/{uid}/chunk?offset={off}", content=data[half:])
    assert r2.status_code == 200 and r2.json()["offset"] == len(data)


def test_chunk_mismatched_offset_409(client):
    data = _wav_bytes(1.0)
    uid = client.post("/api/uploads", json={"filename": "m.wav", "topic": "T",
                                            "bytes_total": len(data)}).json()["upload_id"]
    r = client.post(f"/api/uploads/{uid}/chunk?offset=999", content=data[:10])
    assert r.status_code == 409


def test_finalize_requires_audio_or_fails(client, tmp_path, monkeypatch):
    # validniy wav -> finalize v processing
    data = _wav_bytes(1.0)
    uid = client.post("/api/uploads", json={"filename": "ok.wav", "topic": "T",
                                            "bytes_total": len(data)}).json()["upload_id"]
    client.post(f"/api/uploads/{uid}/chunk?offset=0", content=data)
    ffprobe = os.system("which ffprobe >/dev/null 2>&1")
    r = client.post(f"/api/uploads/{uid}/finalize")
    if ffprobe == 0:
        assert r.status_code == 200
        assert r.json()["status"] == "processing"
    else:
        assert r.status_code == 400  # bez ffprobe validatsiya nevozmozhna


def test_finalize_rejects_truncated_file(client):
    data = _wav_bytes(1.0)
    uid = client.post("/api/uploads", json={"filename": "cut.wav", "topic": "T",
                                            "bytes_total": len(data)}).json()["upload_id"]
    client.post(f"/api/uploads/{uid}/chunk?offset=0", content=data[:50])
    r = client.post(f"/api/uploads/{uid}/finalize")
    assert r.status_code == 400


def test_audio_source_served_after_finalize(client):
    """Audio dorozhka zagruzhennoy vstrechi dastupna kak u zapisannoy (FT-41)."""
    data = _wav_bytes(1.0)
    jid = client.post("/api/uploads", json={"filename": "s.ogg", "topic": "T",
                                            "bytes_total": len(data)}).json()["job_id"]
    uid = client.post("/api/uploads", json={"filename": "s2.wav", "topic": "T2",
                                            "bytes_total": len(data)}).json()["upload_id"]
    client.post(f"/api/uploads/{uid}/chunk?offset=0", content=data)
    ffprobe = os.system("which ffprobe >/dev/null 2>&1")
    if ffprobe != 0:
        pytest.skip("nuzhen ffprobe")
    uid2 = client.post("/api/uploads", json={"job_id": None, "filename": "v.wav",
                                             "topic": "T3",
                                             "bytes_total": len(data)}).json()["upload_id"]
    r = client.post(f"/api/uploads/{uid2}/chunk?offset=0", content=data)
    r = client.post(f"/api/uploads/{uid2}/finalize")
    assert r.status_code == 200
    jid = r.json()["job_id"]
    # razdacha ishohnika s Range
    r1 = client.get(f"/api/jobs/{jid}/source", headers={"range": "bytes=0-3"})
    assert r1.status_code == 206 and len(r1.content) == 4


def test_fsm_upload_transitions(client):
    from mnema.core.pipeline import ALLOWED_TRANSITIONS, transition
    from mnema.core.db import get_session
    from mnema.core.models import Job
    assert "uploading" in ALLOWED_TRANSITIONS["scheduled"]
    assert "processing" in ALLOWED_TRANSITIONS["uploading"]
    assert "uploading" in ALLOWED_TRANSITIONS["failed"]
    jid = client.post("/api/uploads", json={"filename": "f.mp4", "topic": "T",
                                            "bytes_total": 10}).json()["job_id"]
    # uploading -> processing (legcen finalize)
    transition(jid, "processing")
    with get_session() as s:
        assert s.get(Job, jid).status == "processing"


def test_worker_extracts_audio_from_video(tmp_path, monkeypatch):
    """Shag 4: worker izvlekayet meeting.wav/mp3 iz arhivnogo video (bez ASR/AI)."""
    import subprocess
    import shutil as _sh
    if _sh.which("ffmpeg") is None or _sh.which("ffprobe") is None:
        pytest.skip("nuzhen ffmpeg/ffprobe")
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, get_session, set_engine_for_test
    import mnema.core.models  # noqa: F401
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from mnema.core.models import AudioFile, Job
    with get_session() as s:
        job = Job(zoom_meeting_id=None, source="upload", topic="Vid", status="processing")
        s.add(job); s.commit(); s.refresh(job)
        jid = job.id
    p = {}
    from mnema.worker.tasks import _job_paths, _prepare_wav
    p = _job_paths(jid)
    raw = p["dir"] / "raw.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-f", "lavfi", "-i",
                    "testsrc=duration=2:size=128x72:rate=10",
                    "-c:a", "aac", "-c:v", "libx264", str(raw)],
                   check=True, capture_output=True)
    _prepare_wav(jid, p)
    assert p["wav"].exists() and p["wav"].stat().st_size > 44
    assert p["mp3"].exists()
    with get_session() as s:
        kinds = {a.kind for a in s.query(AudioFile).filter(AudioFile.job_id == jid)}
        assert "wav" in kinds and "mp3" in kinds


def test_keep_uploaded_source_false_removes_raw(tmp_path, monkeypatch):
    import subprocess
    import shutil as _sh
    if _sh.which("ffmpeg") is None or _sh.which("ffprobe") is None:
        pytest.skip("nuzhen ffmpeg/ffprobe")
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, get_session, set_engine_for_test
    import mnema.core.models  # noqa: F401
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KEEP_UPLOADED_SOURCE", "0")
    from mnema.core.models import AudioFile, Job
    from mnema.worker.tasks import _job_paths, _maybe_drop_raw_source, _prepare_wav
    with get_session() as s:
        job = Job(zoom_meeting_id=None, source="upload", topic="K", status="processing")
        s.add(job); s.commit(); s.refresh(job)
        jid = job.id
    p = _job_paths(jid)
    raw = p["dir"] / "raw.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:a", "aac", str(raw)],
                   check=True, capture_output=True)
    from mnema.core.models import AudioFile as _AF
    with get_session() as s:
        s.add(_AF(job_id=jid, kind="mp4", path=str(raw), bytes=raw.stat().st_size))
        s.commit()
    _prepare_wav(jid, p)
    _maybe_drop_raw_source(jid, p)
    assert not raw.exists()
    assert p["wav"].exists() and p["mp3"].exists()
    with get_session() as s:
        assert s.query(_AF).filter(_AF.job_id == jid, _AF.kind == "mp4").first() is None


def test_cancel_upload_cancels_job(client):
    uid = client.post("/api/uploads", json={"filename": "c.webm", "topic": "T",
                                            "bytes_total": 100}).json()["upload_id"]
    client.post(f"/api/uploads/{uid}/chunk?offset=0", content=b"x" * 40)
    r = client.post(f"/api/uploads/{uid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


def test_cancel_job_via_api_cleans_upload(client):
    """FIX: DELETE /api/jobs/{id} (cancel_job) otmenyaet i upload-vstrechu,
    udalyaya vremennyy fayl zagruzki."""
    from mnema.core.db import get_session
    from mnema.core.models import Upload
    created = client.post("/api/uploads", json={"filename": "k.mov", "topic": "T",
                                                "bytes_total": 100}).json()
    jid, uid = created["job_id"], created["upload_id"]
    client.post(f"/api/uploads/{uid}/chunk?offset=0", content=b"y" * 40)
    with get_session() as s:
        tmp = s.get(Upload, uid).path
    assert os.path.exists(tmp)
    r = client.delete(f"/api/jobs/{jid}")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert not os.path.exists(tmp)


def test_active_upload_for_resume(client):
    data = _wav_bytes(1.0)
    created = client.post("/api/uploads", json={"filename": "r.mp4", "topic": "T",
                                                "bytes_total": len(data)}).json()
    jid, uid = created["job_id"], created["upload_id"]
    # srazu posle sozdaniya est' aktivnaya zagruzka s offset=0
    act0 = client.get(f"/api/jobs/{jid}/uploads/active").json()
    assert act0["upload_id"] == uid and act0["offset"] == 0
    client.post(f"/api/uploads/{uid}/chunk?offset=0", content=data[:30])
    act = client.get(f"/api/jobs/{jid}/uploads/active").json()
    assert act["upload_id"] == uid and act["offset"] == 30


def test_upload_to_zoom_job_rejected(client):
    jid = client.post("/api/jobs", json={"meeting_id": "111222333"}).json()["id"]
    r = client.post("/api/uploads", json={"job_id": jid, "filename": "a.mp4",
                                          "bytes_total": 10})
    assert r.status_code == 400


def test_gc_stale_uploads(client, monkeypatch):
    from mnema.core.db import get_session
    from mnema.core.models import Upload
    from mnema.core.uploads import gc_stale_uploads
    uid = client.post("/api/uploads", json={"filename": "g.mp4", "topic": "T",
                                            "bytes_total": 100}).json()["upload_id"]
    with get_session() as s:
        up = s.get(Upload, uid)
        import datetime as _dt
        up.created_ts = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48)
        up.updated_ts = up.created_ts
        s.commit()
    assert gc_stale_uploads(max_age_hours=24) == 1
    with get_session() as s:
        assert s.get(Upload, uid).status == "failed"


def test_upload_page_and_jobs_card_render(client):
    r = client.get("/jobs/upload")
    assert r.status_code == 200
    assert "Загрузить" in r.text
    r2 = client.get("/jobs")
    assert "Загрузить архивную запись совещания" in r2.text


def test_chunk_creates_and_finalize_drops_upload_lock(client):
    """FIX: lock-fayl serializatsii ne dolzhen kopitsya v uploads/ —
    finalize (i so sboyom probe) schitaetsya terminal'nym i ubiraet ego."""
    from mnema.core.uploads import _upload_lock_path
    data = _wav_bytes(1.0)
    uid = client.post("/api/uploads", json={"filename": "lk.wav", "topic": "T",
                                            "bytes_total": len(data)}).json()["upload_id"]
    client.post(f"/api/uploads/{uid}/chunk?offset=0", content=data)
    assert _upload_lock_path(uid).exists()
    r = client.post(f"/api/uploads/{uid}/finalize")
    assert r.status_code in (200, 400)  # zavisit ot nalichiya ffprobe
    assert not _upload_lock_path(uid).exists()


def test_cancel_drops_upload_lock(client):
    from mnema.core.uploads import _upload_lock_path
    uid = client.post("/api/uploads", json={"filename": "clk.webm", "topic": "T",
                                            "bytes_total": 100}).json()["upload_id"]
    client.post(f"/api/uploads/{uid}/chunk?offset=0", content=b"x" * 40)
    assert _upload_lock_path(uid).exists()
    assert client.post(f"/api/uploads/{uid}/cancel").status_code == 200
    assert not _upload_lock_path(uid).exists()


def test_gc_stale_uploads_drops_upload_lock(client, monkeypatch):
    from mnema.core.db import get_session
    from mnema.core.models import Upload
    from mnema.core.uploads import _upload_lock_path, gc_stale_uploads
    uid = client.post("/api/uploads", json={"filename": "glk.mp4", "topic": "T",
                                            "bytes_total": 100}).json()["upload_id"]
    lock = _upload_lock_path(uid)
    client.post(f"/api/uploads/{uid}/chunk?offset=0", content=b"z" * 10)
    with get_session() as s:
        up = s.get(Upload, uid)
        import datetime as _dt
        up.created_ts = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=48)
        up.updated_ts = up.created_ts
        s.commit()
    assert lock.exists()
    assert gc_stale_uploads(max_age_hours=24) == 1
    assert not lock.exists()
