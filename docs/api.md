# Mnema — описание API

REST API сервиса `api` (FastAPI). Реализация: `mnema/api/main.py`.

Запуск: `python -m mnema.api.main` (uvicorn, `0.0.0.0:8000`).

Интерактивная документация (из коробки FastAPI):
- Swagger UI — `GET /docs`
- ReDoc — `GET /redoc`

Общие соглашения:
- Все JSON-эндпоинты — префикс `/api`.
- Даты в JSON — ISO 8601 (UTC); в WEB-формах `datetime-local` интерпретируется как локальное время оператора (МСК) и конвертируется в UTC.
- Раздача файлов (аудио/видео/образцы) поддерживает HTTP `Range` (206 Partial
  Content) по RFC 7233: одиночный диапазон, суффиксный `bytes=-N` (последние
  N байт); unsatisfiable диапазон (`start >= size`) → `416` с
  `Content-Range: bytes */<size>`; невалидный или мультидиапазонный заголовок
  игнорируется — файл отдаётся целиком (`200`).
- Ошибки: `404` — объект не найден, `409` — недопустимое состояние/переход,
  `400`/`422` — некорректные данные, `413` — превышен лимит размера.

## Статусы встречи (Job)

Допустимые переходы (`ALLOWED_TRANSITIONS`, `mnema/core/pipeline.py`):

| Текущий статус | Допустимые следующие |
|---|---|
| `scheduled` | `launching`, `uploading`, `cancelled`, `failed` |
| `launching` | `recording`, `scheduled`, `cancelled`, `failed` |
| `recording` | `processing`, `cancelled`, `failed` |
| `uploading` | `processing`, `cancelled`, `failed` |
| `processing` | `awaiting_llm`, `awaiting_review`, `done`, `failed` |
| `awaiting_llm` | `processing`, `done`, `failed` |
| `awaiting_review` | `done`, `processing`, `failed` |
| `done` | `processing` (rerun LLM) |
| `failed` | `processing`, `uploading` |
| `cancelled` | — (терминальный) |

Недопустимый переход возвращает `409`.

---

## Jobs (встречи)

### `GET /api/jobs`

Список встреч (до 200, по `start_at DESC`).

| Параметр (query) | Тип | Описание |
|---|---|---|
| `status` | string, opt | фильтр по статусу |

Ответ: `[{id, zoom_meeting_id, topic, start_at, status, fail_reason, started_ts, ended_ts}]`

### `POST /api/jobs`

Создать встречу.

Тело (JSON):

```json
{
  "meeting_link": "https://us06web.zoom.us/j/123456789?pwd=...",
  "meeting_id": "123456789",
  "pwd": "secret",
  "topic": "Планёрка",
  "start_at": "2026-10-06T10:00:00Z",
  "duration_max_min": 60
}
```

Все поля опциональны (минимум — `meeting_link` или `meeting_id`).
`start_at = null` — немедленный запуск (внешние встречи).

Ответ: `{"id": 1, "status": "scheduled"}`

### `GET /api/jobs/{jid}`

Карточка встречи: поля job + `events[]` (`{ts, event, detail}`) + `audio[]`
(`{kind, duration_s, bytes}`).

При разделении записи на несколько встреч дополнительно:
`parent_job_id` (`null` у исходной записи), `child_job_ids` (список id частей),
`split` — `split_json` встречи:

```json
{
  "boundaries": [{"t": 2730.0, "confidence": 0.86,
                  "topic_before": "Планёрка", "topic_after": "Разбор инцидента"}],
  "model": "llama3-saiga:32b", "skipped": null, "ts": "2026-10-08T10:00:00Z"
}
```

При пропуске этапа `"boundaries"` нет, `"skipped"` — причина
(`disabled`, `too_short`, `too_few_segments`, `llm_not_configured`,
`no_boundaries_found`, …). У потомка `split` = `{"parent": <id>, "boundary_t": <сек>}`.

При сбое применения границ (потомки уже созданы, но цикл разделения
прерван) в `split_json` родителя пишется `"partial": true`, `"children": [id]`
и `"error"` — rerun не создаст дубликатов хвостов; этап `split` показывается
как `failed`.

