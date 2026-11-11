<!-- Копия локального AGENTS.md (untracked): правки вносить здесь, в AGENTS.md попадать не должны. -->

# Project Notes: Mnema

## Особенности тестов (pytest)

- Python: использовать venv проекта: `./venv/bin/python -m pytest` (см. правило python_venv.md).
- **Полный прогон `tests/` медленный — ~13 минут (626 passed, 7 skipped).**
  По умолчанию Bash-команды ограничены 120 с, поэтому pytest без явного `timeout`
  будет убит посреди прогона. Всегда передавайте большой `timeout`
  (≥300000 мс) или ограничивайтесь конкретными файлами тестов.
- Во время разработки гоняйте только затронутые файлы, напр.:
  `timeout 300 ./venv/bin/python -m pytest tests/test_api_integration.py -q`
   (отдельные файлы быстрые: test_api_integration.py ~35 с).
- На `tests/` целиком вывод лучше писать в файл:
  `./venv/bin/python -m pytest tests/ -q > /tmp/pytest_out.txt 2>&1; tail -f не нужен — итог в конце файла`.
  Вывод с `| tail` при таймауте теряется полностью.
- 7 тестов скипаются штатно (нет внешних зависимостей) — это не ошибка.
- Redis в тестах недоступен намеренно (`REDIS_URL` указывает на заведомо пустой
  порт `redis://127.0.0.1:6399/0`): enqueue/stop падают в no-op. Проверку
  постановки в очередь делайте через `monkeypatch.setattr("mnema.core.jobs_launch.request_stop", ...)`
  или аналог, а не через реальный redis.
- Тесты БД используют in-memory SQLite (`set_engine_for_test` + StaticPool);
  env-переменные (`DATA_DIR`, `ARCHIVE_DIR`, `DATABASE_URL`, ...) задаются через
  `os.environ.setdefault` в начале тестовых файлов — новые env добавляйте туда же.
- Переходы статусов в тестах эмулируют web-bot через внутренние
  `POST /api/jobs/{id}/status` (см. `ALLOWED_TRANSITIONS` в `mnema/core/pipeline.py`);
  недопустимый переход → 409, это проверяемое поведение.
- web-bot (Node.js, `web-bot/`) — тесты на встроенном раннере: `node --test web-bot/test/`
  (149 тестов, ~15 с, без npm-зависимостей; fast-check перед коммитом изменений в `web-bot/src/`).
  Минимальная синтаксическая проверка изменённого файла — `node --check src/<file>.js`.

## Деплой на GPU-сервер (удалённый ИИ-сервер)

- Сервер: задать в `scripts/deploy-gpu.local.env` (untracked; `DEST_SSH=user@host`,
  `DEST_DIR=...`) или через переменные окружения; в git — только плейсхолдер
  `user@mnema-gpu.internal`. Каталог на сервере: `/data_local/docker/mnema`.
- Деплой из репо: `./scripts/deploy-gpu.sh` — локально создаётся git-bundle текущей
  ветки, копируется по scp на сервер, там `git fetch <bundle> + git reset --hard FETCH_HEAD`
  (push в origin не используется, untracked-файлы сервера не затрагиваются), затем
  `docker compose -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.override.yml build` +
  `run --rm --no-deps api python -m alembic upgrade head` (миграции перед up) +
  `up -d --force-recreate --no-deps api worker web-bot` (`--no-deps` обязательно —
  иначе up пересоздаст изменённые зависимости, напр. redis, и сбросит очередь).
  Требует чистое дерево (иначе выходит с ошибкой); коммитить нужно до деплоя.
  Перед деплоем проверяет активные записи через `GET :8000/api/jobs`
  (`scripts/check-active-jobs.sh`: launching/recording/uploading → выход с ошибкой,
  чтобы `--force-recreate web-bot` не убил живой браузер посередине встречи);
  проверка повторяется на сервере непосредственно перед `force-recreate` (build/alembic
  длятся минуты — встречa может начаться в этом окне); неизвестное состояние
  (API недоступен/ответ не разобрался) блокирует так же; политика — `scripts/deploy-guard-lib.sh`
  (тестируется в tests/test_deploy_active_check.py); обход — `FORCE_DEPLOY=1`.
- **ВАЖНО: все команды compose на сервере — только с тремя `-f`.** Без
  `docker-compose.gpu.yml` worker поднимется из лёгкого CPU-образа и
  `ASR_DEVICE=cuda:0` потеряется (диаризация/ASR молча пойдут на CPU).
- `docker-compose.override.yml` на сервере untracked (ручной файл): GPU device_ids
  (`device_ids: ["9"]`), порты web-bot `8100-8120:6080-6100`, `!override`-теги.
  `git reset --hard` его не трогает, но при пересоздании с нуля настройки потеряются —
  всё, что должно жить постоянно, добавляйте в `docker-compose.gpu.yml` в git.
- Ключевые env worker'а (GPU): `ASR_DEVICE=cuda:0`, `WHISPER_COMPUTE_TYPE=float16`,
  `DIAR_FP16=0` (в fp16 pyannote склеивает разных говорящих в один кластер —
  качество диаризации важнее скорости; локально на CPU переменная не нужна).
- Веса pyannote: кэш HF — volume `mnema_hf-cache`; кэш `/root/.cache/torch/pyannote`
  ephemeral. Признак устаревших весов в логах: `Model was trained with
  pyannote.audio 0.0.1, yours is 3.4.0` — лечение: `rm -rf /root/.cache/torch/pyannote`
  в контейнере + `docker restart mnema-worker-1` (HF_TOKEN в контейнере есть,
  скачается заново).
- Проверка после деплоя: `docker compose ps`, `curl -s localhost:8000/health`,
  `docker exec mnema-worker-1 printenv ASR_DEVICE DIAR_FP16` (ожидание: `cuda:0`, `0`).

## Выгрузка в самба-шару (SMB-зеркало, `mnema/core/export.py`)

- Транспорт — CIFS-маунт на хосте (`//server/share` → `/mnt/mnema-export`,
  настройка: `scripts/smb-mount.sh`), bind-mount в `worker`/`api` как `/export`.
  В коде — обычная запись файлов, SMB-библиотек нет.
- Хостовый маунт **обязательно** с `iocharset=utf8` (есть в fstab-строке
  скрипта), иначе имена кириллицей бьются.
- `.env` на сервере (untracked): `EXPORT_HOST_DIR=/mnt/mnema-export`,
  `EXPORT_DIR=/export`. Без них выгрузка — no-op (`export_configured()=False`).
- Проверка после деплоя: `docker exec mnema-worker-1 printenv EXPORT_DIR`
  (ожидание `/export`) и запись тестового файла в `/export/` внутри worker.
- Отказ сети не роняет job: ошибка → SessionEvent `export_error`, ретраи в
  `maintenance_job` (каждые 5 мин, ≤5 за проход, после 6 неудач — уведомление
  `job_export_failed`).
