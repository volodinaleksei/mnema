"""LLM-klient (FT-17/FT-40): Ollama/OpenAI-sovmetimy /v1/chat/completions.

map-reduce pri stenograme > konteksta. JSoN-shema protokola; zapreshchenie
vydumyvat' imena i fakty; LLM_MODEL_ALLOWLIST — gejt modeley.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import time

import httpx

log = logging.getLogger("mnema.llm")

# Global'nyy byudzhet vremenidlya vsey zadachi LLM: retri + ozhidanie zamka +
# kazhdoe okno map-reduce. Bez nego job mozhet sidet' v ocheredi godya svershe
# RQ default_timeout (4 ch) i byt' SIGKILL'ded poseredine obrabotki.
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "llm_deadline", default=None)


def _budget_exceeded() -> bool:
    d = _deadline.get()
    return d is not None and time.monotonic() >= d


@contextlib.contextmanager
def llm_budget(seconds: float):
    """Byudzhet na vse LLM-zaprosy vnutri bloka (sek). Vlozhennye bloki berut
    bolshiy (samyy ranliy) dyaadlayn — vtoroy ne prodlevayet total'noe ozhidanie."""
    prev = _deadline.get()
    new = prev if prev is not None else time.monotonic() + max(1.0, seconds)
    _deadline.set(new)
    try:
        yield
    finally:
        _deadline.set(prev)

SYSTEM_PROMPT = (
    "Ty formatshto protokola delovoy vstrechi. Vozvrashi ISTORIMO validny JSON "
    "po skHEME bez kakih-lib poyasneniy. Ne vydumyivay tovo, cheg nie bylo v stenogramme. "
    "Otvetstvennykh beri IZ IMEN stenogrammy; esli ne opredelen — null. Dlya kazhdoi "
    "zadachi ukazyvai source_ts — vremyanoy meetka isodnoy repliki (CHCH:MM:SS). "
    "Sroki (due): YESLI v message est' DATA SOVESHCHEANIYA, otnositel'nye sroki iz "
    "stenogrammy ('zavtra', 'v pyatnicu', 'do konceledi', 'na sledushchey nedele') "
    "VYCHISLYI ot etoy daty i zapishi due REALNOY DATEY v formate DD.MM.YYYY; "
    "esli srok vovsche nie nazvan — null. V pole date vsegda stavi datu vstrechi "
    "iz DATA SOVESHCHEANIYA (esli ona ukazana). "
    "Duration — dlitel'nost' vstrechi v sekundakh tselym chislom (36 minut = 2160). "
    "topic_summaries — GLAVNYY, SAMYI PODROBNYY razdel protokola: po kazhdoi teme povestki "
    "napishi svyazny abzac 3-6 predlozheniy. SOHRANYAI v svodke VSE soderzhanie obsuzhdeniya: "
    "chisla, summy (mln rub., tys. rub., %, kratnost'), daty, nazvaniya proektov i kontragentov, "
    "varianty i diapazony otsenok ('ot plus 20% do pochti udvoeniya', 'mart ili noyabr') — "
    "NE sklejki varianty v odin i NE vybirai za lyudey. Obyazatel'no otmechai status kazhdogo "
    "fakta markerami: 'ne podtverzhdeno', 'podtverzhdeniya net', 'soglasovano predvaritel'no', "
    "'ozhidaetsya, no poka ne sostoialos', 'eto risk, a ne garantirovannaya data', "
    "'otsenka, finel'noy tseny net', 'eti raschety nel'zya smeshivat''. Esli v svodke est' "
    "neskol'ko nesvyazannykh raschetov/tsifr — perechisli ikh otdel'no s tochnymi znacheniyami. "
    "V source_ts svodki — diapazon teki vstreti 'CHCH:MM:SS-CHCH:MM:SS'. "
    "decisions — tol'ko fakicheski prinyatye resheniya i zaprety; esli pri reshenii nazvali "
    "usloviya, tsify ili sroki — vklyuchi ikh v formulyrovku resheniya. "
    "action_items: ODNO poruchenie = ODNA stroka; porucheniya raznym lyudyam ili raznym "
    "deystviyam NE ob\"edinyay v odnu; v teke kazhdoi zadachi SOHRANYAI vse podrobnosti, "
    "tsify, summy i usloviya, proiznesennye pri poruchenii. "
    "changes: YESLI v zaprose prilojen PROTOKOL PREDYDUSHCHEGO SOVESHCHEANIYA — sravni tekyshuyu "
    "vstrechu s nim i zapolni: continuing — prodolzheniya (temy, chto byli v poveske i eshcho ne "
    "zakryty), progress — prodvizhenie (shto sdvinulos s merta), new — polnost'yu novye temy, "
    "unconfirmed — punkty iz togo protokola, ispolnenie kotoryh na etoy vstreche NE podtverzhdeno. "
    "V relative_to — data predydushchego sovieshchaniya. Esli predydushchego protokola net — "
    "spiski v changes pusty, relative_to pustaya stroka. "
    "SkHEMA: {\"topic\": str, \"date\": str, \"duration\": float, "
    "\"attendees\": [{\"name\",\"first_seen\",\"last_seen\"}], \"agenda\": [str], "
    "\"topic_summaries\": [{\"topic\",\"summary\",\"source_ts\"}], \"decisions\": [str], "
    "\"action_items\": [{\"task\",\"owner\",\"due\",\"source_ts\"}], "
    "\"open_questions\": [str], "
    "\"changes\": {\"relative_to\": str, \"continuing\": [str], \"progress\": [str], "
    "\"new\": [str], \"unconfirmed\": [str]}}"
)