События этапа: `split_start`, `split_done` (границы и id детей в detail),
`split_skipped` (причина), `split_audio_error` (ошибка ffmpeg — разделение
в БД при этом остаётся в силе), `split_error` (сбой применения границ,
частичное разделение).

Событие `llm_error` — LLM не вернул протокол; причина в `detail`, после него
job переходит в `awaiting_llm` (этап LLM показывается как `failed`).

### `DELETE /api/jobs/{jid}`

Отмена встречи → `{"id", "status"}`.

### `GET /api/jobs/{jid}/join-info`

Служебный эндпоинт для web-bot (только сетевой периметр): возвращает
`{meeting_id, pwd, join_link, end_at}`; пароль берётся из secrets store.

### `POST /api/jobs/{jid}/events`

Событие от web-bot в event-log.

Тело: `{"event": "string", "detail": "string|null"}` → `{"ok": true}`

### `POST /api/jobs/{jid}/status`

Смена статуса (web-bot). Тело:

```json
{
  "status": "recording",
  "fail_reason": "null",
  "error": "null",
  "attempts": 1,
  "screenshot_path": "null"
}
```

Переход валидируется по таблице выше; нарушение → `409`. Идемпотентен по
статусу: пост уже текущего статуса (retry web-bot после сетевого таймаута)
возвращает `{"ok": true, "status": "...", "already": true}` без перехода.
Гонка параллельного писателя (status изменился между проверкой и коммитом —
условный `UPDATE ... WHERE status = :cur`) → `409`, тихого перезаписывания
нет.
Ответ: `{"ok": true, "status": "..."}`

### `POST /api/jobs/{jid}/finished`

Сообщение web-bot о завершении записи. Тело:

```json
{
  "webm_path": "/data/.../rec.webm",
  "wav_path": "/data/.../audio.wav",
  "mp3_path": "/data/.../audio.mp3",
  "duration_s": 1234.5,
  "sizes": {"wav": 123, "mp3": 456}
}
```

Регистрирует `audio_files` (dedup по kind), переводит job в `processing` и
ставит в очередь ASR-оркестрацию. Идемпотентен: повторный пост (retry по сети,
salvage ubitogo web-bot) не даёт 409 — файлы перезаписываются по метаданным,
ответ `{"ok": true, "already": true}` без повторного transition/enqueue.
Параллельная смена статуса другим писателем (cancel/retry) тоже не даёт 500 —
`transition` бросает `TransitionRace` и ответ тот же `already: true`.
Из статуса `failed` (salvage упавшей задачи — данные на диске) возвращает
задачу в `processing` с событием `recording_recovered`.
Ответ: `{"ok": true}`

### `POST /api/jobs/{jid}/leave`

Ручное завершение встречи оператором (web-bot выходит из созвона).
Допустимо только для статусов `launching` / `recording` (иначе `409`).

Ответ: `{"ok": true, "status": "...", "stop_queued": true}`

### `POST /api/jobs/{jid}/confirm-end`

Безопасный ручной выход: web-bot обращается к API, когда плановое время окончания
встречи (`end_at`) прошло, а уровень звука измерить не удалось (детектор слеп)
— вместо автовыхода система спрашивает оператора. Допустимо только для статуса
`recording` (иначе `409`). Повторный запрос при уже висящем `pending`
дедуплицируется (`already_pending: true`).

Тело (оба поля необязательные): `{"reason": "...", "screenshot_path": "..."}` —
путь принимается только внутри `DATA_DIR/failure_screenshots`.

Ставит `confirm_end_requests` (`pending`, одноразовый токен), событие
`confirm_end_requested` и отправляет письмо с двумя ссылками
(`Завершить запись` → `GET /confirm-end/<token>`, `Посмотреть экран` → noVNC)
и inline-скриншотом. Запись продолжается, пока оператор не ответит.

Ответ: `{"ok": true, "request_id": 5}` или
`{"ok": true, "request_id": 5, "already_pending": true}`

