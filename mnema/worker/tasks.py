"""Zadachi konveyera (FT-34) dlya RQ-workera (roli: asr/speaker/structure/archive).

Kazhdaya zadacha idempotentna (HT-2): povtornyy zabeg peresozdayet rezultat s
nulya. Vne-zavisimosti (gigastt/ollama) — capability, FT-39.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
from pathlib import Path

from sqlalchemy import select

from ..core.db import get_session
from ..core.models import (AudioFile, Job, Protocol, ReviewRequest, Segment,
                           SessionEvent, VoiceProfile)

log = logging.getLogger("mnema.tasks")


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def _job_paths(job_id: int) -> dict[str, Path]:
    d = _data_dir() / "jobs" / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return {"dir": d, "webm": d / "raw.webm", "wav": d / "meeting.wav",
            "mp3": d / "meeting.mp3", "steno": d / "steno.md",
            "full_wav": d / "meeting-full.wav", "full_mp3": d / "meeting-full.mp3"}


# ---------------------------------------------------------------- ASR (FT-11..15)
def run_orchestrate(job_id: int) -> None:
    """Tochka vhoda konveyera: prep+VAD, zatem parallel'nyye vetki asr/speaker,
    zatem join (steno+structure). V direct-rezhime (bez redis) — posledovatel'no.

    Vetki pisayut v raznye kolonki odnih i tekh zhe segmтов: asr — text/lang/
    engine/confidence, speaker — speaker_*/diar_label/needs_review. Join
    zapuskayetsya cherez RQ depends_on posle oboikh vetok (wait-all)."""
    try:
        run_prep(job_id)
        from ..core.queue import get_queue
        _, mode = get_queue("speaker")
        if mode == "rq":
            from ..core.queue import enqueue_job
            ja = enqueue_job("asr", "mnema.worker.tasks.run_asr_text", job_id)
            js = enqueue_job("speaker", "mnema.worker.tasks.run_speaker_id", job_id)
            deps = [d for d in (ja, js) if d is not None]
            enqueue_job("structure", "mnema.worker.tasks.run_join", job_id,
                        depends_on=deps)
        else:
            run_asr_text(job_id)
            run_speaker_id(job_id)
            run_join(job_id)
    except Exception as e:  # noqa: BLE001
        from ..core.pipeline import transition
        transition(job_id, "failed", fail_reason=_classify(e),
                   error=str(e)[:1000], event="asr_error")
        raise


def run_prep(job_id: int) -> list[dict]:
    """Raspadovka (wav/mp3) + VAD + sozdanie rows segmentov (idempotentno)."""
    p = _job_paths(job_id)
    _prepare_wav(job_id, p)
    _maybe_drop_raw_source(job_id, p)
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event="vad_start"))
        s.commit()
    from ..core.vad import segment_speech
    wav = p["wav"]
    raw_segs = segment_speech(wav) if wav.exists() else []
    with get_session() as s:
        s.query(Segment).filter(Segment.job_id == job_id).delete()
        for seg in raw_segs:
            s.add(Segment(job_id=job_id, raw_track="web-mix",
                          t_start=seg["t_start"], t_end=seg["t_end"], text="",
                          speaker_source="unresolved"))
        s.add(SessionEvent(job_id=job_id, event="vad_done",
                           detail=f"сегментов: {len(raw_segs)}"))
        s.commit()
    from ..core import export
    export.export_recording(job_id)  # zerkalo samba: vnutri try/except, ne lonit' konveyer
    return raw_segs


def _job_segments(job_id: int) -> list:
    with get_session() as s:
        return s.scalars(select(Segment).where(Segment.job_id == job_id)
                         .order_by(Segment.t_start, Segment.id)).all()


def _fail_branch(job_id: int, e: Exception, event: str) -> None:
    from ..core.pipeline import ALLOWED_TRANSITIONS, transition
    from ..core.db import get_session as _gs
    with _gs() as s:
        job = s.get(Job, job_id)
        if job is None or job.status in ("failed", "done", "cancelled") \
                or "failed" not in ALLOWED_TRANSITIONS.get(job.status, set()):
            return  # uzhe zahvachennoy drugoy vetkoy — ne prevrashchat' v failed->failed
    transition(job_id, "failed", fail_reason=_classify(e),
               error=str(e)[:1000], event=event)


def run_asr_text(job_id: int) -> None:
    """Vetka transkribirovaniya: VAD-segmenty -> text (gigastt/faster-whisper)."""
    try:
        p = _job_paths(job_id)
        wav = p["wav"]
        segs = _job_segments(job_id)
        if not segs:
            return
        from ..core.asr_client import transcribe_segments, engine_name
        engine = engine_name()
        with get_session() as s:
            s.add(SessionEvent(job_id=job_id, event="asr_start",
                               detail=f"движок: {engine}"))
            s.commit()
        vad = [{"t_start": s.t_start, "t_end": s.t_end} for s in segs]
        results = transcribe_segments(wav, vad) if wav.exists() else []
        with get_session() as s:
            rows = s.scalars(select(Segment).where(Segment.job_id == job_id)
                             .order_by(Segment.t_start, Segment.id)).all()
            for row, txt in zip(rows, results):
                row.text = txt.get("text", "")
                row.lang = txt.get("lang")
                row.engine = engine
                row.confidence = txt.get("confidence")
                row.asr_json = txt.get("parts")
            s.add(SessionEvent(job_id=job_id, event="asr_done",
                               detail=f"движок: {engine}"))
            s.commit()
    except Exception as e:  # noqa: BLE001
        _fail_branch(job_id, e, "asr_error")
        raise


def run_speaker_id(job_id: int) -> None:
    """Vetka govoraschih: diarizaciya (kak govoril) + voiceprint-matsching (kto)."""
    try:
        p = _job_paths(job_id)
        wav = p["wav"]
        segs = _job_segments(job_id)
        if not segs:
            return
        vad = [{"t_start": s.t_start, "t_end": s.t_end} for s in segs]
        from ..core.diarize import diarize_with_turns
        with get_session() as s:
            s.add(SessionEvent(job_id=job_id, event="diar_start",
                               detail=f"сегментов: {len(segs)}"))
            s.commit()
        try:
            labels, turns, ratios = diarize_with_turns(wav, vad)
        except Exception as e:  # noqa: BLE001
            log.warning("diarize failed (%s) — all segments SPEAKER_UNKNOWN", e)
            labels, turns, ratios = ["SPEAKER_UNKNOWN"] * len(segs), None, None
        with get_session() as s:
            rows = s.scalars(select(Segment).where(Segment.job_id == job_id)
                             .order_by(Segment.t_start, Segment.id)).all()
            for i, row in enumerate(rows):
                label = labels[i] if i < len(labels) else "SPEAKER_UNKNOWN"
                if row.speaker_source == "voice_mix":
                    # operatorskaya pometka miksa: perezapisyvat' ee na
                    # multi_voice/ne-ozvuchennyy pri povtornoy diarizacii nel'zya
                    continue
                row.speaker_id = label
                row.diar_label = label
                ratio = ratios[i] if (ratios and i < len(ratios)) else None
                if turns and ratio is not None and ratio < _overlap_ratio_min():
                    # v segmente dva golosa: embedding iz nego nyat' nel'zya,
                    # imya ne prisvaivaem — tol'ko review. Bez turns (CPU/diari
                    # otsutstvuet) coverage ne izvesen — ne kleit.
                    row.speaker_source = "multi_voice"
                    row.needs_review = True
            job = s.get(Job, job_id)
            if job is not None:
                job.diar_turns = turns
            s.add(SessionEvent(job_id=job_id, event="diar_done",
                               detail=f"кластеров: {len(set(labels))}"))
            s.commit()
        _run_speaker(job_id)
    except Exception as e:  # noqa: BLE001
        _fail_branch(job_id, e, "speaker_error")
        raise


def _overlap_ratio_min() -> float:
    """Dolya dominantnogo govoryashchego, priseyusha segm smeshannym (overlap)."""
    try:
        return float(os.environ.get("OVERLAP_MIN_COVERAGE", "0.7") or 0.7)
    except ValueError:
        return 0.7


def run_join(job_id: int) -> None:
    """Sliyaniye vetok posle oboikh: steno.md, zatem split+LLM — tol'ko kogda vse
    govoryashchiye opredeleny. Pri neraspznannyh golosakh stop v awaiting_review
    (analiz LLM idet POSLE utochneniya lyudey, a ne do nego)."""
    try:
        p = _job_paths(job_id)
        _write_steno(job_id, p)
        if _route_awaiting_review_if_unresolved(job_id):
            return
        _run_split_and_structure(job_id)
    except Exception as e:  # noqa: BLE001
        _fail_branch(job_id, e, "processing_error")
        raise


def _route_awaiting_review_if_unresolved(job_id: int) -> bool:
    """True esli est' pending review-request: status -> awaiting_review s deadlaynom."""
    from ..core.pipeline import review_requests_resolved, transition
    if review_requests_resolved(job_id):
        return False
    import datetime as dt
    hours = int(os.environ.get("REVIEW_TIMEOUT_HOURS", "48") or 48)
    with get_session() as s:
        job = s.get(Job, job_id)
        job.review_deadline = (dt.datetime.now(dt.timezone.utc)
                               + dt.timedelta(hours=hours))
        s.commit()
    transition(job_id, "awaiting_review")
    return True