def _meeting_date_block(meeting_date: str) -> str:
    meeting_date = (meeting_date or "").strip()
    if not meeting_date or meeting_date.lower() in ("null", "none"):
        return ""
    return (f"DATA SOVESHCHEANIYA: {meeting_date} (DD.MM.YYYY). "
            f"Otnositel'nye sroki iz stenogrammy ischislyay ot noy i stav v due "
            f"kak real'nyye daty DD.MM.YYYY.\n\n")


PROTOCOL_JSON_KEYS = {"topic", "date", "duration", "attendees", "agenda",
                      "decisions", "action_items", "open_questions"}

CHANGES_KEYS = ("relative_to", "continuing", "progress", "new", "unconfirmed")


class LLMError(Exception):
    pass


class LLMGateWaitError(LLMError):
    """Ozhidanie ocheredi k LLM ischerpao LLM_LOCK_WAIT_TIMEOUT_S. Nerepetitno:
    vtoraya popytka zanjala by ochered' snova, umnozhaya ozhidanie."""


_THINK_REJECTED = False  # server ne znaet parametr think — bolshe ne peredavat'


def _thinking_enabled() -> bool:
    """razmyshleniya modeli: po umolchaniyu minimum (off)."""
    return os.environ.get("LLM_THINKING", "off").strip().lower() in (
        "on", "1", "true", "yes")


def llm_model() -> str:
    m = os.environ.get("LLM_MODEL", "").strip()
    allow = os.environ.get("LLM_MODEL_ALLOWLIST", "").strip()
    if allow and m not in [x.strip() for x in allow.split(",")]:
        raise LLMError(f"LLM_MODEL {m} nie v ALLOWLIST")
    return m or "llama3-saiga:32b"


def _request_timeout_s() -> float:
    try:
        return max(30.0, float(os.environ.get("LLM_REQUEST_TIMEOUT_S", "900") or 900))
    except ValueError:
        return 900.0


def _job_budget_s() -> float:
    """Total'nyy byudzhet odnogo protokola (map-reduce okna + reduce + retri).
    Derzhim nizhe RQ default_timeout (4 ch), chtoby job ne ubivalis' poseredine."""
    try:
        return max(120.0, float(os.environ.get("LLM_JOB_BUDGET_S", "10800") or 10800))
    except ValueError:
        return 10800.0