### `GET /confirm-end/{token}`

Кнопка «Завершить» из письма: отмечает запрос `confirmed`, пишет событие
`confirm_end_confirmed` и ставит web-botу stop (`manual_leave`), запись уходит
в обработку. HTML-ответ. Одноразово: повторный вызов той же ссылки возвращает
«уже обработано» и повторный stop не делает. Неизвестный токен → `404`.
Частные случаи (страница с предупреждением ⚠): redis недоступен — запрос
подтверждён, но команда остановки не поставилась (нужно завершить кнопкой в
интерфейсе); запись уже не `recording` — завершать нечего.

---

## VNC (экраны браузера, noVNC)

### `GET /api/vnc/sessions`

Состояние всех VNC-сессий (setup + `job-<id>`) — для UI и диагностики.

### `POST /api/vnc/setup/open`

Открыть/поднять сессию настройки (живой браузер с Zoom для оператора:
печеньки, отпечатки базы).

Ответ: `{"ok", "queued", "session", "url"}` — `url` (noVNC) только для живой
сессии (`ready`/`running`), иначе `null`.

### `POST /api/vnc/setup/restart`

Перезапуск setup-сессии. Формат ответа как у `setup/open`.

### `POST /api/jobs/{jid}/vnc`

Экран конкретной встречи. Если сессия живая — сразу `200` с `url`;
иначе ставит команду создания и возвращает `202` с `url: null`.
Для архивных записей (`source=upload`) — `409`.

### `DELETE /api/jobs/{jid}/vnc`

Закрыть VNC-сессию встречи → `{"ok": true, "queued": bool}`.

---

## Протоколы

### `GET /api/protocols/{pid}`

Получить протокол.

| Параметр (query) | Значения | Описание |
|---|---|---|
| `format` | `md` (default) | markdown-файл (`text/markdown`) |
| | `docx` | Word-документ |
| | `json` | структурированные данные протокола |

При `md`/`docx` перед отдачей имена говорящих освежаются (`refresh_protocol`).
Отсутствующий файл → `404`.

### `GET /api/protocols/{pid}/audio`

Аудио встречи протокола. `format=mp3|wav|webm` (default `mp3`), поддержка
`Range`. Неверный формат → `400`.

### `PATCH /api/protocols/{pid}/segments`

FT-32: ручное исправление имён говорящих в сегментах встречи.

Тело: `{"segments": [{"id": <segment_id>, "speaker_name": "Иванов И. И."}, ...]}`

Для каждого сегмента (проверяется принадлежность job'у протокола, чужие id
и нецелочисленные `id` (dict/list вместо числа) молча пропускаются)
проставляется `speaker_name`, `speaker_source=voiceprint`,
снимается `needs_review`. Если изменён хотя бы один сегмент — опубликованный
протокол освежается (`refresh_protocol`), чтобы имена в md/docx совпадали.

Ответ: `{"updated": N}` — `404`, если протокол не найден.

---

## Аудио / исходники встречи

### `GET /api/jobs/{jid}/audio`

Аудио встречи. `format=mp3|wav|webm` (default `webm`), поддержка `Range`.
mp3 доступен сразу после окончания записи.

### `GET /api/jobs/{jid}/source`

Раздача исходного архивного файла (для job с `source=upload`), поддержка
`Range`. MIME определяется по расширению (mp4/mkv/mov/webm/mp3/wav и др.).

---

## Review requests (уточнение говорящих)

### `GET /api/reviews`

Список запросов на уточнение.

| Параметр (query) | По умолчанию | Описание |
|---|---|---|
| `status` | `pending` | `pending` / `confirmed` / `excluded` / `superseded` / `timeout` / `mixed` |

Ответ: `[{id, job_id, cluster_label, candidate, candidate_id, similarity, status}]`

Значения: `pending` — ждёт ответа; `confirmed` — отвечено оператором;
`excluded` — кластер закрыт исключением всех фрагментов; `superseded` —
кластер опустел после открепления фрагмента (см. DELETE); `timeout` — закрыт
maintenance по истечении `REVIEW_TIMEOUT_HOURS`; `mixed` — все оставшиеся
фрагменты помечены миксом двух голосов (см. POST .../mix).