def run_asr_pipeline(job_id: int) -> None:
    """Posledovatel'nyy rezhim (dev/tests/bez redis): vetsi vot poryadke."""
    try:
        run_prep(job_id)
        run_asr_text(job_id)
        run_speaker_id(job_id)
        run_join(job_id)
    except Exception as e:  # noqa: BLE001
        _fail_branch(job_id, e, "asr_error")
        raise


def _classify(e: Exception) -> str:
    txt = str(e).lower()
    if "split" in txt:
        return "split_error"
    if "asr" in txt or "transcription" in txt:
        return "asr_error"
    return "processing_error"


def _maybe_drop_raw_source(job_id: int, p: dict) -> None:
    """KEEP_UPLOADED_SOURCE=0: udalit' ishodnyy arhiv (i ego AudioFile-row)
    posle uspechnogo izvlecheniya meeting.wav/mp3. Tol'ko dlya source=upload.

    Udalenie po puti (ne po kind), chtoby ne zadet' distributivnye rowy
    meeting.wav/meeting.mp3."""
    from sqlalchemy import select as _select
    from ..core.models import Upload
    from ..core.uploads import keep_source
    if keep_source():
        return
    if not p["wav"].exists() or not p["mp3"].exists():
        return
    raw = _find_raw(p["dir"])
    if raw is None:
        return
    if str(raw) == str(p["wav"]) or str(raw) == str(p["mp3"]):
        return  # raw i est' distributiv — ne udalyaem
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None or job.source != "upload":
            return
        s.query(AudioFile).filter(AudioFile.job_id == job_id,
                                  AudioFile.path == str(raw)).delete()
        for up in s.scalars(_select(Upload).where(Upload.job_id == job_id)).all():
            if up.status == "complete" and up.path == str(raw):
                up.path = ""
        s.commit()
    raw.unlink(missing_ok=True)


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink vmesto kopii (obychno tot zhe volume /data): ekonomiya v GB.
    Pri nevozmozhnosti hardlinka (raznye FS) — copyfile."""
    if dst.exists():
        dst.unlink()
    try:
        os.link(str(src), str(dst))
    except OSError:
        shutil.copyfile(str(src), str(dst))


def _find_raw(job_dir: Path) -> Path | None:
    """Nayti ishodnyy fayl konveyera: raw.webm (zapis') ili raw.<ext> (arhiv-zagruzka).

    Proizvodnye fayly konveyera nazyvayutsya meeting.*, togda kak raw.* sozdayet
    tol'ko finalize zagruzki (ili web-bot) — lyuboy raw.<ext> eto istoch."""
    from ..core.audio import MEDIA_EXTENSIONS
    cand = job_dir / "raw.webm"
    if cand.exists():
        return cand
    for f in sorted(job_dir.glob("raw.*")):
        if f.suffix.lower() in MEDIA_EXTENSIONS:
            return f
    return None


def _prepare_wav(job_id: int, p: dict) -> None:
    """Ishodnik (webm-zapis' ili arhivnyy video/audio) -> wav/mp3 (FT-6 + FT-41)."""
    from ..core.audio import webm_to_wav16k, webm_to_mp3, repair_webm, wav_duration_s
    from ..core.audio import media_to_wav16k, media_to_mp3
    raw = _find_raw(p["dir"])
    if raw is None:
        return
    if p["wav"].exists() and p["wav"].stat().st_size > 44:
        return
    is_webm = raw.suffix.lower() == ".webm"
    if is_webm:
        if not _webm_ok(raw):
            repair_webm(raw)
        webm_to_wav16k(raw, p["wav"])
        dur = wav_duration_s(p["wav"])
        if not p["mp3"].exists():
            ok = webm_to_mp3(raw, p["mp3"]) or _wav_to_mp3(p)
            if not ok:
                pass  # mp3 enc niedostupen — vydayem wav kak distributiv (FT-41a)
    elif raw.suffix.lower() == ".wav":
        # arhivnyy wav lyubogo formata: peresampleiruem v canonical'nyy 16k mono
        media_to_wav16k(raw, p["wav"])
        dur = wav_duration_s(p["wav"])
        if not p["mp3"].exists():
            media_to_mp3(raw, p["mp3"]) or _wav_to_mp3(p)
    elif raw.suffix.lower() == ".mp3":
        from ..core.audio import ffprobe_duration
        if raw != p["mp3"]:
            _link_or_copy(raw, p["mp3"])  # gotovaya dorozhka dlya vydachi
        dur = ffprobe_duration(raw)
        media_to_wav16k(raw, p["wav"])
    else:
        # video/konteyner: izvlekayem pervuyu audio-dorozhku
        media_to_wav16k(raw, p["wav"])
        dur = wav_duration_s(p["wav"])
        if not p["mp3"].exists():
            media_to_mp3(raw, p["mp3"])
    _register_audio_files(job_id, p, dur)


def _wav_to_mp3(p: dict) -> bool:
    from ..core.audio import wav16k_to_mp3
    return wav16k_to_mp3(p["wav"], p["mp3"])


def _webm_ok(webm: Path) -> bool:
    from ..core.audio import ffprobe_duration
    return ffprobe_duration(webm) > 0


def _register_audio_files(job_id: int, p: dict, dur: float) -> None:
    with get_session() as s:
        for kind, path in (("wav", p["wav"]), ("mp3", p["mp3"])):
            if path.exists():
                exists = s.scalars(select(AudioFile).where(
                    AudioFile.job_id == job_id, AudioFile.kind == kind)).first()
                if exists is None:
                    s.add(AudioFile(job_id=job_id, kind=kind, path=str(path),
                                    bytes=path.stat().st_size, duration_s=dur))
        s.commit()


def run_structure(job_id: int) -> None:
    """Vozvrat posle utochneniya spikero: steno.md s itogovymi imenami + LLM.
    Tot zhe put' dlya resume/review-timeout/rerun: popytka razdeleniya vnutri."""
    try:
        _write_steno(job_id, _job_paths(job_id))
        _run_split_and_structure(job_id)
    except Exception as e:  # noqa: BLE001
        _fail_branch(job_id, e, "processing_error")
        raise


def _run_split_and_structure(job_id: int) -> None:
    """Этап split (попытка разделения записи на несколько встреч) перед LLM.

    Ошибка детекции (LLMError и иная) не роняет job: событие split_skipped,
    дальше — обычный _run_structure по полной записи. Сбой применения границ
    (SplitApplyError) — событие split_error, но частично созданные потомки
    всё равно получают run_structure (иначе зависнут в processing). Падение
    потомка не трогает родителя."""
    from ..core import split as split_core
    apply_failed = False
    try:
        child_ids = split_core.split_job(job_id)
    except split_core.SplitApplyError as e:
        _log_event(job_id, "split_error",
                   f"сбой применения границ: {str(e)[:300]}")
        child_ids = e.child_ids
        apply_failed = True
    except Exception as e:  # noqa: BLE001 — split не причина топить job
        _log_event(job_id, "split_skipped", f"ошибка детекции: {str(e)[:300]}")
        child_ids = []
    if child_ids:
        # родительский steno устарел (содержит хвост второй встречи) —
        # перезаписываем до _run_structure (он переиспользует файл)
        _write_steno(job_id, _job_paths(job_id))
        from ..core.queue import enqueue_job
        for cid in child_ids:
            if enqueue_job("structure", "mnema.worker.tasks.run_structure",
                           cid, dedupe=True) is None:
                try:
                    run_structure(cid)  # direct-режим: inline
                except Exception:  # noqa: BLE001 — падение потомка не трогает родителя
                    pass
    if apply_failed:
        raise RuntimeError("split: применение границ не завершено, "
                           "родительская стенограмма может быть неполной")
    _run_structure(job_id)


def _log_event(job_id: int, event: str, detail: str) -> None:
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event=event, detail=detail[:1000]))
        s.commit()


