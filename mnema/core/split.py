"""Разделение одной записи на несколько встреч (этап split).

Одна запись (непрерывный созвон) может содержать несколько совещаний подряд.
После стенограммы/уточнения голосов LLM ищет границы между совещаниями;
хвосты переезжают в дочерние Job'ы (parent_job_id), время сегментов
пересчитывается от 0, аудио режется ffmpeg-слайсами от полной копии
meeting-full.*. Дальше каждая часть обрабатывается независимо.

Идемпотентность rerun: решённый job имеет split_json (границы или skipped).
Ошибки аудио (ffmpeg) НЕ роняют job: событие split_audio_error, разделение
в БД выполняется без аудио-слайсов (протокол важнее).

Сбой применения границ (не детекции) — SplitApplyError: уже созданные потомки
передаются вызывающему (чтобы они не остались без run_structure), а в
split_json родителя фиксируется частичное решение (partial=True) и событие
split_error — rerun не создаст дубликатов.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import select, update

from .db import get_session
from .models import AudioFile, Job, Segment, SessionEvent


def split_enabled() -> bool:
    """kill-switch SPLIT_MEETINGS (по умолчанию включён)."""
    return os.environ.get("SPLIT_MEETINGS", "1").strip() != "0"


def _min_minutes() -> float:
    try:
        return float(os.environ.get("SPLIT_MIN_MINUTES", "20") or 20)
    except ValueError:
        return 20.0


def min_confidence() -> float:
    try:
        return float(os.environ.get("SPLIT_MIN_CONFIDENCE", "0.7") or 0.7)
    except ValueError:
        return 0.7


MIN_SEGMENTS = 10          # меньше реплик — делить нечего
MIN_TAIL_S = 120.0         # хвост короче двух минут — потомок не создаём


class SplitApplyError(Exception):
    """Границы приняты, но применение сорвалось на середине.

    child_ids — потомки, уже созданные и коммитнутые (их сегменты переехали):
    вызывающий обязан поставить им run_structure, иначе они зависнут в
    processing. split_json родителя помечен partial — rerun не повторит
    разделение и не создаст дубликатов."""

    def __init__(self, message: str, child_ids: list[int]):
        super().__init__(message)
        self.child_ids = list(child_ids)



def silence_gap_s() -> float:
    """Пауза между репликами (с), с которой в стенограмму для LLM вставляются
    метки тишины — длинная пауза сильный признак границы встреч."""
    try:
        return float(os.environ.get("SPLIT_SILENCE_GAP_S", "60") or 60)
    except ValueError:
        return 60.0


def _transient(reason: str) -> bool:
    """Причины, которые НЕ пишем в split_json: после перенастройки rerun
    должен получить шанс разделить (kill-switch, LLM не настроен)."""
    return reason in ("disabled", "llm_not_configured")


def should_try_split(job, segs: list) -> str | None:
    """None — можно пробовать разделить; иначе — причина пропуска."""
    if not split_enabled():
        return "disabled"
    if job.split_json is not None:
        return "already_decided"
    if job.parent_job_id is not None:
        return "child_job"
    if len(segs) < MIN_SEGMENTS:
        return "too_few_segments"
    dur = max((s.t_end for s in segs), default=0.0)
    if dur < _min_minutes() * 60.0:
        return "too_short"
    from .capabilities import capability_status
    if capability_status()["llm"]["state"] != "configured":
        return "llm_not_configured"
    return None


def snap_boundary(segs: list, t: float, window_s: float = 120.0) -> float:
    """Граница «прищёлкивается» к паузе между сегментами VAD.

    Попадание в интервал [prev.t_end, next.t_start] — режем по t_start
    следующего сегмента. Внутри речи — сдвиг к максимуму gap'а вокруг
    кандидата (±window_s). Вне диапазона — ближайший край."""
    if not segs:
        return t
    ordered = sorted(segs, key=lambda s: s.t_start)
    for i in range(len(ordered) - 1):
        if ordered[i].t_end <= t <= ordered[i + 1].t_start:
            return ordered[i + 1].t_start
    if t <= ordered[0].t_start:
        return ordered[0].t_start
    if t >= ordered[-1].t_end:
        return ordered[-1].t_end
    best: tuple[float, int] | None = None
    for i in range(len(ordered) - 1):
        gap_mid = (ordered[i].t_end + ordered[i + 1].t_start) / 2
        if abs(gap_mid - t) > window_s:
            continue
        gap = max(0.0, ordered[i + 1].t_start - ordered[i].t_end)
        if best is None or gap > best[0]:
            best = (gap, i)
    if best is None:
        for i in range(len(ordered) - 1, -1, -1):
            if ordered[i].t_start <= t:
                return ordered[i].t_start
        return t
    return ordered[best[1] + 1].t_start


def _rebase_child_asr_json(s, child_id: int, b: float, hi: float) -> None:
    """Parts v asr_json potomka: roditel'skie absolqutnyye `s`/`e` -> lokal'nyye
    shkaly childa (minus b, obrezka k [0, hi-b]). Pustye posle obrizki parts
    sbrasyvaemsya v None (togda segment idet kak celaya repika bez razbivki)."""
    segs = s.scalars(select(Segment).where(
        Segment.job_id == child_id, Segment.asr_json.isnot(None))).all()
    lo_lim, hi_lim = 0.0, max(0.0, hi - b)
    for seg in segs:
        if not seg.asr_json:
            continue
        rebased = []
        for p in seg.asr_json:
            try:
                ps = float(p.get("s", 0)) - b
                pe = float(p.get("e", 0)) - b
            except (TypeError, ValueError):
                continue
            ps = max(lo_lim, ps)
            pe = min(hi_lim, pe)
            if pe - ps <= 0.01:
                continue
            q = dict(p)
            q["s"] = round(ps, 3)
            q["e"] = round(pe, 3)
            rebased.append(q)
        seg.asr_json = rebased or None


def _clip_turns(turns: list | None, lo: float, hi: float) -> list | None:
    """Диа-turns диапазона [lo, hi) с реbase в локальное время части."""
    if not turns:
        return None
    out = []
    for ts, te, label in turns:
        a, b = max(float(ts), lo), min(float(te), hi)
        if b - a > 0:
            out.append([a - lo, b - lo, label])
    return out


def _event(job_id: int, event: str, detail: str) -> None:
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event=event, detail=detail[:1000]))
        s.commit()


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def _persist_skip(job_id: int, reason: str) -> None:
    """Разделять НЕ будем: причина в split_json + событие split_skipped.

    Преходящие причины (см. _transient) в split_json НЕ пишутся — иначе
    rerun после настройки LLM навсегда упрётся в «уже решено»."""
    if not _transient(reason):
        with get_session() as s:
            job = s.get(Job, job_id)
            if job is not None and job.split_json is None:
                job.split_json = {
                    "skipped": reason,
                    "ts": dt.datetime.now(dt.timezone.utc).isoformat()}
            s.commit()
    _event(job_id, "split_skipped", reason)


def _dedupe_boundaries(boundaries: list[dict], min_gap_s: float = 60.0) -> list[dict]:
    from .llm_client import merge_boundary_candidates
    return merge_boundary_candidates(boundaries, min_gap_s)


def _abort_partial(job_id: int, accepted: list[dict],
                   child_ids: list[int], error: str) -> None:
    """Применение границ сорвалось: фиксируем состояние, чтобы rerun не создал
    дубликаты уже отделившихся хвостов. Ни один потомок не коммитнут —
    решение НЕ пишем: rerun получит чистый шанс. Committed хвосты — последние
    len(child_ids) границ (хвосты снимались справа налево)."""
    if child_ids:
        try:
            with get_session() as s:
                job = s.get(Job, job_id)
                if job is not None and job.split_json is None:
                    job.split_json = {
                        "boundaries": [
                            {"t": float(bb["t"]),
                             "confidence": float(bb["confidence"]),
                             "topic_before": job.topic or "",
                             "topic_after": bb.get("topic_after") or ""}
                            for bb in accepted[len(accepted) - len(child_ids):]],
                        "children": sorted(child_ids),
                        "partial": True,
                        "error": error[:500],
                        "ts": dt.datetime.now(dt.timezone.utc).isoformat()}
                s.commit()
        except Exception:  # noqa: BLE001 — маркировка best effort
            pass
    try:
        _event(job_id, "split_error",
               f"применение границ сорвалось "
               f"(создано потомков: {len(child_ids)}): {error[:300]}")
    except Exception:  # noqa: BLE001 — событие не должно перекрывать причину
        pass



def _child_topic(boundary: dict, idx: int, parent_topic: str) -> str:
    """Тема потомка из LLM; fallback «<тема родителя> (часть N)» (N=2..)."""
    topic = (boundary.get("topic_after") or "").strip()
    if topic:
        return topic[:512]
    base = parent_topic.strip() or "Встреча"
    return f"{base} (часть {idx + 2})"[:512]


def _ensure_full_copy(p: dict) -> None:
    """Полная копия meeting-full.* — единственный шанс сохранить запись
    целиком при усечении (raw.* мог быть удалён KEEP_UPLOADED_SOURCE=0)."""
    for key, full_key in (("wav", "full_wav"), ("mp3", "full_mp3")):
        full: Path = p[full_key]
        if not full.exists() and p[key].exists():
            shutil.copyfile(str(p[key]), str(full))


def _slice_audio(job_id: int, src: Path, dst: Path, lo: float, hi: float) -> bool:
    """Фрагмент [lo, hi) исходника в dst. Ошибка — событие, не исключение."""
    if not src.exists():
        return False
    from .audio import media_slice
    fd, tmp_name = tempfile.mkstemp(suffix=dst.suffix, dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        media_slice(src, lo, hi, tmp)
        shutil.move(str(tmp), str(dst))
        return True
    except Exception as e:  # noqa: BLE001 — аудио не роняет разделение
        tmp.unlink(missing_ok=True)
        _event(job_id, "split_audio_error",
               f"{dst.name}: {str(e)[:300]}")
        return False


def _register_audio(job_id: int, p: dict, dur: float, kinds: tuple[str, ...]) -> None:
    with get_session() as s:
        for kind in kinds:
            path: Path = p[kind]
            if not path.exists():
                continue
            exists = s.scalars(select(AudioFile).where(
                AudioFile.job_id == job_id, AudioFile.kind == kind)).first()
            if exists is None:
                exists = AudioFile(job_id=job_id, kind=kind, path=str(path))
                s.add(exists)
            exists.bytes = path.stat().st_size
            exists.duration_s = dur
        s.commit()


def _model_name() -> str | None:
    from .llm_client import LLMError, llm_model
    try:
        return llm_model()
    except LLMError:
        return None


def _trim_straddlers(s, job_id: int, b: float) -> None:
    """Реплика, начатая до границы и окончившаяся после неё, остаётся
    предыдущей части, но обрезается по границу: текст не выходит за усечённое
    аудио части (хвост слышен потомку, текст — нет)."""
    s.execute(update(Segment)
              .where(Segment.job_id == job_id,
                     Segment.t_start < b, Segment.t_end > b)
              .values(t_end=b))


def split_job(job_id: int) -> list[int]:
    """Главная функция этапа: детекция границ + разделение.

    Возвращает id созданных потомков (пусто — разделения нет/пропуск).
    LLMError из детекции — наружу (caller пишет split_skipped и не роняет
    job); всё после «граница принята» не откатывается."""
    from .llm_client import detect_meeting_boundaries
    from ..worker.tasks import _job_paths

    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return []
        segs = s.scalars(select(Segment).where(
            Segment.job_id == job_id).order_by(Segment.t_start, Segment.id)).all()
        duration_s = max((s_.t_end for s_ in segs), default=0.0)
        job_topic = job.topic or ""
        parent_start = job.start_at
        parent_turns = job.diar_turns

    reason = should_try_split(job, segs)
    if reason is not None:
        if reason != "already_decided":
            _persist_skip(job_id, reason)
        return []

    _event(job_id, "split_start", f"длительность: {duration_s:.0f} с")
    transcript = _transcript_for_split(job_id)
    found = detect_meeting_boundaries(transcript, topic=job_topic,
                                      duration_s=duration_s)
    cutoff = min_confidence()
    accepted = [b for b in found
                if b["confidence"] >= cutoff and b["t"] > 0
                and duration_s - b["t"] >= MIN_TAIL_S]
    for b in accepted:
        b["t"] = snap_boundary(segs, float(b["t"]))
    accepted = _dedupe_boundaries([b for b in accepted
                                   if 0 < b["t"] <= duration_s - MIN_TAIL_S])
    if not accepted:
        _persist_skip(job_id, "no_boundaries_found")
        return []

    p = _job_paths(job_id)
    _ensure_full_copy(p)

    child_ids: list[int] = []
    # справа налево: сегменты переезжают ровно один раз (хвост уже снят с
    # родителя предыдущим шагом), пересчёт t=0 — один раз на хвост.
    # Сбой применения — SplitApplyError: уже созданные потомки не должны
    # остаться без run_structure, решение фиксируется partial (без дубликатов).
    try:
        for idx in range(len(accepted) - 1, -1, -1):
            b = float(accepted[idx]["t"])
            hi = float(accepted[idx + 1]["t"]) if idx + 1 < len(accepted) else duration_s
            with get_session() as s:
                _trim_straddlers(s, job_id, b)
                child = Job(source="split", parent_job_id=job_id,
                            topic=_child_topic(accepted[idx], idx, job_topic),
                            status="processing", created_by="split",
                            start_at=(parent_start + dt.timedelta(seconds=b)
                                      if parent_start else None),
                            split_json={"parent": job_id, "boundary_t": b,
                                        "ts": dt.datetime.now(
                                            dt.timezone.utc).isoformat()})
                s.add(child)
                s.flush()
                cid = child.id
                s.execute(update(Segment)
                          .where(Segment.job_id == job_id, Segment.t_start >= b)
                          .values(job_id=cid,
                                  t_start=Segment.t_start - b,
                                  t_end=Segment.t_end - b))
                # H2: asr_json.parts хранyat absolqutnyye taimstAMPy roditelya;
                # ih nado perestavit' na shkaly potomka (-b) i ogranichit' [0, hi-b].
                # Inache coalesce/phrase-resort dayut repiki za predelami diar turns
                # i nevertyye t_start pri klike v pleere.
                _rebase_child_asr_json(s, cid, b, hi)
                s.commit()
            child_ids.append(cid)
            # 7b: пресет этапов-предков — иначе stages потомка вечно показывают
            # prep/asr/speaker «running» (vetki у потомка не запускаются)
            for ev in ("vad_done", "asr_done", "diar_done", "voiceprint_done",
                       "review_resolved"):
                _event(cid, ev, f"унаследовано от job {job_id}")
            cp = _job_paths(cid)
            got = tuple(
                kind for kind, src in (("wav", p["full_wav"]), ("mp3", p["full_mp3"]))
                if _slice_audio(cid, src, cp[kind], b, hi))
            dur = _slice_duration(cp, got)
            _register_audio(cid, cp, dur, got)
            with get_session() as s:
                child = s.get(Job, cid)
                if child is not None:
                    child.diar_turns = _clip_turns(parent_turns, b, hi)
                    s.commit()

        # решение 6: пустая тема родителя заполняется, когда разделение состоялось
        with get_session() as s:
            job = s.get(Job, job_id)
            if job is not None and not (job.topic or "").strip():
                job.topic = f"Встреча {job_id} (часть 1)"[:512]
            s.commit()

        # родитель: усечение аудио [0, b1) (источник — полная копия) + diar + json
        b1 = float(accepted[0]["t"])
        parent_got = tuple(
            kind for kind, src in (("wav", p["full_wav"]), ("mp3", p["full_mp3"]))
            if _slice_audio(job_id, src, p[kind], 0.0, b1))
        if parent_got:
            _register_audio(job_id, p, _slice_duration(p, parent_got), parent_got)
        with get_session() as s:
            job = s.get(Job, job_id)
            if job is not None:
                job.diar_turns = _clip_turns(parent_turns, 0.0, b1)
                job.split_json = {
                    "boundaries": [{"t": float(bb["t"]),
                                    "confidence": float(bb["confidence"]),
                                    "topic_before": job_topic,
                                    "topic_after": bb.get("topic_after") or ""}
                                   for bb in accepted],
                    "model": _model_name(),
                    "skipped": None,
                    "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            s.commit()
    except Exception as e:  # noqa: BLE001 — не бросать потомков без structure
        _abort_partial(job_id, accepted, child_ids, str(e))
        raise SplitApplyError(str(e), child_ids) from e
    from . import export
    for cid in child_ids:
        export.export_recording(cid)
    export.export_recording(job_id)  # перезапись усечённым (идемпотентность по export_json.src_size)
    _event(job_id, "split_done",
           "границы: " + "; ".join(f"{_fmt(b['t'])} (c={b['confidence']:.2f})"
                                   for b in accepted)
           + f"; дочерние: {', '.join(str(c) for c in reversed(child_ids))}")
    return list(reversed(child_ids))


def _slice_duration(p: dict, kinds: tuple[str, ...]) -> float:
    if "wav" in kinds and p["wav"].exists():
        from .audio import wav_duration_s
        d = wav_duration_s(p["wav"])
        if d > 0:
            return d
    return 0.0


def _transcript_for_split(job_id: int) -> str:
    from ..worker.tasks import _fmt, _transcript_lines
    from .coalesce import coalesce_segments, expand_segments, speaker_display
    with get_session() as s:
        job = s.get(Job, job_id)
        turns = job.diar_turns if job is not None else None
        segs = s.scalars(select(Segment).where(
            Segment.job_id == job_id, Segment.excluded.is_(False)
        ).order_by(Segment.t_start, Segment.id)).all()
    gap = silence_gap_s()
    expanded = expand_segments(list(segs), turns)
    if gap <= 0:
        return _transcript_lines(segs, turns)
    disp = coalesce_segments(expanded)
    out: list[str] = []
    prev_end: float | None = None
    for seg in disp:
        if prev_end is not None and seg.t_start - prev_end >= gap:
            out.append(f"[ТИШИНА {_fmt(prev_end)} — {_fmt(seg.t_start)} "
                       f"({seg.t_start - prev_end:.0f} c)]")
        out.append(f"{_fmt(seg.t_start)} {speaker_display(seg)}: {seg.text}")
        prev_end = max(prev_end or 0.0, seg.t_end)
    return "\n".join(out)