### `POST /api/reviews/{rid}`

Ответ оператора; применяется ко всему кластеру. Тело:

```json
{
  "employee_id": 12,
  "employee_name": "Иванов И. И.",
  "create_new": false,
  "answered_by": "web"
}
```

Фрагменты с `speaker_source=multi_voice` (в двух голосах в одной VAD-строке)
имя НЕ получают — реплики второго человека не должны появляться под первым
именем; такие фрагменты остаются на ручной правке сегментов.

После ответа кластер переобучается (enroll в workere) и, если все уточнения
закрыты, анализ возобновляется (переход `awaiting_review → processing`).
Постановка анализа в очередь дедуплицируется: если для встречи уже стоит
незапущенная попытка, вторая не добавляется.
Ответ: `{"ok": true, "name": "...", "job_done": bool}`

### `DELETE /api/reviews/{rid}/segments/{seg_id}`

Вырвать фрагмент из кластера: segment переносится в собственный подкластер
(`<label>-x<seg_id>`) и получает новый pending review-request. Если в
исходном кластере не осталось неоисключённых фрагментов — review закрывается
(`status=superseded`) и анализ возобновляется. Только для `pending` reviews
(иначе `409`).

Ответ: `{"ok", "cluster_label", "review_closed", "job_done"}`

### `POST /api/reviews/{rid}/segments/{seg_id}/exclude`

Исключить фрагмент из анализа полностью (два голоса / посторонние звуки) —
не попадает в стенограмму и протокол. Если кластер опустел — review
закрывается (`status=excluded`) и анализ возобновляется.

Ответ: `{"ok", "cluster_closed", "job_done"}`

### `POST /api/reviews/{rid}/segments/{seg_id}/mix`

Пометить фрагмент «Микс голосов»: два человека перебивают друг друга, и
распознать их по отдельности нельзя (фрагменты длиннее ~5 с с наложением
диаризация помечает как `multi_voice`). Фрагмент перестаёт уточняться как
отдельный голос (`speaker_source=voice_mix`, `needs_review` снят, hint-имя
`speaker_name` очищается — под одним именем нельзя подписывать двух людей), в
эмбеддинги кластера и в имена не попадает, но остаётся в стенограмме,
протоколе и входе LLM с пометкой «Два голоса в фрагменте». Если в кластере
не осталось фрагментов, ждущих решения, — review закрывается
(`status=mixed`) и анализ возобновляется. Для исключённого фрагмента и
отвеченного review — `409`.

Ответ: `{"ok", "cluster_closed", "job_done"}`

---

## Voice profiles (реестр голосов)

### `GET /api/voice-profiles`

Список профилей:
`[{id, employee_name, employee_id, position, samples, consent, match_stats}]`

### `POST /api/voice-profiles`

Создать профиль. Тело:

```json
{
  "employee_name": "Иванов И. И.",
  "employee_id": "123",
  "position": "Инженер",
  "consent": true
}
```

`consent=false` → `400` (GT-31: без согласия профиль не создаётся).
Ответ: `{"id": N}`

### `PATCH /api/voice-profiles/{vid}`

Правка ФИО/ID/должности. Переименование говорящего в сегментах
подтверждённых встреч + обновление выгруженных протоколов.
Пустое `employee_name` → `400`. Ответ: `{"ok": true, "employee_name": "..."}`

### `DELETE /api/voice-profiles/{vid}`

Удаление профиля вместе с эталонами и embeddings.

### `POST /api/voice-profiles/{vid}/sample`

Загрузка эталонной записи (multipart/form-data, поле `file`, webm с микрофона).
Проходит quality gate (ffmpeg); embedding считает worker (pyannote/GPU).
Файл принимается потоково с лимитом `SAMPLE_MAX_MB` (по умолчанию 64 МБ) —
превышение → `413`, временный файл удаляется.