def run_voice_enroll(job_id: int, cluster_label: str) -> None:
    """Reestr golosa: utocnenie klastera operatorom (ili taym-autom) dokuchaet
    profil' novym etalonom-embdom klastera. Tol'ko v workere (est' pyannote);
    v api s backends=None vyhod bez hash-musora. Auto-otvety voiceprint
    isklyucheny, chtoby ne usilivat' obratnyu svyazi.

    Ref-set vmesto EMA: kazhdy istochnik — otdel'nyy etalon (ref_embeddings),
    matsching idyot po max cosine; srednee embedding — produsirovanno kesh.
    EMA, peremeshivayushchiy vseh v odin vektor, razmyval pohozhie golosa."""
    from ..core.speaker import _embedding_backend
    if _embedding_backend() != "pyannote":
        return
    with get_session() as s:
        r = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == job_id,
            ReviewRequest.cluster_label == cluster_label,
            ReviewRequest.status == "confirmed")).first()
        if r is None or not r.candidate_profile_id or r.answered_by == "voiceprint":
            return
        if profile_absent(s, r.candidate_profile_id):
            return
        profile_id = r.candidate_profile_id
    try:
        from ..core.speaker_backfill import cluster_embedding_for
        emb = cluster_embedding_for(job_id, cluster_label)
    except Exception as e:  # noqa: BLE001 — enroll ne lonit utochnenie
        log.warning("voice_enroll failed (job %s): %s", job_id, e)
        return
    if not emb:
        return
    from ..core.speaker_backfill import append_ref
    with get_session() as s:
        profile = s.get(VoiceProfile, profile_id)
        if profile is None:
            return
        if not append_ref(profile, emb, _cluster_source_key(job_id, cluster_label)):
            stats = dict(profile.match_stats or {})
            stats["enroll_skipped_dup"] = stats.get("enroll_skipped_dup", 0) + 1
            profile.match_stats = stats
            s.commit()
            return
        stats = dict(profile.match_stats or {})
        stats["enrollments"] = stats.get("enrollments", 0) + 1
        profile.match_stats = stats
        s.commit()
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event="voice_enroll",
                           detail=f"кластер {cluster_label} -> профиль {profile_id}"))
        s.commit()


def _cluster_source_key(job_id: int, cluster_label: str) -> str:
    return f"job:{job_id}:{cluster_label}"


