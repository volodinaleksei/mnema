"""Kachestvo golosovogo konveyera: SNR-geyt FT-29a, coverage-diari, max-cosine,
otsutstvie hash-musora v zhivyh putyah (K1/K2), multi prototipy profilya.

Redis/HF niedostupny: pyannote monkeypatch-atsya, VAD energy, real'nyy ffmpeg
dlya sinteza i SNR-ocenki.
"""
import datetime as dt
import math
import os
import struct
import wave
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine, select

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive-vq")
os.environ.setdefault("DATA_DIR", "/tmp/test-data-vq")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-vq.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # zavedomo niet
os.environ["LLM_BASE_URL"] = ""
os.environ["ASR_BASE_URL"] = ""


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    return tmp_path


def _mk_wav(path: Path, seconds: float, sample_fn, sr: int = 16000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        n = int(seconds * sr)
        w.writeframes(b"".join(struct.pack("<h", int(sample_fn(i / sr)))
                               for i in range(n)))
    return path


def _loud_speech_wav(path: Path, seconds: float = 2.0) -> Path:
    """Gromkiy signal s pauzami: vysokaya rechevaya energiya i vysokaya noys/rotos."""
    def f(t):
        phase = (t * 0.9) % 1.0
        if phase < 0.5:
            return 8000 * math.sin(2 * math.pi * 220 * t)
        return 0
    return _mk_wav(path, seconds, f)


def _dead_silent_wav(path: Path) -> Path:
    return _mk_wav(path, 2.0, lambda t: 0)


def _hum_flat_wav(path: Path) -> Path:
    """Noys-podobnyy signal s pochti postoyannoy energией: SNR ~ 0 dB."""
    return _mk_wav(path, 2.0, lambda t: 100 * math.sin(2 * math.pi * 50 * t)
                   + 50 * math.sin(2 * math.pi * 137 * t))


# ---- quality_gate (FT-29a: dlina + SNR/uroven') -------------------------------
def test_quality_gate_rejects_dead_mic(db):
    from mnema.core.speaker import quality_gate
    p = _dead_silent_wav(Path(os.environ["DATA_DIR"]) / "dead.wav")
    ok, reason = quality_gate(p)
    assert not ok and "tih" in reason


def test_quality_gate_rejects_low_snr(db):
    from mnema.core.speaker import quality_gate
    p = _hum_flat_wav(Path(os.environ["DATA_DIR"]) / "hum.wav")
    ok, reason = quality_gate(p)
    assert not ok and "shum" in reason


def test_quality_gate_accepts_loud_speech(db):
    from mnema.core.speaker import quality_gate
    p = _loud_speech_wav(Path(os.environ["DATA_DIR"]) / "good.wav")
    ok, reason = quality_gate(p)
    assert ok, reason


def test_quality_gate_rejects_short(db):
    from mnema.core.speaker import quality_gate
    p = _loud_speech_wav(Path(os.environ["DATA_DIR"]) / "short.wav", 0.5)
    ok, reason = quality_gate(p)
    assert not ok and "korotko" in reason


# ---- K2: bez pyannote / pri otkazhe — None, ne hash-zaglushka ------------------
def test_compute_embedding_none_without_backend(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: None)
    from mnema.core.speaker import compute_embedding_for_file
    p = _loud_speech_wav(Path(os.environ["DATA_DIR"]) / "x.wav")
    assert compute_embedding_for_file(p) is None


def test_compute_embedding_none_when_pyannote_raises(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend",
                        lambda: "pyannote")

    def boom(path):
        raise RuntimeError("cuda oom")
    monkeypatch.setattr("mnema.core.speaker._pyannote_embedding", boom)
    from mnema.core.speaker import compute_embedding_for_file
    p = _loud_speech_wav(Path(os.environ["DATA_DIR"]) / "y.wav")
    assert compute_embedding_for_file(p) is None


# ---- V1: dominant coverage vmesto serediny -------------------------------------
TURNS = [[0.0, 5.0, "A"], [5.0, 10.0, "B"]]


def test_dominant_label_picks_covering_speaker():
    from mnema.core.diarize import label_at_turns
    label, cov = label_at_turns(TURNS, 4.0, 6.0)   # porovnu 50/50
    assert label in ("A", "B") and cov == pytest.approx(0.5)
    label, cov = label_at_turns(TURNS, 0.2, 4.8)
    assert label == "A" and cov == pytest.approx(1.0)


def test_dominant_label_flags_overlap():
    from mnema.core.diarize import label_at_turns
    _l, cov = label_at_turns(TURNS, 2.0, 8.0)      # poly intervala v chuzhom turne
    assert cov < 0.7  # takoy fragment — v embedding nel'zya


def test_dominant_label_no_coverage():
    from mnema.core.diarize import label_at_turns
    assert label_at_turns([], 0.0, 1.0) == (None, 0.0)


def test_run_speaker_id_marks_overlap_multi_voice(db, tmp_path, monkeypatch):
    import mnema.core.models as m
    from mnema.core.db import get_session
    with get_session() as s:
        s.add(m.Job(id=1, topic="T", status="processing", source="upload"))
        s.add(m.Segment(job_id=1, t_start=1.0, t_end=9.0, text="x",
                        speaker_source="unresolved"))   # styk A/B
        s.add(m.Segment(job_id=1, t_start=0.2, t_end=2.0, text="y",
                        speaker_source="unresolved"))   # chisty A
        s.commit()
    d = tmp_path / "jobs" / "1"
    d.mkdir(parents=True, exist_ok=True)
    wav = d / "meeting.wav"
    _loud_speech_wav(wav)
    monkeypatch.setattr("mnema.core.diarize.diarize_with_turns",
                        lambda wav, vad: (["A", "A"], [[0.0, 5.0, "A"],
                                                       [5.0, 10.0, "B"]],
                                          [0.5, 1.0]))
    monkeypatch.setattr("mnema.worker.tasks._run_speaker", lambda job_id: None)
    from mnema.worker.tasks import run_speaker_id
    run_speaker_id(1)
    with get_session() as s:
        segs = s.scalars(select(m.Segment).where(m.Segment.job_id == 1)
                         .order_by(m.Segment.t_start)).all()
        assert segs[0].speaker_source == "multi_voice" and segs[0].needs_review
        assert segs[1].speaker_source == "unresolved" and not segs[1].needs_review
        job = s.get(m.Job, 1)
        assert job.diar_turns == [[0.0, 5.0, "A"], [5.0, 10.0, "B"]]


# ---- multi_voice ne poluchaet imeni pri matsching/operatorom --------------------
def _speaker_cluster_env(monkeypatch, verdict, profile_id, sim):
    import mnema.core.speaker as sp
    monkeypatch.setattr("mnema.worker.tasks._cluster_embedding",
                        lambda wav, segs, turns=None: [1.0, 0.0])
    monkeypatch.setattr(sp, "match_cluster",
                        lambda emb: (verdict, _prof(profile_id), sim))


def _prof(pid):
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        return s.get(VoiceProfile, pid)


def _mkprofile(name):
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        v = VoiceProfile(employee_name=name,
                         consent_ts=dt.datetime.now(dt.timezone.utc))
        s.add(v)
        s.commit()
        s.refresh(v)
        return v.id


def test_multi_voice_segment_stays_unnamed_on_auto(db, monkeypatch):
    pid = _mkprofile("Avtomat A.A.")
    from mnema.core.db import get_session
    from mnema.core.models import Job, Segment
    with get_session() as s:
        s.add(Job(id=20, topic="t", status="processing", source="upload"))
        s.add(Segment(job_id=20, speaker_id="S0", diar_label="S0", t_start=0,
                      t_end=5, text="x", speaker_source="multi_voice",
                      needs_review=True))
        s.commit()
    _speaker_cluster_env(monkeypatch, "auto", pid, 0.9)
    from mnema.worker.tasks import _run_speaker
    _run_speaker(20)
    with get_session() as s:
        seg = s.scalars(select(Segment).where(Segment.job_id == 20)).first()
        assert seg.speaker_source == "multi_voice"
        assert seg.speaker_name is None and seg.needs_review is True


def test_voice_mix_segment_stays_unnamed_on_auto(db, monkeypatch):
    """Operatorskiy mikss dvukh golosov: imya ne prisvaivaetsya i segment
    bolshe ne zhdyot utochneniya."""
    pid = _mkprofile("Avtomat Mix Mix.")
    from mnema.core.db import get_session
    from mnema.core.models import Job, Segment
    with get_session() as s:
        s.add(Job(id=24, topic="t", status="processing", source="upload"))
        s.add(Segment(job_id=24, speaker_id="S0", diar_label="S0", t_start=0,
                      t_end=5, text="x", speaker_source="voice_mix",
                      needs_review=False))
        s.commit()
    _speaker_cluster_env(monkeypatch, "auto", pid, 0.9)
    from mnema.worker.tasks import _run_speaker
    _run_speaker(24)
    with get_session() as s:
        seg = s.scalars(select(Segment).where(Segment.job_id == 24)).first()
        assert seg.speaker_source == "voice_mix"
        assert seg.speaker_name is None and seg.needs_review is False


def test_voice_mix_stays_unnamed_when_answer_is_mixed(db):
    """Otvet operatora na klaster s voice_mix: imya ne prisvaivaetsya miksu,
    no drugie fragmenty klastera ego poluchayut."""
    from mnema.core.db import get_session
    from mnema.core.models import Job, ReviewRequest, Segment
    from mnema.core.speaker import apply_review_answer
    with get_session() as s:
        s.add(Job(id=26, topic="t", status="awaiting_review", source="upload"))
        rv = ReviewRequest(job_id=26, cluster_label="S0", status="pending")
        s.add(rv)
        s.add(Segment(job_id=26, speaker_id="S0", diar_label="S0", t_start=0,
                      t_end=5, text="miks", speaker_source="voice_mix",
                      speaker_name="Sidorov S. S.", needs_review=False))
        s.add(Segment(job_id=26, speaker_id="S0", diar_label="S0", t_start=6,
                      t_end=9, text="odin golos", speaker_source="unresolved",
                      needs_review=True))
        s.commit()
        rid = rv.id
    apply_review_answer(rid, None, "Ivanov I. I.", False, "web")
    with get_session() as s:
        mix = s.scalars(select(Segment).where(
            Segment.job_id == 26, Segment.text == "miks")).first()
        solo = s.scalars(select(Segment).where(
            Segment.job_id == 26, Segment.text == "odin golos")).first()
        assert mix.speaker_name is None and mix.speaker_source == "voice_mix"
        assert solo.speaker_name == "Ivanov I. I."


def test_run_speaker_does_not_reopen_mixed_review(db, monkeypatch):
    """Zakrytyy kak mixed review povtornoy matsching ne prevrashchaet v pending."""
    from mnema.core.db import get_session
    from mnema.core.models import Job, ReviewRequest, Segment
    with get_session() as s:
        s.add(Job(id=25, topic="t", status="processing", source="upload"))
        s.add(Segment(job_id=25, speaker_id="S0", diar_label="S0", t_start=0,
                      t_end=5, text="x", speaker_source="voice_mix",
                      needs_review=False))
        s.add(ReviewRequest(job_id=25, cluster_label="S0", status="mixed",
                            answered_by="web"))
        s.commit()
    _speaker_cluster_env(monkeypatch, "unresolved", 0, 0.1)
    from mnema.worker.tasks import _run_speaker
    _run_speaker(25)
    with get_session() as s:
        rrs = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == 25)).all()
        assert len(rrs) == 1 and rrs[0].status == "mixed"