def _max_tokens() -> int:
    """Potolok generacii: razvyornyy protokol (svodki po temam so vsemi tsiframi,
    blok changes) ne pomeshaetsya v 6-8k tokenov iobrezaltsya posereine JSON;
    bez polya model' vykladyvaet shtop-potolok (na medlennoy modeli >10 min —
    HTTP-timeout rvyot zapros, retay nagromazhdaet zadayu na GPU po krugu)."""
    try:
        return max(0, int(os.environ.get("LLM_MAX_TOKENS", "16000") or 0))
    except ValueError:
        return 16000


_OLLAMA_NATIVE: bool | None = None  # None = eshche ne.proverili


def _ollama_base() -> str | None:
    """Koren' Ollama (bez /v1): esli /api/tags otvechayet — eto Ollama.

    Rezul'tat keshiruetsya na process. Sootvetstvuyushchiy /v1 chat completions
    na myslyashchikh modelyakh ne otdayot flag think kak nalozhienny na shablon:
    razmyshleniya kushayut max_tokens i protocolla prihodit pustym/obrezzannym
    (izmereno: /v1 think=false -> 8000 tokov razmyshleniy, pusty content;
    rodnoy /api/chat think=false -> chisty JSON bez mysley, ~6x bystree)."""
    global _OLLAMA_NATIVE
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    if not base:
        return None
    origin = base[:-3].rstrip("/") if base.endswith("/v1") else base
    if _OLLAMA_NATIVE is False:
        return None
    if _OLLAMA_NATIVE is True:
        return origin
    try:
        r = httpx.get(f"{origin}/api/tags", timeout=3.0)
        _OLLAMA_NATIVE = r.status_code == 200
    except httpx.HTTPError:
        _OLLAMA_NATIVE = False
    return origin if _OLLAMA_NATIVE else None


def _post_chat(payload: dict):
    """Odin POST /chat/completions v exklyuzivnom LLM-zamke (odin analiz za
    raz; ozhidanie — FIFO-ochered'yu). Bez redis zamyok — no-op.

    Dlya Ollama payload perekladyvaetsya v rodnoy /api/chat (sm. _ollama_base)
    i otvet unwrappaetsya v form OpenAI, chto by vyzvashchiye kody ne menyalis'.
    """
    from .llm_gate import LLMGateTimeout, llm_turn
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    native = _ollama_base()
    try:
        with llm_turn():
            if native:
                p = dict(payload)
                options: dict = {}
                for src, dst in (("temperature", "temperature"),
                                 ("max_tokens", "num_predict")):
                    if src in p:
                        options[dst] = p.pop(src)
                p.pop("num_predict", None)
                p.pop("thinking", None)  # rodnomu /api/chat enough `think` bool
                p["options"] = options
                p.setdefault("stream", False)
                # myslitepol'zovatelskiy format: think=False uzhe v payload
                return _native_to_openai(httpx.post(
                    f"{native}/api/chat", headers=headers, json=p,
                    timeout=_request_timeout_s()))
            return httpx.post(f"{base}/chat/completions", headers=headers,
                              json=payload, timeout=_request_timeout_s())
    except LLMGateTimeout as e:
        # zhdali ocheredi slishkom dolgo: nerepetitno (LLMGateWaitError),
        # chtoby retries ne umnozhali chasovoe ozhidanie na kazhday popytke
        raise LLMGateWaitError(str(e)) from e


class _NativeResp:
    """Oborotka otveta /api/chat v interface otveta OpenAI (status_code/json/text)."""

    def __init__(self, r: httpx.Response):
        self.status_code = r.status_code
        self.text = r.text
        self._r = r

    def json(self) -> dict:
        d = self._r.json()
        msg = d.get("message") or {}
        return {"choices": [{"message": {"content": msg.get("content", "")}}]}


def _native_to_openai(r: httpx.Response) -> _NativeResp:
    return _NativeResp(r)