def profile_absent(s, profile_id: int) -> bool:
    return s.get(VoiceProfile, profile_id) is None


def run_profile_sample_enroll(profile_id: int, sample_path: str) -> None:
    """Embedding novoy zapisi s mikrofona (K1): api bez ASR-skada tol'ho fayl,
    zhivoy vektor stroit worker i dokhlyaet v set' etalonov. Idempotentno po
    puti v ref_sources."""
    from ..core.speaker import _embedding_backend, compute_embedding_for_file
    if _embedding_backend() != "pyannote":
        return
    p = Path(sample_path)
    if not p.is_file():
        log.warning("profile_sample_enroll: file not found: %s", sample_path)
        return
    emb = compute_embedding_for_file(p)
    if not emb:
        log.warning("profile_sample_enroll: no embedding produced (%s)", sample_path)
        return
    from ..core.speaker_backfill import append_ref
    with get_session() as s:
        v = s.get(VoiceProfile, profile_id)
        if v is None:
            return
        if not append_ref(v, emb, f"path:{sample_path}"):
            return
        stats = dict(v.match_stats or {})
        stats["enrollments"] = stats.get("enrollments", 0) + 1
        v.match_stats = stats
        s.commit()
    _profile_event(profile_id, "sample_enrolled", sample_path)


def run_profile_reembed(profile_id: int) -> None:
    """ Udalenie/ugolovnoe etalona: pereschitat' embedding profila po
    ostavshimsya istochnikam. Tol'ko v workere (est' pyannote)."""
    from ..core.speaker import _embedding_backend
    if _embedding_backend() != "pyannote":
        return
    try:
        from ..core.speaker_backfill import recompute_profile_embedding
        detail = recompute_profile_embedding(profile_id)
    except Exception as e:  # noqa: BLE001 — reembed ne lonit udalenie etalona
        log.warning("profile_reembed failed (profile %s): %s", profile_id, e)
        return
    _profile_event(profile_id, "profile_reembed", detail)


def run_reverify_profile(profile_id: int) -> None:
    """Ruchnaya pereproverka cheloveka: zanovo sobrat' etalon i pere-machnut'
    vse vstrechi, gdye on podtverzhdён. Otkryvayut novyye utochneniya,
    zakryvayutsya совпавшiye; trudnich vkladov v match_stats ne delaetsya."""
    from ..core.speaker import _embedding_backend
    if _embedding_backend() != "pyannote":
        return
    try:
        from ..core.speaker_backfill import (profile_review_jobs,
                                             recompute_profile_embedding)
        detail = recompute_profile_embedding(profile_id)
    except Exception as e:  # noqa: BLE001
        log.warning("reverify failed (profile %s): %s", profile_id, e)
        _profile_event(profile_id, "reverify_failed", str(e)[:500])
        return
    jobs = profile_review_jobs(profile_id)
    rematched = 0
    from ..core.speaker_backfill import rematch_job
    for job_id in jobs:
        try:
            rematch_job(job_id)
            rematched += 1
        except Exception as e:  # noqa: BLE001 — odin plotnoy job ne lonit pereproverku
            log.warning("reverify rematch failed (job %s): %s", job_id, e)
    _profile_event(profile_id, "reverify_done",
                   f"{detail}; pere-macheno vstrech: {rematched}/{len(jobs)}")


def _profile_event(profile_id: int, event: str, detail: str) -> None:
    """Sobytie v match_stats profilya (poslednie statusy pereproverok dla UI):
    SessionEvent privyazan k jobu, a protsess profile-level."""
    with get_session() as s:
        v = s.get(VoiceProfile, profile_id)
        if v is None:
            return
        stats = dict(v.match_stats or {})
        stats[event] = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "detail": detail}
        v.match_stats = stats
        s.commit()


def _run_speaker(job_id: int) -> None:
    """FT-25..28: emgeddingi klasteov + matsching + review zapolnenie.

    V5: klastery s otvetom operatora (confirmed ne avtomatom) — zamochony:
    povtornyy matsching (rematch posle backfill) ne perekhodyaet v nih imena.
    multi_voice segmenty (na lozhenie dvuh golosov) v embedding ne berutsya."""
    from ..core.speaker import match_cluster
    p = _job_paths(job_id)
    wav = p["wav"]
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event="voiceprint_start"))
        s.commit()
    centroids: dict[str, list[float] | None] = {}
    locked_labels: set[str] = set()
    with get_session() as s:
        job = s.get(Job, job_id)
        turns = job.diar_turns if job is not None else None
        segs = s.scalars(select(Segment).where(
            Segment.job_id == job_id).order_by(Segment.t_start)).all()
        by_cluster: dict[str, list] = {}
        for seg in segs:
            by_cluster.setdefault(seg.diar_label or "unknown", []).append(seg)
        for label, cl in by_cluster.items():
            from datetime import datetime, timezone
            from ..core.models import ReviewRequest
            existing = s.scalars(select(ReviewRequest).where(
                ReviewRequest.job_id == job_id,
                ReviewRequest.cluster_label == label,
                ReviewRequest.status == "pending")).first()
            if existing is None:
                existing = s.scalars(select(ReviewRequest).where(
                    ReviewRequest.job_id == job_id,
                    ReviewRequest.cluster_label == label,
                    ReviewRequest.status.in_(("confirmed", "excluded",
                                              "superseded", "mixed")))).first()
            operator_locked = (existing is not None
                               and existing.status == "confirmed"
                               and existing.answered_by not in (None, "voiceprint"))
            clean = [x for x in cl
                     if x.speaker_source not in ("multi_voice", "voice_mix")]
            embedding = _cluster_embedding(wav, clean, turns=turns)
            centroids[label] = embedding
            verdict, profile, sim = match_cluster(embedding) if embedding else ("unresolved", None, 0.0)
            if operator_locked:
                locked_labels.add(label)
            for seg in cl:
                if operator_locked:
                    continue  # otvet cheloveka vertnee lyubogo novogo verdicta
                if seg.speaker_source == "voice_mix":
                    # operator skazal: mikss dvuh golosov — imya ne prisvaivaem,
                    # v embedding ne berem, na review bolshe ne vynosim
                    seg.needs_review = False
                    seg.speaker_name = None  # hint-imya do pometski: dva golosa, nyudy
                    continue
                if seg.speaker_source == "multi_voice":
                    # v fragmente dva golosa: imya ne prisvaivaem, v embedding
                    # on bez togo ne beretsya — tol'ko flag dlya stenogrammy
                    seg.needs_review = True
                    continue
                if verdict == "auto" and profile:
                    seg.speaker_name = profile.employee_name
                    seg.speaker_source = "voiceprint"
                    seg.needs_review = False
                elif verdict == "hint" and profile:
                    seg.speaker_name = profile.employee_name
                    seg.speaker_source = "voiceprint"
                    seg.needs_review = True
                else:
                    seg.speaker_name = None
                    seg.speaker_source = "unresolved"
                    seg.needs_review = True
            auto = verdict == "auto" and profile is not None
            if existing is not None:
                if existing.status == "pending":
                    if profile is not None:
                        existing.candidate_profile_id = profile.id
                    existing.similarity = sim
                    if auto:
                        existing.status = "confirmed"
                        existing.answered_by = "voiceprint"
                        existing.answered_ts = datetime.now(timezone.utc)
                        stats = dict(profile.match_stats or {})
                        stats["auto_confirmed"] = stats.get("auto_confirmed", 0) + 1
                        profile.match_stats = stats
            else:
                rv = ReviewRequest(job_id=job_id, cluster_label=label,
                                   candidate_profile_id=(profile.id if verdict in ("auto", "hint") and profile else None),
                                   similarity=sim,
                                   status="confirmed" if auto else "pending",
                                   answered_by="voiceprint" if auto else None,
                                   answered_ts=datetime.now(timezone.utc) if auto else None)
                s.add(rv)
                if auto:
                    stats = dict(profile.match_stats or {})
                    stats["auto_confirmed"] = stats.get("auto_confirmed", 0) + 1
                    profile.match_stats = stats
            s.commit()
    from ..core.db import get_session as _gs
    with _gs() as s:
        try:
            n = _phrase_resort(s, job_id, wav, list(s.scalars(select(Segment).where(
                Segment.job_id == job_id).order_by(Segment.t_start)).all()),
                centroids, locked_labels)
            if n:
                s.add(SessionEvent(job_id=job_id, event="phrase_resort",
                                   detail=f"фраз перенесено в другие кластеры: {n}"))
                # klaster bez fragmentov (vse frazy utohli) ne dolzhen vechno
                # derzhat' job v awaiting_review
                _supersede_empty_reviews(s, job_id)
        except Exception as e:  # noqa: BLE001 — resort ne lonit speaker-etap
            log.warning("phrase_resort failed (%r) — skip", e)
        s.add(SessionEvent(job_id=job_id, event="voiceprint_done"))
        s.commit()


