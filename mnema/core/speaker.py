"""Golosovaya identifikatsiya (4.6): emgeddingi, matsching s porogami, otvety operatora.

Tyazhelaya chast' (pyannote embedding) — lazy import: esli model' nie
ustanovlena/ispol'zuyetsya CPU-only stub, sistema ne upadaet (kholodnyj start,
FT-29). Embedding — vektor; sravnenie — kosinusnoe shodstvo (FT-27).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

from sqlalchemy import select

from .db import get_session
from .models import ReviewRequest, Segment, VoiceProfile

log = logging.getLogger("mnema.speaker")

# ---- emgeddingi --------------------------------------------------------------
_backend_cache: dict = {}


def _embedding_backend():
    """pyannote-embedding / SpeechBrain; None esli niedostupen (CPU stub)."""
    if "backend" in _backend_cache:
        return _backend_cache["backend"]
    backend = None
    try:
        import torch  # noqa: F401
        from pyannote.audio import Inference, Model  # noqa: F401
        backend = "pyannote"
    except Exception:  # noqa: BLE001
        try:
            from speechbrain.pretrained import EncoderClassifier  # noqa: F401
            backend = "speechbrain"
        except Exception:  # noqa: BLE001
            backend = None
    _backend_cache["backend"] = backend
    return backend


def compute_embedding_for_file(path: Path) -> list[float] | None:
    """Zhivoy embedding (pyannote); None pri otkazhanii model'i.

    Hash-stub iz zhivykh putey ubran: 32-D vektor-surtseg, zapisiannyi v
     profil' pri saze pyannote, vechno daoitsya s 512-D etalonami (cosine ~0
    i profil' ne uzaetsya). None chesno otpravlyaet klaster v unresolved."""
    if _embedding_backend() != "pyannote":
        return None
    try:
        return _pyannote_embedding(path)
    except Exception as e:  # noqa: BLE001
        log.warning("embedding failed (%s): %s", path.name, e)
        return None


_inference_cache: dict = {}


def _get_inference(device_str: str):
    """Kesh Inference po ustroystvu: zagruzka modeli ~20 s, povtornaya na kazhdy
    srez klastera — glavnyy proigrysh po skornosti speaker-shaga."""
    key = device_str
    if key in _inference_cache:
        return _inference_cache[key]
    from pyannote.audio import Inference, Model
    import torch
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    model = Model.from_pretrained("pyannote/embedding", use_auth_token=tok)
    inference = Inference(model, window="whole", device=torch.device(device_str))
    _inference_cache[key] = inference
    return inference


# Fil'tr podgotovki dlya embeddingov: highpass sbrasyvaet guditel' seti/konditsionera,
# loudnorm privodit raznyy trakt (browser-mik/zal/Zoom) k odnomu urovnyu — bez nego
# cosine sistematicheski zanimaetsya mezhdu 'studioynym' etalonom i zapisyu vstrechi.
EMBED_AFILTER = "highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11"


def _pyannote_embedding(path: Path) -> list[float]:
    device_str = os.environ.get("ASR_DEVICE", "cpu")
    inference = _get_inference(device_str)
    import tempfile
    from .audio import run
    with tempfile.NamedTemporaryFile(suffix=".wav") as tf:
        r = run(["ffmpeg", "-y", "-i", str(path), "-af", EMBED_AFILTER,
                 "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", tf.name])
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg embed-prep: {r.stderr[-300:]}")
        vec = inference(tf.name)
    import numpy as np
    arr = np.asarray(vec).flatten()
    norm = float(np.linalg.norm(arr)) or 1.0
    return (arr / norm).tolist()


def quality_gate(path: Path) -> tuple[bool, str]:
    """FT-29a: dlitel'nost' + SNR/uroven' — inache periezapis'.

    'Mertvy' mikrofon (RMS ~0) i noy s SNR < ~3 dB otbrasyvayutsya:
    embedding s takogo zapisa zagryazhaet profil'. Пороги env-тюнингуемые."""
    from .audio import ffprobe_duration, snr_db, speech_level, wav_duration_s
    p = Path(path)
    dur = wav_duration_s(p) if p.suffix == ".wav" else ffprobe_duration(p)
    if dur < 1.5:
        return False, f"slisikom korotko ({dur:.1f} s < 1.5 s)"
    if dur > 120:
        return False, "slishkom dlino (> 120 s)"
    try:
        min_snr = float(os.environ.get("ENROLL_MIN_SNR_DB", "3") or 3)
        min_level = float(os.environ.get("ENROLL_MIN_RMS", "50") or 50)
    except ValueError:
        min_snr, min_level = 3.0, 50.0
    level = speech_level(p)
    if level < min_level:
        return False, f"zvuk slishkom tih (RMS {level:.0f} < {min_level:.0f})"
    snr = snr_db(p)
    if snr < min_snr:
        return False, f"shumnyy zapis' (SNR ~{snr:.0f} dB < {min_snr:.0f} dB)"
    return True, "ok"


# ---- kosinusnoe shodstvo i matsching (FT-27) --------------------------------
def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def thresholds() -> tuple[float, float]:
    t_conf = float(os.environ.get("SPEAKER_T_CONFIRM", "0.65") or 0.65)
    t_hint = float(os.environ.get("SPEAKER_T_HINT", "0.45") or 0.45)
    return t_conf, t_hint