def _chat(messages: list[dict], retries: int = 2) -> str:
    global _THINK_REJECTED
    thinking_on = _thinking_enabled()
    payload: dict = {"model": llm_model(), "messages": messages,
                     "temperature": 0.1}
    mt = _max_tokens()
    if mt:
        payload["max_tokens"] = mt
        payload["num_predict"] = mt  # ollama-native alias; neznakomye poly ignoryatsya
    if not _THINK_REJECTED:
        # dva formata odnovremenno: ollama (`think`) i OpenAI-style
        # (`thinking.type`); neznakomye poly servery ignoriiruyut, a strogiy
        # 400 s «think» otlavlivaeetsya nizhe i flag bolshe ne peredaetsya
        payload["think"] = thinking_on
        payload["thinking"] = {"type": "enabled" if thinking_on else "disabled"}
    last_err = None
    for attempt in range(retries + 1):
        if _budget_exceeded():
            raise LLMError(f"byudzhet LLM ischerpan (job deadline)")
        try:
            r = _post_chat(payload)
            if r.status_code >= 400:
                if ("think" in payload and r.status_code == 400
                        and "think" in r.text):
                    # server (ne ollama) ne priemaet flag mysley — prodolzhaem
                    # bez nego, chtoby ne lomayut'sya na kazhdom zaprase
                    _THINK_REJECTED = True
                    payload.pop("think")
                    payload.pop("thinking")
                    last_err = LLMError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
                    continue
                raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
            return r.json()["choices"][0]["message"]["content"]
        except LLMGateWaitError:
            raise  # ozhidanie zamka — ne retribaem (umnozhayet chasovoe ozhidanie)
        except (LLMError, httpx.HTTPError) as e:
            last_err = e
            if attempt < retries:  # ne spyat' posle posledney popytki
                time.sleep(min(2 ** attempt, 8))
    raise LLMError(str(last_err))