def _supersede_empty_reviews(s, job_id: int) -> int:
    """Zakryvaet pending review-request klasterey bez segmentov (posle resort).

    Tylko sinteticskie podklastera (metka s defizom: -x<id>/< -p<id>):
    obychnye pyannote-metki ne dolzny ischezat' bez operatora."""
    n = 0
    for rv in s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == job_id,
            ReviewRequest.status == "pending")).all():
        if not rv.cluster_label or "-" not in rv.cluster_label:
            continue
        left = s.scalars(select(Segment.id).where(
            Segment.job_id == job_id,
            Segment.speaker_id == rv.cluster_label)).first()
        if left is None:
            rv.status = "superseded"
            rv.answered_by = "voiceprint"
            rv.answered_ts = dt.datetime.now(dt.timezone.utc)
            n += 1
    return n


def _phrase_verify_min_s() -> float:
    try:
        return float(os.environ.get("PHRASE_VERIFY_MIN_S", "1.0") or 1.0)
    except ValueError:
        return 1.0


def _phrase_keep_cos() -> float:
    try:
        return float(os.environ.get("PHRASE_KEEP_COS", "0.30") or 0.30)
    except ValueError:
        return 0.30


def _phrase_match_cos() -> float:
    try:
        return float(os.environ.get("PHRASE_MATCH_COS", "0.45") or 0.45)
    except ValueError:
        return 0.45


def _apply_turn_edits(turns: list, edits: list[tuple[float, float, str, str]]) -> list:
    """Frazy, perenesenyye resortom, otpechatayutsya v turns: v okne [w0,w1)
    chast' turna s metkoy from_lbl zamenyaetsya na new_lbl, ostal'nyye turny i
    chuzhiye (ne from_lbl) pereksrytiya ne trogayutsya. Vozvrat novyy spiso
    [t0, t1, label] bez nul-evyh kuschkov."""
    out: list = []
    for t in turns or []:
        try:
            pieces = [(float(t[0]), float(t[1]), t[2])]
        except (TypeError, ValueError, IndexError):
            out.append(t if isinstance(t, list) else list(t))
            continue
        for w0, w1, new_lbl, from_lbl in edits:
            nxt: list[tuple[float, float, str]] = []
            for a, b, l in pieces:
                if l != from_lbl or b <= w0 or a >= w1:
                    nxt.append((a, b, l))
                    continue
                if a < w0:
                    nxt.append((a, w0, l))
                nxt.append((max(a, w0), min(b, w1), new_lbl))
                if b > w1:
                    nxt.append((w1, b, l))
            pieces = nxt
        out.extend([[a, b, l] for a, b, l in pieces if b - a > 1e-9])
    return out