def profile_similarity(embedding: list[float], v: VoiceProfile) -> float:
    """Max cosine po vsem etalonom profilya (srednee + ref_embeddings).

    Otnositel'no usrednennogo vektora max-po-setu sohanyaet razlichimost'
    pohozhih golosov (usrednenie razmyvaet varyacii) i toleriruet 'rezhim'
    zapisi (doklad/razgovor) mezhdu etalonom i vstrechey.
    Razmernosti nesovpadayushchie s zaprosom otbrosyvaetsya: cosine s zip
    daet 'uosechkoy' sluchaynoe znachenie, a legacie 32-D hash-stuby iz BDB
    (staroy api) s 512-D etalonami sravnivat'sya vovse ne dolzhny."""
    dim = len(embedding)
    best = 0.0
    if v.embedding and len(v.embedding) == dim:
        best = cosine(embedding, v.embedding)
    for ref in (v.ref_embeddings or []):
        if len(ref) != dim:
            continue
        s = cosine(embedding, ref)
        if s > best:
            best = s
    return best


def match_cluster(embedding: list[float]) -> tuple[str, VoiceProfile | None, float]:
    """Vozvr. ('auto'|'hint'|'unresolved', profil'|None, similarity)."""
    t_conf, t_hint = thresholds()
    best_profile = None
    best_sim = 0.0
    with get_session() as s:
        for v in s.scalars(select(VoiceProfile)).all():
            if not v.embedding and not v.ref_embeddings:
                continue
            sim = profile_similarity(embedding, v)
            if sim > best_sim:
                best_sim, best_profile = sim, v
    if best_profile and best_sim >= t_conf:
        return "auto", best_profile, best_sim
    if best_profile and best_sim >= t_hint:
        return "hint", best_profile, best_sim
    return "unresolved", None, best_sim


def _cluster_embedding_for(s, job_id: int, cluster_label: str) -> list[float] | None:
    """Embedding klastera iz zapisi vstrechi, chtoby novyy profil' matsilsya.

    Tol'ko zhivoy backend: hash-stub 32-D, poluchennyy v api-konteynere bez
    pyannote, zagryazit profil' mushorom nesovmestimoy dimensii (kosinus vsegda
    ~0 vs 512-D etalonov) — lu4he ostavit' profil' bez embedding.
    """
    if _embedding_backend() != "pyannote":
        return None
    try:
        from ..worker.tasks import _cluster_embedding, _job_paths
        from .models import Job
        wav = _job_paths(job_id)["wav"]
        if not wav.exists():
            return None
        job = s.get(Job, job_id)
        turns = job.diar_turns if job is not None else None
        segs = list(s.scalars(select(Segment).where(
            Segment.job_id == job_id, Segment.speaker_id == cluster_label,
            Segment.excluded.is_(False))).all())
        return _cluster_embedding(wav, segs, turns=turns)
    except Exception:  # noqa: BLE001
        return None


def apply_review_answer(rid: int, employee_id: int | None, employee_name: str | None,
                        create_new: bool, answered_by: str) -> str | None:
    """FT-28/FT-32: otvet primenyaetsya ko vsemu klasteryu + dokuchaet profil'."""
    with get_session() as s:
        r = s.get(ReviewRequest, rid)
        if r is None:
            return None
        job_id, cluster = r.job_id, r.cluster_label
        if create_new and employee_name:
            v = s.scalars(select(VoiceProfile).where(
                VoiceProfile.employee_name == employee_name)).first()
            reused = v is not None
            if v is None:
                v = VoiceProfile(employee_name=employee_name,
                                 consent_ts=dt.datetime.now(dt.timezone.utc))
                s.add(v)
                s.flush()
            name = employee_name
            r.candidate_profile_id = v.id
            if not reused and v.embedding is None:
                emb = _cluster_embedding_for(s, job_id, cluster)
                if emb:
                    v.embedding = emb
                    # embedding etalona iz utocnennogo klastera — srazu v set'
                    # refov, chtoby max-cosine matsching uchityval ego s razu
                    v.ref_embeddings = [emb]
                    stats = dict(v.match_stats or {})
                    stats["ref_sources"] = [f"job:{job_id}:{cluster}"]
                    v.match_stats = stats
        elif employee_id:
            v = s.get(VoiceProfile, employee_id)
            name = v.employee_name if v else employee_name
            r.candidate_profile_id = employee_id
        else:
            name = employee_name
        r.status = "confirmed"
        r.answered_by = answered_by
        r.answered_ts = dt.datetime.now(dt.timezone.utc)
        # peremenuyat' segmenty klasterya; multi_voice (dva golosa v fragmente)
        # imeni ne prisvaivaetsya — v fragmente vtoroy chelovek, ego frazy ne
        # dolzhny pisatsya pod podtverzhdennym imenom (kak i v _run_speaker);
        # voice_mix (operator podtverdil mikss) — to zhe, no snyato s review
        for seg in s.scalars(select(Segment).where(
                Segment.job_id == job_id, Segment.speaker_id == cluster)).all():
            if seg.speaker_source == "voice_mix":
                seg.needs_review = False
                seg.speaker_name = None  # pod miksom dva cheloveka — imya ne derzhim
                continue
            if seg.speaker_source == "multi_voice":
                seg.needs_review = True
                continue
            if name:
                seg.speaker_name = name
                seg.speaker_source = "voiceprint"
            seg.needs_review = False
        # update match_stats (FT-33)
        if r.candidate_profile_id:
            v = s.get(VoiceProfile, r.candidate_profile_id)
            if v:
                stats = dict(v.match_stats or {})
                stats["confirmed"] = stats.get("confirmed", 0) + 1
                v.match_stats = stats
        s.commit()
    try:
        from .protocol_md import refresh_protocol
        from .models import Protocol
        with get_session() as s:
            proto = s.scalars(select(Protocol).where(Protocol.job_id == job_id)).first()
            pid = proto.id if proto else None
        if pid:
            refresh_protocol(pid)
    except Exception:  # noqa: BLE001 — vygruzki nie kritichny dlya otveta review
        pass
    return name