def _extract_json(text: str) -> dict:
    """Vyznut' pervy JSON-obiekt iz otveta (LLM mozhem obrambivat' markdown)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise LLMError("otvet nie soderzhit JSON")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"nevalidny JSON: {e}") from e


def validate_protocol(data: dict) -> dict:
    if not isinstance(data, dict):
        raise LLMError("JSON nie objiekt")
    missing = PROTOCOL_JSON_KEYS - set(data)
    if missing:
        raise LLMError(f"v JSON niet klyuchey: {missing}")
    if not isinstance(data.get("topic_summaries"), list):
        data["topic_summaries"] = []
    ch = data.get("changes")
    if not isinstance(ch, dict):
        ch = {}
    norm_ch: dict = {"relative_to": str(ch.get("relative_to") or "")}
    for k in CHANGES_KEYS[1:]:
        vals = ch.get(k)
        norm_ch[k] = [str(x) for x in vals if str(x).strip()] if isinstance(vals, list) else []
    data["changes"] = norm_ch
    return data


def _prev_block(prev_digest: str) -> str:
    prev_digest = (prev_digest or "").strip()
    if not prev_digest:
        return ""
    return (f"PROTOKOL PREDYDUSHCHEGO SOVESHCHEANIYA (dlya sravneniya v pole "
            f"changes, ego soderzhanie v svodki ne perenosy):\n{prev_digest}\n\n")


def structure_transcript(transcript: str, topic: str = "", roster: str = "",
                         meeting_date: str = "",
                         prev_digest: str = "") -> dict:
    """Polnaya stenogramma -> protocol JSON. Kosol'sl'zye okna esli > konteksta.

    roster — spravka 'FIО — dolzhnost' uchastnikov (dlya disambiguacii, v
    protokole ne otrazhaetsya). meeting_date — data sovieshchaniya DD.MM.YYYY
    (ot noy LLM ischislyayet otnositel'nyye sroki porucheniy). prev_digest —
    kratkoe soderzhanie protokola predydushchey vstrechi (dlya bloka changes)."""
    roster = (roster or "").strip()
    with llm_budget(_job_budget_s()):
        return _structure_transcript_inner(transcript, topic, roster, meeting_date,
                                           prev_digest)


def _structure_transcript_inner(transcript, topic, roster, meeting_date,
                                prev_digest) -> dict:
    date_block = _meeting_date_block(meeting_date)
    max_chars = int(os.environ.get("LLM_WINDOW_CHARS", "24000"))
    if len(transcript) <= max_chars:
        user = (f"{date_block}TEMA: {topic}\n\nSTENOGRAMMA:\n{transcript}")
        if roster:
            user += (f"\n\nSPRAVOCHNIK UCHASTNIKOV (FIО — dolzhnost, dlya poni"
                     f"maniya, kto chto skazal; v protokole dolzhnosti ne "
                     f"ukazyvai):\n{roster}")
        user += f"\n\n{_prev_block(prev_digest)}" if prev_digest else ""
        reply = _chat([{"role": "system", "content": SYSTEM_PROMPT},
                       {"role": "user", "content": user}])
        data = validate_protocol(_extract_json(reply))
        if meeting_date.strip() and not (data.get("date") or "").strip():
            data["date"] = meeting_date.strip()
        return data
    return _map_reduce(transcript, topic, max_chars, roster, meeting_date,
                       prev_digest)


def _bounded_json(data: dict, max_chars: int) -> str:
    """JSON ne dolszhen uryvats' poseredine elementa: syroy `[:max_chars]`
    rezh' item i LLM poluchal polomanny vkhod. Udalyaem tseliye elementy
    samykh dlinykh spiskov, poka serializaciya ne uleditsya."""
    s = json.dumps(data, ensure_ascii=False)
    if len(s) <= max_chars:
        return s
    import copy
    data = copy.deepcopy(data)
    droppable = sorted(("topic_summaries", "action_items", "agenda",
                        "decisions", "open_questions"),
                       key=lambda k: -len(data.get(k) or []))
    for key in droppable:
        items = data.get(key) or []
        while items and len(json.dumps(data, ensure_ascii=False)) > max_chars:
            items.pop()
        if len(json.dumps(data, ensure_ascii=False)) <= max_chars:
            return json.dumps(data, ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False)  # minimal'nyy — ostal'noye ne rezhem


def _map_reduce(transcript: str, topic: str, max_chars: int, roster: str = "",
                meeting_date: str = "", prev_digest: str = "") -> dict:
    date_block = _meeting_date_block(meeting_date)
    windows = []
    roster_block = (f"\n\nSPRAVOCHNIK UCHASTNIKOV (FIО — dolzhnost):\n{roster}"
                    if roster else "")
    step = int(max_chars * 0.8)
    i = 0
    while i < len(transcript):
        windows.append(transcript[i:i + max_chars])
        i += step
    partials: list[dict] = []
    failed_windows: list[int] = []  # M2: tiho propushchennoe okno = teryannaya chast' protokola
    for idx, w in enumerate(windows):
        if _budget_exceeded():
            failed_windows.extend(range(idx, len(windows)))
            break
        user = (f"{date_block}OKNO {idx + 1} iz {len(windows)}. Vytashi iz eta fragmanta "
                f"po skHEME (chastichno): podrobnye svodki po temam (topic_summaries, "
                f"se so vsemi tsiframi i markeramy), resheniya, zadachi, otkrytye voprosy, "
                f"upomyanutykh lyudey.\n\n{w}" + roster_block)
        try:
            reply = _chat([{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": user}])
            partials.append(validate_protocol(_extract_json(reply)))
        except LLMGateWaitError:
            raise  # zamyok: ne tonkaya oshibka okna, a sistemnaya — podnyimaem
        except LLMError:
            failed_windows.append(idx)
            continue
    if not partials:
        raise LLMError("map-reduce: ni odno okno nie dalo valid JSON")
    merged = {"topic": topic, "date": "", "duration": 0, "attendees": [],
              "agenda": [], "topic_summaries": [], "decisions": [],
              "action_items": [], "open_questions": [],
              "changes": {"relative_to": "", "continuing": [], "progress": [],
                          "new": [], "unconfirmed": []}}
    for p in partials:
        merged["date"] = merged["date"] or p.get("date", "")
        merged["duration"] = merged["duration"] or p.get("duration", 0)
        for key in ("agenda", "decisions", "open_questions"):
            for item in p.get(key) or []:
                if item not in merged[key]:
                    merged[key].append(item)
        for ts_item in p.get("topic_summaries") or []:
            if isinstance(ts_item, dict) and ts_item not in merged["topic_summaries"]:
                merged["topic_summaries"].append(ts_item)
        for a in p.get("attendees") or []:
            if a.get("name") and a["name"] not in [x.get("name") for x in merged["attendees"]]:
                merged["attendees"].append(a)
        for ai in p.get("action_items") or []:
            merged["action_items"].append(ai)
    reduce_input = _bounded_json(merged, max_chars)
    if failed_windows:
        log.warning("map-reduce: %d/%d okon davali oshibku (soderzhanie vyshlo "
                    "iza protokola): %s", len(failed_windows), len(windows),
                    failed_windows[:20])
    reply = _chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (date_block
                                     + _prev_block(prev_digest)
                                     + "Soberi EDINY protokola po skHEME iz etih "
                                       "chastichnyh rezultatov. Dedup ciranykh. Bez vydumok:\n\n"
                                     + reduce_input)}])
    data = validate_protocol(_extract_json(reply))
    if not data.get("topic_summaries"):
        # reduce zatrubil svodki — brat' to, chto sobralos iz okon
        data["topic_summaries"] = merged["topic_summaries"]
    if meeting_date.strip() and not (data.get("date") or "").strip():
        data["date"] = meeting_date.strip()
    return data


SPLIT_SYSTEM_PROMPT = (
    "Ty analysiruesh stenogramu ONE audiozapisi s neskol'kimi vstrechami podrjad. "
    "V stenograme metki [ТИШИНА HH:MM:SS — HH:MM:SS (N c)] — dl'nye pauzy mezhdu "
    "replikami: Granada vstrechi chashche vse-go v konce ili sleza za takoy metkoy. "
    "Naydi GRANICY mezhdu raznymi sovieshchaniyami: cmera, sogovorniki, "
    "poviestka ili privetstvie osnovy. Vozvrashi ISTORIMO validny JSON bez "
    "poyasneniy: {\"boundaries\": [{\"start_ts\": \"CHCH:MM:SS\", "
    "\"confidence\": 0..1, \"topic_after\": \"tema vstrechi posle granicy\"}]}. "
    "Esli v zapisi ODNA vstrecha — Verni {\"boundaries\": []}. "
    "start_ts — vremya pervoy repliki NOVOY vstrechi iz stenogrammy. "
    "Ne vydumyivay granicy: esli uspehen nie uveren — ne vklyuchay yeye."
)


def parse_ts(value: object) -> float:
    """CHCH:MM:SS (MM/SS dopuskeny dvuznachnymi, s — desyatichnaya) -> sekundy.

    LLM often writes 'M:SS' or 'HH:MM:SS,m'. Raises LLMError on unparsable."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < 0:
            raise LLMError(f"otricatel'noye vremya: {value}")
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise LLMError(f"vremya nie stroka: {value!r}")
    text = value.strip().replace(",", ".")
    parts = [p for p in text.split(":")]
    if not 1 <= len(parts) <= 3 or any(not p.strip() for p in parts):
        raise LLMError(f"nie format vremeni: {value!r}")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise LLMError(f"nie format vremeni: {value!r}") from e
    if any(n < 0 for n in nums):
        raise LLMError(f"otricatel'noye vremya: {value!r}")
    for n in nums[1:]:
        if n >= 6000:
            raise LLMError(f"nie format vremeni: {value!r}")
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    if secs > 30 * 3600:
        raise LLMError(f"vremya vnye zdravogo diapazona: {value!r}")
    return secs


def _confidence(value: object) -> float:
    try:
        c = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return c if 0.0 <= c <= 1.0 else 0.0


def _compress_line(line: str, limit: int = 120) -> str:
    if len(line) <= limit:
        return line
    return line[:limit].rstrip() + "…"


def boundaries_windows(transcript: str, window_chars: int | None = None
                       ) -> list[tuple[int, int]]:
    """Okna [start_offset, end_offset) s perekrytiyem 10% dlya skol'zyashchego poiska."""
    n = len(transcript)
    if window_chars is None:
        window_chars = int(os.environ.get("SPLIT_WINDOW_CHARS", "40000") or 40000)
    if n <= window_chars:
        return [(0, n)]
    overlap = max(1, int(window_chars * 0.1))
    step = max(1, window_chars - overlap)
    out: list[tuple[int, int]] = []
    i = 0
    while i < n:
        out.append((i, min(i + window_chars, n)))
        if i + window_chars >= n:
            break
        i += step
    return out


def detect_meeting_boundaries(transcript: str, topic: str = "",
                              duration_s: float = 0.0) -> list[dict]:
    """Odna zapis' -> spisanok granicy mezhdu vstrechami (LLM po stenogramme).

    Vozvrashaet [] esli granicy nie naydeny. Kazhdaya granica:
    {t: sek, confidence: 0..1, topic_after: str}. Pri stenograme she
    SPLIT_WINDOW_CHARS — skol'zyashchiye okna s perekrytiyem, kandidaty
    meryatsya po blizosti (<60 c — berotsya s bol'shey confidence)."""
    max_chars = int(os.environ.get("SPLIT_WINDOW_CHARS", "40000") or 40000)
    lines = [_compress_line(ln) for ln in transcript.splitlines() if ln.strip()]
    body = "\n".join(lines) or (transcript or "")
    candidates: list[dict] = []
    windows = boundaries_windows(body, max_chars)
    ok_windows = 0
    last_err: LLMError | None = None
    for lo, hi in windows:
        chunk = body[lo:hi]
        user = (f"TEMA PREDPOLOZhitel'no: {topic or '(neizvestna)'}\n"
                f"DLITEL'NOST' ZAPISI: {duration_s:.0f} s\n\n"
                f"STENOGRAMMA (fragment):\n{chunk}")
        try:
            reply = _chat([{"role": "system", "content": SPLIT_SYSTEM_PROMPT},
                           {"role": "user", "content": user}])
            data = _extract_json(reply)
            ok_windows += 1
        except LLMError as e:
            last_err = e
            continue
        for b in data.get("boundaries") or []:
            if not isinstance(b, dict):
                continue
            try:
                t = parse_ts(b.get("start_ts"))
            except LLMError:
                continue
            candidates.append({"t": t,
                               "confidence": _confidence(b.get("confidence")),
                               "topic_after": str(b.get("topic_after") or "").strip()})
    if windows and ok_windows == 0 and last_err is not None:
        # vse okna padeni — ete nie "granicy ne naydeny", a sbay detekcii
        raise last_err
    merged = merge_boundary_candidates(candidates)
    if duration_s > 0:
        merged = [b for b in merged
                  if 0.0 < b["t"] < duration_s and duration_s - b["t"] > 30.0]
    return merged


def merge_boundary_candidates(cands: list[dict], min_gap_s: float = 60.0
                              ) -> list[dict]:
    """Kandidaty iz perekryvayushchikhsya okon: blizkie (<min_gap_s) sliyayutsya,
    peryodayetsya bolshej confidence. Rezultat — monotonno vozrastayushchiy."""
    out: list[dict] = []
    for c in sorted(cands, key=lambda c: c["t"]):
        if out and c["t"] - out[-1]["t"] < min_gap_s:
            if c["confidence"] > out[-1]["confidence"]:
                out[-1] = c
            continue
        out.append(dict(c))
    return out