def _phrase_resort(s, job_id: int, wav: Path, segs: list,
                   centroids: dict, locked: set[str]) -> int:
    """A: frazy (chasti asr_json), zvuchashchie ne kak govoryashchiy klastera,
    perenodyatsya v sobstvennyy/podoshedshiy klaster.

    Pyannote proteaet korotkie vstavki (1-2 c) vnlitri okna 10 s i cladet vsyu
    VAD-stroku na odin klaster (job 22: '... Dobrogo dnya' vtorogo cheloveka
    pod imenem dokladchika). Timeshampy ASR-chastey dayut vozmozhnost'
    'doslushat' kazhduyu frazu otdel'no: emgedding frazy i sravnenie s
    centrodamı vseh klasterev. Kosterly bez vstroennyh emedgeydzhov:
    no-op pri otsutstvii pyannote (compute_embedding_for_file -> None).

    Vozvraschaet chiso sozdannykh dopolnitel'nykh strok. Zamochennye
    operatorom klastery (locked) i multi_voice ne trogaem."""
    if os.environ.get("PHRASE_VERIFY", "1").strip() == "0":
        return 0
    import tempfile
    from ..core.speaker import cosine, match_cluster, compute_embedding_for_file
    from ..core.vad import extract_wav_slice

    keep, match_min = _phrase_keep_cos(), _phrase_match_cos()
    min_s = _phrase_verify_min_s()
    known_labels = set(centroids)

    def phrase_emb(t0: float, t1: float):
        with tempfile.NamedTemporaryFile(suffix=".wav") as tf:
            extract_wav_slice(wav, t0, t1, Path(tf.name))
            return compute_embedding_for_file(Path(tf.name))

    def classify(part_emb: list[float], base: str) -> str | None:
        """Klaster frazy: svoy pri cs>=keep; inache — podoshedshiy chuzhoy
        (cs>=match_min i s otryvom ot svoego); None esli ni na kogo ne pohozha."""
        cs = cosine(part_emb, centroids[base])
        if cs >= keep:
            return base
        best, best_cs = None, cs
        for lbl, cen in centroids.items():
            if lbl == base or not cen:
                continue
            sim = cosine(part_emb, cen)
            if sim >= match_min and sim > best_cs + 0.10:
                best, best_cs = lbl, sim
        return best

    def name_for(label: str) -> tuple[str | None, str]:
        """Imya klastera: uzhe prislvoennoe (matsching/main cikl/otvet operatora),
        inache — matsching centroda novogo klastera."""
        if label in name_map:
            return name_map[label]
        if centroids.get(label):
            verdict, profile, _sim = match_cluster(centroids[label])
            if profile and verdict in ("auto", "hint"):
                return profile.employee_name, "voiceprint"
        return None, "unresolved"

    def ensure_review(label: str) -> None:
        """Review-request dla novogo klastera, kak dlya nraspoznannyh golosov;
        klastery iz main cikla svoi uzhe poluchili."""
        if label in known_labels:
            return
        dup = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == job_id,
            ReviewRequest.cluster_label == label)).first()
        if dup is not None:
            return
        profile, sim, auto = None, 0.0, False
        if centroids.get(label):
            verdict, profile, sim = match_cluster(centroids[label])
            auto = verdict == "auto" and profile is not None
        s.add(ReviewRequest(
            job_id=job_id, cluster_label=label,
            candidate_profile_id=(profile.id if profile else None),
            similarity=sim,
            status="confirmed" if auto else "pending",
            answered_by="voiceprint" if auto else None,
            answered_ts=dt.datetime.now(dt.timezone.utc) if auto else None))

    def join_parts(grp: list[dict]) -> str:
        return " ".join((p.get("t") or "").strip() for p in grp).strip()

    name_map: dict[str, tuple[str | None, str]] = {}
    for seg in segs:
        lbl = seg.diar_label or seg.speaker_id
        if lbl and seg.speaker_name and lbl not in name_map:
            name_map[lbl] = (seg.speaker_name,
                             seg.speaker_source or "voiceprint")

    created = 0
    turn_edits: list[tuple[float, float, str, str]] = []
    for seg in segs:
        if seg.excluded or seg.speaker_source in ("multi_voice", "voice_mix"):
            continue
        base = seg.diar_label or seg.speaker_id
        if not base or base in locked or not centroids.get(base):
            continue
        parts = [p for p in (seg.asr_json or [])
                 if (p.get("t") or "").strip()
                 and p.get("s") is not None and p.get("e") is not None]
        if len(parts) < 2:
            continue
        # klyuch — indeks chasti, ne id(p): CPython mojet перeисpol'zovat'
        # address osvobodivshihsya dictov v drukh strokakh (hrupkiy kontrakt)
        emb_cache: dict[int, list[float] | None] = {}
        for i, p in enumerate(parts):
            if float(p["e"]) - float(p["s"]) >= min_s:
                emb_cache[i] = phrase_emb(float(p["s"]), float(p["e"]))
        labels: list[str] = []
        for i, p in enumerate(parts):
            e = emb_cache.get(i)
            if e is None:
                labels.append(base)  # ochen' korotkuyu frazu ne doslushaem
                continue
            labels.append(classify(e, base) or f"{base}-p{seg.id}")
        if len(set(labels)) <= 1:
            continue
        grouped: list[tuple[str, list[tuple[int, dict]]]] = []
        cur_lbl = None
        cur: list[tuple[int, dict]] = []
        for i, (p, lbl) in enumerate(zip(parts, labels)):
            if cur_lbl is None:
                cur_lbl = lbl
            if lbl == cur_lbl:
                cur.append((i, p))
            else:
                grouped.append((cur_lbl, cur))
                cur_lbl, cur = lbl, [(i, p)]
        if cur:
            grouped.append((cur_lbl, cur))
        # perenesenyye frazy: okno v turns, gde base-klaster zamenyaetsya
        # novoy metkoy — inache expand_segments pri rendere razrezebyu stroku
        # po starey diagramme i vyyvod rassoglasuetsya s resortom
        for i, (p, lbl) in enumerate(zip(parts, labels)):
            if lbl != base and emb_cache.get(i):
                turn_edits.append((float(p["s"]), float(p["e"]), lbl, base))
        for lbl, grp in grouped:
            if lbl not in centroids:
                vecs = [emb_cache[i] for i, _p in grp if emb_cache.get(i)]
                if vecs:
                    centroids[lbl] = [sum(v[k] for v in vecs) / len(vecs)
                                      for k in range(len(vecs[0]))]
        # segment sohanyaet pervuyu gruppirovku (nachalo stroke), ostal'nyye
        # idut novymi segmenty; pri smene golosa v nachale i segment menyaet
        # klaster na pervoy gruppy
        anchor_lbl, anchor_grp = grouped[0]
        rest = grouped[1:]
        if anchor_lbl != base:
            name, source = name_for(anchor_lbl)
            seg.speaker_id = anchor_lbl
            seg.diar_label = anchor_lbl
            seg.speaker_name = name
            seg.speaker_source = source
            seg.needs_review = (source != "voiceprint")
            ensure_review(anchor_lbl)
        seg.text = join_parts([p for _i, p in anchor_grp])
        seg.asr_json = [p for _i, p in anchor_grp]
        seg.t_start = float(anchor_grp[0][1]["s"])
        seg.t_end = float(anchor_grp[-1][1]["e"])
        for lbl, grp in rest:
            name, source = name_for(lbl)
            s.add(Segment(job_id=job_id, speaker_id=lbl, diar_label=lbl,
                          speaker_name=name, speaker_source=source,
                          needs_review=(source != "voiceprint"),
                          t_start=float(grp[0][1]["s"]),
                          t_end=float(grp[-1][1]["e"]),
                          text=join_parts([p for _i, p in grp]),
                          lang=seg.lang, engine=seg.engine,
                          confidence=seg.confidence,
                          asr_json=[p for _i, p in grp]))
            created += 1
            ensure_review(lbl)
    if turn_edits:
        # turns — obstvennost' job (shkaly vsey zapisi): pechatayem perenosy,
        # chtoby stenogramma/protokol (expand_segments) reshli po utochnennoy
        # diagramme, a ne po ishodnoy
        job = s.get(Job, job_id)
        if job is not None and job.diar_turns:
            job.diar_turns = _apply_turn_edits(job.diar_turns, turn_edits)
    return created