Ответ (inline, если API собран с ASR): `{"ok": true}`
Ответ (удалённый режим): `{"ok": true, "pending_embedding": true, "worker_queued": bool}`
Брак по качеству: `{"ok": false, "reason": "..."}`

### `GET /api/voice-profiles/{vid}/samples`

Эталоны профиля: `[{index, name, exists, size, url}]`

### `GET /api/voice-profiles/{vid}/samples/{idx}`

Прослушать/скачать один эталон (Range;webm/wav/mp3/ogg/m4a).

### `DELETE /api/voice-profiles/{vid}/samples/{idx}`

Удалить эталон;embedding профиля пересчитывается в workere.
Ответ: `{"ok": true, "samples": N}`

### `GET /api/voice-profiles/{vid}/clusters`

Кластеры человека (confirmed + pending) с фрагментами для прослушивания:
`[{review_id, job_id, topic, cluster_label, status, answered_by, similarity, has_audio, audio_format, segments[]}]`

### `POST /api/voice-profiles/{vid}/reverify`

Ручной перезапуск распознавания для человека (пересборка эталона + повторное
узнавание всех встреч, выполняется в workere).
Ответ: `{"ok": true, "queued": bool}`

---

## Загрузка архивных записей (chunked uploads)

### `POST /api/uploads`

Инициализация загрузки. Тело:

```json
{
  "filename": "meeting.mp4",
  "bytes_total": 104857600,
  "job_id": null,
  "topic": "Встреча от 01.10",
  "meeting_date": "2026-10-01T12:00:00Z",
  "note": ""
}
```

Ответ: `{"job_id", "upload_id", "offset", "chunk_mb"}`
Ошибки: `413` — слишком большой файл, `404` — нет `job_id`, `400` — невалидные данные.

### `GET /api/uploads/{uid}/offset`

Текущий прогресс: `{"offset": N, "status": "pending|uploading|complete|failed"}`

### `POST /api/uploads/{uid}/chunk?offset=N`

Чанк файлом в raw body (`Content-Type: application/octet-stream`),
`offset` — опциональный query. Ответ: `{"offset", "status"}`.
Несовпадение offset → `409`; превышение лимита → `413`. На один запрос
действует отдельный потолок (≈ 2 × штатный чанк + 8 МБ): `Content-Length`
больше него или фактически прочитанные байты сверх него → `413` без
буферизации всего тела в RAM. Параллельные POST одного upload'а
сериализуются (flock) — interleav байт при гонке двух писателей исключён.

### `POST /api/uploads/{uid}/finalize`

Завершение загрузки: ffprobe-валидация аудио/видеодорожки (выполняется вне
сессии БД), перенос файла в `/data/jobs/<id>/raw.<ext>`, переход job'а в
`processing` и постановка в очередь ASR. Ответ:
`{"ok": true, "job_id", "status"}`. Ошибки: `400` — файл загружен не
полностью / отсутствует / не удалось извлечь валидную дорожку (загрузка
переходит в `failed` с `error`); уже finalized/отменён — тоже `400`.

### `POST /api/uploads/{uid}/cancel`

Отмена незавершённой загрузки: загрузка → `failed` («отменено оператором»),
временный файл удаляется; job в статусе `uploading` переходит в `cancelled`.
Ответ: `{"ok", "job_id", "status|null"}`.

### `GET /api/jobs/{jid}/uploads/active`

Активная (незавершённая) загрузка встречи для возобновления:
`{upload_id, filename, bytes_total, offset, status}` или `{"upload_id": null}`.

---

## Алерты

### `POST /api/alert`

Служебный сигнал от web-bot → e-mail: reCAPTCHA (§12) или зависший автовход
в Zoom (`join_stuck:<job_id>` — кнопка входа недоступна, один раз на встречу,
в письме порт noVNC). Отправка только если `alert` входит в `NOTIFY_EVENTS`
(входит по умолчанию, как и `confirm_end`).
Тело: `{"subject": "...", "body": "...", "dedup_key": "alert"}`
Ответ: `{"queued": <notification_id|null>}`

---

## Health