def test_operator_locked_cluster_not_renamed_by_rematch(db, monkeypatch):
    """V5: novyy verdict (osle backfill) ne perekhodyaet imena v klastere s
    ruchnym otvetom operatora."""
    human = _mkprofile("Chelovek Ch.Ch.")
    wrong = _mkprofile("Nepohozhiy N.N.")
    from mnema.core.db import get_session
    from mnema.core.models import Job, ReviewRequest, Segment
    with get_session() as s:
        s.add(Job(id=21, topic="t", status="processing", source="upload"))
        s.add(Segment(job_id=21, speaker_id="S0", diar_label="S0", t_start=0,
                      t_end=5, text="x", speaker_name="Chelovek Ch.Ch.",
                      speaker_source="voiceprint"))
        s.add(ReviewRequest(job_id=21, cluster_label="S0",
                            candidate_profile_id=human, status="confirmed",
                            answered_by="web"))
        s.commit()
    _speaker_cluster_env(monkeypatch, "auto", wrong, 0.95)
    from mnema.worker.tasks import _run_speaker
    _run_speaker(21)
    with get_session() as s:
        seg = s.scalars(select(Segment).where(Segment.job_id == 21)).first()
        assert seg.speaker_name == "Chelovek Ch.Ch."
        rr = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == 21)).first()
        assert rr.candidate_profile_id == human