def _embed_min_s() -> float:
    """EMBED_MIN_S: minimal'naya dlitel'nost' fragmenta dlya embeddinga (s)."""
    try:
        return float(os.environ.get("EMBED_MIN_S", "1.5") or 1.5)
    except ValueError:
        return 1.5


EMBED_MAX_SEGS = 10        # luchshih fragmentov na klaster (kachestvo > skorost')


def _cluster_embedding(wav: Path, segs: list, turns: list | None = None
                       ) -> list[float] | None:
    """Srednee po LUCHSHIM cistym fragmentam klastera.

    Ranshe bralis' pervye 5 segmetov >= 1.5 s — samye rannie, a ne samye
    bogatye; teper' otbor po dlitel'nosti. Esli yest' turns diagramizatsii,
    v embedding berezhetsya tol'ko chast' segmenta, pokrytaya turnom klastera
    (chisty podinterval >= EMBED_MIN_S); segmenty s na lozheniem propuskayutsya."""
    from ..core.vad import extract_wav_slice
    from ..core.diarize import label_at_turns
    import tempfile
    cands: list[tuple[float, float, float]] = []  # (dur, t0, t1)
    overlap_min = 0.7
    for seg in segs:
        if seg.speaker_source in ("multi_voice", "voice_mix"):
            continue
        t0, t1 = seg.t_start, seg.t_end
        if turns:
            label, ratio = label_at_turns(turns, t0, t1)
            if ratio >= overlap_min and label:
                clean = _turn_subrange(turns, t0, t1, label)
                if clean is None:
                    continue
                t0, t1 = clean
            else:
                continue  # na lozhenie / metka ne yasna — v noysnye etalony ne idyot
        if t1 - t0 >= _embed_min_s():
            cands.append((t1 - t0, t0, t1))
    cands.sort(reverse=True)
    cands = cands[:EMBED_MAX_SEGS]
    if not cands:
        return None
    vecs = []
    for _dur, t0, t1 in cands:
        with tempfile.NamedTemporaryFile(suffix=".wav") as tf:
            extract_wav_slice(wav, t0, t1, Path(tf.name))
            from ..core.speaker import compute_embedding_for_file
            vec = compute_embedding_for_file(Path(tf.name))
        if vec:
            vecs.append(vec)
    if not vecs:
        return None
    n = len(vecs[0])
    mean = [sum(v[i] for v in vecs) / len(vecs) for i in range(n)]
    import math
    norm = math.sqrt(sum(x * x for x in mean)) or 1.0
    return [x / norm for x in mean]


def _turn_subrange(turns: list, t0: float, t1: float, label: str
                   ) -> tuple[float, float] | None:
    """Samyy dlinnyy podinterval [t0, t1], pokrytyy turnom `label`."""
    best: tuple[float, float] | None = None
    for ts, te, spk in turns:
        if spk != label:
            continue
        a, b = max(ts, t0), min(te, t1)
        if b - a <= 0:
            continue
        if best is None or (b - a) > (best[1] - best[0]):
            best = (a, b)
    return best


def _write_steno(job_id: int, p: dict) -> Path:
    with get_session() as s:
        segs = s.scalars(select(Segment).where(
            Segment.job_id == job_id, Segment.excluded.is_(False)
        ).order_by(Segment.t_start)).all()
        job = s.get(Job, job_id)
    lines = [f"# Протокол (транскрипт): {job.topic}", ""]
    from ..core.coalesce import coalesce_segments, expand_segments, speaker_display
    for seg in coalesce_segments(expand_segments(list(segs),
                                                 job.diar_turns if job else None)):
        tag = (" (dva golosa v fragmente)" if getattr(seg, "two_voices", False) else "") + \
            (" (trebuetsya utochnenie)" if getattr(
                seg, "pending_review",
                bool(seg.needs_review) and not getattr(seg, "two_voices", False))
             else "")
        lines.append(f"{_fmt(seg.t_start)} {speaker_display(seg)}{tag}: {seg.text}")
    p["steno"].write_text("\n".join(lines), encoding="utf-8")
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event="steno_done",
                           detail=str(p["steno"])))
        s.commit()
    return p["steno"]


def _fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def _archive_day(date_value: dt.datetime | None) -> str:
    """Katalog arhiva po dnyu lokal'nogo poyasa (FT-20): date iz BDB — naivnyy
    UTC, vechernyaya vstrecha (23:xx MSK) inache popadayet v predydushchiy den'.
    """
    if date_value is None:
        return "unknown"
    from ..core.tz import to_local
    return to_local(date_value).strftime("%Y-%m-%d")


def _run_structure(job_id: int) -> None:
    """FT-17: LLM protocol. Pri llm=not_configured/misconfigured -> awaiting_llm."""
    from ..core.capabilities import capability_status
    from ..core.pipeline import transition
    p = _job_paths(job_id)
    steno = p["steno"] if p["steno"].exists() else _write_steno(job_id, p)
    caps = capability_status()
    if caps["llm"]["state"] != "configured":
        with get_session() as s:
            proto = s.scalars(select(Protocol).where(Protocol.job_id == job_id)).first()
            if proto is None:
                proto = Protocol(job_id=job_id, json_data={"transcript_only": True},
                                 steno_md_path=str(steno))
                s.add(proto)
            else:
                proto.steno_md_path = str(steno)
            s.commit()
        transition(job_id, "awaiting_llm")
        return
    try:
        _build_protocol(job_id)
    except Exception as e:  # noqa: BLE001
        # LLMvernul neuralid Nson / otsvet ne doshol: stenogramma uzhe na
        # diske — ne tonim job, a dosylаем v awaiting_llm (donastraivaemyj shut).
        from ..core.llm_client import LLMError
        if not isinstance(e, LLMError):
            transition(job_id, "failed", fail_reason="llm_invalid_json",
                       error=str(e)[:1000], event="structure_error")
            raise
        _log_event(job_id, "llm_error", f"LLM не вернул протокол: {str(e)[:300]}")
        with get_session() as s:
            proto = s.scalars(select(Protocol).where(Protocol.job_id == job_id)).first()
            if proto is None:
                proto = Protocol(job_id=job_id, json_data={"transcript_only": True},
                                 steno_md_path=str(steno))
                s.add(proto)
            else:
                proto.steno_md_path = str(steno)
            s.commit()
        transition(job_id, "awaiting_llm")