### `GET /health`

```json
{
  "status": "ok",
  "capabilities": {"llm": {...}, "asr": {...}},
  "zoom_session": {"ok": true, "detail": "...", "checked_ts": "..."} | null,
  "queue": true
}
```

Проверяет: доступные возможности (LLM/ASR), состояние Zoom-сессии
(последняя `SessionHealth`), доступность очереди (Redis ping).

---

## WEB UI (HTML-страницы и формы)

Не REST, но используются оператором напрямую; часть кнопок вызывает те же
обработчики, что и API.

| Метод и путь | Описание |
|---|---|
| `GET /static/*` | Статика (css/js) |
| `GET /` | Дашборд: активные встречи, ошибки, здоровье очередей/Zoom, VNC |
| `GET /jobs` | Список встреч (`?status=` фильтр) |
| `POST /jobs/create` | Форма создания встречи (preset_id / meeting_link / pwd / topic / start_at / end_at / start_now); ошибки — redirect `?error=link|date|preset|end_before_start` |
| `POST /jobs/{jid}/cancel` | Отмена встречи |
| `POST /jobs/{jid}/rename` | Переименовать (поле `topic`) |
| `POST /jobs/{jid}/update` | Редактирование ещё не начатой встречи (`meeting_link`/`pwd`/`topic`/`start_at`/`end_at`); пустой `end_at` в форме — очистить (`update_job` различает `None` = очистить и «не присылалось» = не трогать); `end_at <= start_at` → `422` |
| `POST /jobs/{jid}/delete` | Удаление (для `launching/recording` — `409`) |
| `POST /jobs/{jid}/leave` | Выход из созвона (как `POST /api/jobs/{jid}/leave`) |
| `POST /jobs/{jid}/vnc` | Экран встречи (как `POST /api/jobs/{jid}/vnc`) |
| `POST /jobs/{jid}/rerun-llm` | Перезапуск LLM-анализа (только `done`/`awaiting_llm`, иначе `409`); повторный rerun не копит дубликаты в очереди — ждущая попытка переиспользуется |
| `POST /jobs/import-ics` | Импорт встреч из ICS-файла (multipart `file`) → `{"imported": N}`; файл больше 64 МБ → `413` |
| `GET /jobs/upload` | Страница загрузки архива (`?job_id=`) |
| `GET /jobs/{jid}` | Карточка встречи (стадии, события, протокол, reviews) |
| `GET /jobs/{jid}/log` | Лог событий встречи |
| `GET /jobs/{jid}/stages` | Стадии обработки встречи |
| `POST /presets/add` | Добавить пресет ссылки (`title`, `meeting_link`, `pwd`) |
| `POST /presets/{pid}/delete` | Удалить пресет |
| `GET /protocols` | Список протоколов |
| `GET /protocols/{pid}` | Протокол со стенограммой |
| `GET /reviews` | Очередь уточнений (`?job_id=` фильтр) |
| `GET /voices` | Реестр голосовых профилей |

---

## Внутренние взаимодействующие компоненты

- **web-bot** (Node.js) — ходит в встречи Zoom и пишет через
  `POST /api/jobs/{jid}/status`, `/events`, `/finished`, читает
  `GET /api/jobs/{jid}/join-info`, шлёт `POST /api/alert`, при слепом
  детекторе уровня после `end_at` — `POST /api/jobs/{jid}/confirm-end`.
  Потребляет redis-очередь salvage (`mnema:salvage`): по сигналу или при
  старте дособирает сырые webm-сегменты с диска в финальные файлы и шлёт
  `POST .../finished` (восстановление после падения, идемпотентно).
- **worker** — берёт задачи из очереди (Redis); API с ним общается только
  через очередь (`enqueue_job`) и общий volume `DATA_DIR`.
- **api maintenance** — `stalled_recording_watch`: job в `recording` без
  событий дольше `STALLED_RECORDING_MIN` → `rpush mnema:salvage` (событие
  `salvage_requested`, без спама) + `review_timeout_check` (FT-28) +
  ретраи выгрузки в SMB.