# ---- V2: max-cosine po ref-etalonom ---------------------------------------------
def test_profile_similarity_uses_max_over_refs():
    import math as _m
    from mnema.core.models import VoiceProfile
    from mnema.core.speaker import cosine, profile_similarity
    vp = VoiceProfile(employee_name="Set S.S.", embedding=[0.0, 1.0],
                      ref_embeddings=[[1.0, 0.0], [0.0, 1.0]])
    assert profile_similarity([1.0, 0.0], vp) == pytest.approx(1.0)
    assert profile_similarity([0.7, 0.7], vp) == pytest.approx(
        0.7 / _m.sqrt(0.98))
    # srednee embedding odno dalo by menshiy skore dlya vtorogo golosa
    assert cosine([1.0, 0.0], vp.embedding) < profile_similarity([1.0, 0.0], vp)


def test_match_cluster_auto_via_ref_only_profile(db, monkeypatch):
    """Profil' bez srednego embeddinga no s ref uzaetsya (max cosine)."""
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        s.add(VoiceProfile(employee_name="RefOnly R.R.", embedding=None,
                           ref_embeddings=[[0.0, 1.0]]))
        s.commit()
    monkeypatch.setenv("SPEAKER_T_CONFIRM", "0.65")
    monkeypatch.setenv("SPEAKER_T_HINT", "0.45")
    from mnema.core.speaker import match_cluster
    verdict, prof, sim = match_cluster([0.0, 1.0])
    assert verdict == "auto" and prof is not None and sim == pytest.approx(1.0)