def _get_job(job_id: int) -> Job:
    with get_session() as s:
        return s.get(Job, job_id)


def _llm_roster(s, segs: list) -> str:
    """Spravka 'FIО — dolzhnost' po uchastnikam vstrechi (dlya LLM-konteksta).

    Tol'ko te, kogo opoznali v segmentakh i u kogo zapolnena dolzhnost'.
    V protokole/otchete ne otrazhaetsya."""
    from ..core.models import VoiceProfile
    names = {seg.speaker_name for seg in segs if seg.speaker_name}
    if not names:
        return ""
    rows = s.scalars(select(VoiceProfile).where(
        VoiceProfile.employee_name.in_(names))).all()
    return "; ".join(f"{v.employee_name} — {v.position}"
                     for v in sorted(rows, key=lambda v: v.employee_name)
                     if v.position)


def _prev_protocol_digest(s, job_id: int, topic: str) -> tuple[str, str]:
    """Kratkoe soderzhanie protokola predydushchey vstrechi s tey zhe temoy.

    Vozvraschaet (digest, data_ DD.MM.YYYY); ('', '') esli predka net.
    Digest: data + reshenniya + zadachi + otkrytyye voprosy + temy svodok
    predydushchego protokola — bazy LLM dlya bloka changes."""
    from ..core.models import Protocol
    from ..core.tz import to_local
    from sqlalchemy import func
    like = (topic or "").strip()
    if not like:
        return "", ""
    cur = s.get(Job, job_id)
    cur_when = func.coalesce(cur.ended_ts, cur.start_at, cur.created_ts) if cur \
        else None
    q = (select(Protocol)
         .join(Job, Protocol.job_id == Job.id)
         .where(Job.id != job_id, Job.topic == like))
    if cur_when is not None:
        q = q.where(func.coalesce(Job.ended_ts, Job.start_at, Job.created_ts)
                    < cur_when)
    prev = s.execute(q.order_by(
        func.coalesce(Job.ended_ts, Job.start_at, Job.created_ts).desc()
    ).limit(1)).scalar_one_or_none()
    if prev is None or not prev.json_data:
        return "", ""
    d = prev.json_data
    prev_job = s.get(Job, prev.job_id)
    when = (prev_job.ended_ts or prev_job.start_at) if prev_job else None
    prev_date = to_local(when).strftime("%d.%m.%Y") if when else (d.get("date") or "")
    parts: list[str] = []
    for x in (d.get("decisions") or [])[:20]:
        parts.append(f"RESHENO: {x}")
    for ai in (d.get("action_items") or [])[:30]:
        if isinstance(ai, dict):
            owner = ai.get("owner") or "—"
            due = ai.get("due") or "—"
            parts.append(f"ZADACHA: {ai.get('task', '')} ({owner}, {due})")
    for q in (d.get("open_questions") or [])[:20]:
        parts.append(f"OTKRYTY VOPROS: {q}")
    digest = "\n".join(parts)
    return digest[:6000], prev_date


def _real_duration(segs: list) -> float:
    """Dlitel'nost' po fakticheskim segmentam (konets posledney repliki).

    LLM periodicheski vozvrashchaet duration v minutah ('36') vmesto sekund —
    fakticheskie dannye stochnye."""
    if not segs:
        return 0.0
    return round(max(float(getattr(s, "t_end", 0) or 0) for s in segs))


def _build_protocol(job_id: int) -> None:
    from ..core.llm_client import structure_transcript, llm_model
    from ..core.protocol_md import assemble_protocol
    from ..core.pipeline import transition
    from ..core.models import VoiceProfile
    p = _job_paths(job_id)
    with get_session() as s:
        segs = s.scalars(select(Segment).where(
            Segment.job_id == job_id, Segment.excluded.is_(False)
        ).order_by(Segment.t_start)).all()
        job = s.get(Job, job_id)
        roster = _llm_roster(s, segs)
        prev_digest, prev_date = _prev_protocol_digest(s, job_id, job.topic)
    transcript = _transcript_lines(segs, job.diar_turns if job else None)
    from ..core.tz import to_local
    meeting_when = job.ended_ts or job.start_at if job else None
    meeting_date = (to_local(meeting_when).strftime("%d.%m.%Y")
                    if meeting_when else "")
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event="llm_start",
                           detail=f"сегментов: {len(segs)}"))
        s.commit()
    data = structure_transcript(transcript, topic=job.topic, roster=roster,
                                meeting_date=meeting_date,
                                prev_digest=prev_digest)
    if prev_digest and prev_date:
        ch = data.setdefault("changes", {"relative_to": "", "continuing": [],
                                         "progress": [], "new": [],
                                         "unconfirmed": []})
        if not (ch.get("relative_to") or "").strip():
            ch["relative_to"] = prev_date
    real_dur = _real_duration(list(segs))
    if real_dur:
        data["duration"] = real_dur
    archive_dir = Path(os.environ.get("ARCHIVE_DIR", "/data/archive"))
    day = _archive_day(job.ended_ts or job.start_at)
    slug = "".join(c if c.isalnum() else "-" for c in (job.topic or f"job{job_id}"))[:40] or f"job{job_id}"
    out = archive_dir / day / slug
    md, docx = assemble_protocol(job, data, segs, out)
    with get_session() as s:
        proto = s.scalars(select(Protocol).where(Protocol.job_id == job_id)).first()
        if proto is None:
            proto = Protocol(job_id=job_id)
            s.add(proto)
        proto.json_data = data
        proto.md_path = md
        proto.docx_path = docx
        proto.steno_md_path = str(p["steno"])
        proto.llm_model = llm_model()
        s.add(SessionEvent(job_id=job_id, event="llm_done",
                           detail=f"модель: {llm_model()}"))
        s.commit()
    from ..core import export
    export.export_protocol_files(job_id)  # zerkalo: vnutri try/except
    # utochneniya (4.6) ili done
    _route_after_structure(job_id)


def _route_after_structure(job_id: int) -> None:
    from ..core.pipeline import transition
    if not _route_awaiting_review_if_unresolved(job_id):
        transition(job_id, "done")


def _transcript_lines(segs: list, turns: list | None = None) -> str:
    from ..core.coalesce import coalesce_segments, expand_segments, speaker_display
    out = []
    for seg in coalesce_segments(expand_segments(list(segs), turns)):
        mix = " [два голоса]" if getattr(seg, "two_voices", False) else ""
        out.append(f"{_fmt(seg.t_start)} {speaker_display(seg)}{mix}: {seg.text}")
    return "\n".join(out)