# ---- S4: backfill-pereborka sohnyaet i zapisi, i klastery ------------------------
def test_recompute_collects_samples_and_clusters(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker.compute_embedding_for_file",
                        lambda path: [1.0, 0.0])
    monkeypatch.setattr("mnema.core.speaker_backfill.cluster_embedding_for",
                        lambda job_id, cluster: [0.0, 1.0])
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, VoiceProfile
    with get_session() as s:
        v = VoiceProfile(employee_name="Recomp R.K.")
        s.add(v)
        s.commit()
        vid = v.id
        s.add(ReviewRequest(job_id=31, cluster_label="S0",
                            candidate_profile_id=vid, status="confirmed",
                            answered_by="web"))
        s.commit()
    d = Path(os.environ["DATA_DIR"]) / "voice_samples" / str(vid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.webm").write_bytes(b"x")
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        v.sample_paths = [str(d / "a.webm")]
        s.commit()
    from mnema.core.speaker_backfill import recompute_profile_embedding
    detail = recompute_profile_embedding(vid)
    assert "1 zapisey + 1 klasteov" in detail
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        assert len(v.ref_embeddings) == 2
        keys = v.match_stats["ref_sources"]
        assert keys[1] == "job:31:S0" and keys[0].startswith("path:")


# ---- K1: upload v api bez pyannote ne pishet embedding, stavit v ochered' --------
def test_upload_sample_without_backend_defers_to_worker(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: None)
    calls = []
    monkeypatch.setattr("mnema.core.queue.enqueue_job",
                        lambda name, func, *a, **kw: calls.append((name, func, a))
                        or "rqjob")
    vid = _mkprofile("K1 K.K.")
    p = _loud_speech_wav(Path(os.environ["DATA_DIR"]) / "sample.webm")
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    with TestClient(app) as client:
        with open(p, "rb") as fh:
            r = client.post(f"/api/voice-profiles/{vid}/sample",
                            files={"file": ("sample.webm", fh, "audio/wav")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["pending_embedding"] is True
    assert body["worker_queued"] is True
    enr = [c for c in calls if "run_profile_sample_enroll" in str(c[1])]
    assert enr and enr[0][0] == "speaker" and enr[0][2][0] == vid
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        assert v.embedding is None  # hash-musora bolshe net
        assert len(v.sample_paths) == 1


def test_upload_sample_dead_mic_rejected(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: None)
    monkeypatch.setattr("mnema.core.queue.enqueue_job", lambda *a, **k: None)
    vid = _mkprofile("Tihiy T.T.")
    p = _dead_silent_wav(Path(os.environ["DATA_DIR"]) / "dead2.wav")
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    with TestClient(app) as client:
        with open(p, "rb") as fh:
            r = client.post(f"/api/voice-profiles/{vid}/sample",
                            files={"file": ("dead.webm", fh, "audio/wav")})
    assert r.status_code == 200 and r.json()["ok"] is False
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        assert not s.get(VoiceProfile, vid).sample_paths


def test_worker_sample_enroll_appends_ref_idempotent(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker.compute_embedding_for_file",
                        lambda path: [0.0, 1.0])
    vid = _mkprofile("Sampl S.S.")
    d = Path(os.environ["DATA_DIR"]) / "voice_samples" / str(vid)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "s1.wav"
    p.write_bytes(b"x")
    from mnema.worker.tasks import run_profile_sample_enroll
    run_profile_sample_enroll(vid, str(p))
    run_profile_sample_enroll(vid, str(p))   # povtor ne mnozhit
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        assert v.ref_embeddings == [[0.0, 1.0]]
        assert v.match_stats.get("enrollments") == 1


# ---- audit fixa: legacie 32-D hash-stuby ne dolzhi vliyat' na matsching --------
def test_legacy_hash_embedding_does_not_pollute_ref_set(db):
    """Staryy api pisal v profil' 32-D hash. Pri append_ref zhivym 512-D legasy
    ne dolzhen popadat' v ref-set (inoje perechen'sya u sekaet cosine vsluhuyu)."""
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        v = VoiceProfile(employee_name="Legasy L.L.", embedding=[0.11] * 32)
        s.add(v)
        s.commit()
        vid = v.id
    from mnema.core.speaker_backfill import append_ref
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        assert append_ref(v, [0.0] + [1.0] * 511, "job:1:S0") is True
        s.commit()
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        assert [len(r) for r in v.ref_embeddings] == [512]  # 32-D hash otsyanutsya
        assert len(v.embedding) == 512


def test_profile_similarity_skips_dim_mismatch():
    from mnema.core.models import VoiceProfile
    from mnema.core.speaker import profile_similarity
    vp = VoiceProfile(employee_name="Mix M.M.", embedding=[0.1] * 32,
                      ref_embeddings=[[0.1] * 32, [0.0, 1.0]])
    assert profile_similarity([0.0, 1.0], vp) == pytest.approx(1.0)   # po 2-D ref
    assert profile_similarity([1.0, 0.0], vp) == pytest.approx(0.0)   # 32-D ignor


def test_mean_embedding_ignores_foreign_dims():
    import math
    from mnema.core.speaker_backfill import mean_embedding
    m = mean_embedding([[1.0, 0.0], [0.5, 0.5], [9.0] * 32])
    assert len(m) == 2 and math.isclose(math.hypot(*m), 1.0)
    assert m[0] == pytest.approx(0.75 / math.hypot(0.75, 0.25))  # 32-D ne vpletyotsya
