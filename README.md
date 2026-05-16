# 1C MCP Suite v3

Полная сборка MCP-инструментов для AI-агента OpenCode, работающего с конфигурациями 1С.

Это итоговый вариант, который объединяет:

- **Базу** (`23_1c-mcp-suite`) — 5 рабочих MCP-серверов: граф метаданных
  на Neo4j, BSL Language Server, справка платформы с семантическим
  поиском, шаблоны кода, 1С:Напарник.
- **v2** — 3 новых сервера (query-builder, testing, code-rag), метрики и кэш, 4 новых скилла. *(Изначально v2 включал и оркестратор субагентов, но он был удалён в апреле 2026 — см. `docs/historical/orchestrator_benchmark.py`.)*
- **v2.1** — пагинация и lazy-loading во всех тяжёлых инструментах
  metadata-graph, защитный лимит сканирования в `code_patterns`, новый
  REST-прокси к живой базе 1С с трёхслойной защитой read-only.
- **v3** — **гибридный поиск (Dense + Sparse/BM25)** для базы знаний ИТС
  через `qdrant-client` и `fastembed`. См. отдельный раздел ниже.

## Что нового в v3 относительно v2.1

### Гибридный поиск ИТС (Dense + BM25 + RRF)

В предыдущих версиях коллекция `its_articles` индексировалась только
плотными векторами `multilingual-e5-base`. Этого хватает для поиска по
смыслу («как разделить строку», «обработка ошибок»), но плотный поиск
систематически промахивается мимо точных технических терминов:
`#std466`, `УИДЗначения`, `ОбработкаПроведения`, `НайтиПоНаименованию` —
символы и точные имена для эмбеддинга «выглядят похоже» и тонут в шуме.

В v3 каждый чанк ИТС теперь индексируется **двумя векторами одновременно**:

| Вектор | Что ловит | Модель |
|---|---|---|
| `dense` | смысл, синонимы, переформулировки | `intfloat/multilingual-e5-base` |
| `sparse` | точные слова, технические термины, идентификаторы | `Qdrant/bm25` (fastembed) |

При поиске агент отправляет **один гибридный запрос** в Qdrant. Внутри
этот запрос разворачивается в две prefetch-ветки (одна по `dense`, одна
по `sparse`), и Qdrant сам объединяет два списка результатов через
**RRF (Reciprocal Rank Fusion)** — нативный механизм, появившийся в
Qdrant 1.10. Это даёт качественно лучшие результаты, чем любой из двух
поисков по отдельности, и не требует ручного смешивания скоров.

Что под капотом:

- `its_indexer.py` переписан с сырого `urllib` на официальный
  `qdrant-client`, использует named vectors (`dense`/`sparse`) и
  `SparseVectorParams(modifier=IDF)` для BM25.
- `mcp-platform-help/server.py` получил функции `_its_search_hybrid`
  (через `client.query_points` с `prefetch + Fusion.RRF`),
  `_its_search_legacy_dense` (graceful fallback), и диспетчер
  `_its_search`, который автоматически определяет формат коллекции и
  выбирает нужный режим.
- При запуске сервер автоматически детектирует формат коллекции и
  возвращает его в `platform_help_stats` как `its_collection_kind:
  hybrid | legacy_dense | missing`. По нему сразу видно, в каком режиме
  работает поиск.

### Граничные случаи и обратная совместимость

- **Старая (v2) коллекция `its_articles`.** Если у вас в Qdrant уже
  лежит коллекция, созданная старым индексатором (плоский dense без
  имени), новый `its_indexer.py` при запуске её обнаружит и
  автоматически удалит/пересоздаст в гибридном формате. Сервер
  `mcp-platform-help` тем временем продолжит работать в режиме
  `legacy_dense` через graceful fallback на сырой HTTP — пока вы не
  дойдёте до переиндексации.
- **Принудительная переиндексация.** Установите `ITS_FORCE_REINDEX=true`
  в `.env`, а затем:
  ```bash
  docker compose run --rm its-indexer
  ```
- **Sparse-модель fastembed** скачивается и кешируется при сборке
  образа `Dockerfile.embeddings` (через ARG `BM25_MODEL=Qdrant/bm25`),
  поэтому в рантайме интернет не нужен.
- **Память.** BM25-модель fastembed весит порядка 30 МБ, dense e5-base
  ~1.1 ГБ — потребление RAM `mcp-platform-help` в v3 практически не
  отличается от v2.

### Почему гибрид только для ИТС

Коллекция `platform_help` (синтакс-помощник, индексируется отдельным
скриптом `indexer.py` из `.hbk`) осталась только-dense. Там тексты
короткие и полностью «синтаксические» (имя функции + описание + типы),
плотного e5 хватает с большим запасом. Переделка `indexer.py` под
hybrid — отдельная работа без явного выигрыша.

Для ИТС же гибрид даёт максимум: длинные статьи с точными номерами
стандартов, именами объектов конфигурации и техническими терминами —
ровно тот случай, когда BM25 закрывает дыры плотного поиска.

## Состав v3

```
1c-mcp-suite-v3/
├── docker-compose.yml          ← один файл со всеми 10 сервисами
├── .env.example                ← переменные для всех слоёв (LLM, ONEC, ITS)
├── opencode-config.json        ← конфиг OpenCode
├── Dockerfile                  ← образ OpenCode
├── entrypoint.sh
│
├── 1c-config-xml/              ← сюда выгрузка конфигурации в XML
├── its-articles/               ← сюда PDF-статьи ИТС для индексации
├── platform-help-data/         ← .hbk файлы справки платформы
├── custom-templates/           ← пользовательские шаблоны
├── workspace/                  ← рабочая папка OpenCode (.opencode/skills)
│
└── 1c-mcp-suite/
    ├── start.py                ← регистрирует все 8 серверов
    ├── requirements.txt
    ├── requirements-embeddings.txt   ← + qdrant-client, fastembed
    ├── Dockerfile.python
    ├── Dockerfile.embeddings        ← теперь ставит BM25-модель
    ├── Dockerfile.bsl
    ├── mcp-config.json              ← 10 серверов
    │
    ├── mcp_pagination.py            ← v2.1: общий модуль пагинации
    ├── mcp_metrics.py               ← v2: метрики + dashboard 9000
    ├── mcp_cache.py                 ← v2: TTL-кэш (memory/Redis)
    │
    ├── mcp-metadata-graph/          ← v2.1: пагинация во всех инструментах
    ├── mcp-bsl-checker/
    ├── mcp-platform-help/           ← v3: hybrid ITS search
    ├── mcp-platform-help-embeddings/
    │   ├── indexer.py               ← .hbk → Qdrant (dense)
    │   └── its_indexer.py           ← v3: PDF/TXT → Qdrant (HYBRID)
    ├── mcp-code-templates/
    ├── mcp-1c-naparnik/
    ├── mcp-metadata-graph-neo4j/
    │   └── indexer.py               ← XML → Neo4j
    │
    ├── mcp-query-builder/           ← v2: конструктор запросов
    ├── mcp-testing/                 ← v2: генерация тестов
    ├── mcp-code-rag/                ← v2 + v2.1 paginated code_patterns
    └── mcp-rest-proxy/              ← v2.1: OData с защитой read-only
```

### Скиллы OpenCode (`workspace/.opencode/skills/`)

Базовые (из исходной сборки):

- `bsl-review`
- `code-from-template`
- `explore-metadata`
- `impact-analysis`
- `module-scaffold`
- `naparnik-review`
- `platform-help-lookup`

Из v2:

- `query-build` — работа с запросами 1С
- `test-generate` — генерация тестов

Из v2.1:

- `rest-query` — работа с живой базой через OData read-only

## Все 9 MCP-серверов v3

| # | Сервер | Порт | Назначение | Слой |
|---|---|---|---|---|
| 1 | `mcp-metadata-graph` | 8001 | Граф метаданных в Neo4j | база + пагинация v2.1 |
| 2 | `mcp-bsl-checker` | 8002 | Статический анализ BSL | база |
| 3 | `mcp-platform-help` | 8003 | Справка платформы + **гибридный ИТС** | база + v3 |
| 4 | `mcp-1c-naparnik` | 8007 | Прокси к code.1c.ai | база |
| 5 | `mcp-code-templates` | 8008 | 200+ шаблонов кода | база |
| 6 | `mcp-query-builder` | 8009 | Конструктор запросов с валидацией | v2 |
| 7 | `mcp-testing` | 8010 | Генерация тестов + прогон в реальной 1С через yaxunit-stack | v2 + v3.1 |
| 8 | `mcp-code-rag` | 8011 | RAG по BSL проекта + paginated patterns | v2 + v2.1 |
| 9 | `mcp-rest-proxy` | 8013 | OData/HTTP к живой базе (read-only) | v2.1 |

> **Историческая справка:** в исходной v2-сборке был ещё `mcp-orchestrator` (порт 8012, оркестратор 5 субагентов). Удалён в апреле 2026 после бенчмарка, показавшего overhead 24× по медиане против прямых вызовов MCP — подробности в `docs/historical/orchestrator_benchmark.py` и `PLAN.md`. opencode прекрасно справляется с многошаговыми задачами через нативный Tool Use API без оркестратора.

Дополнительно: в стеке также есть `mcp-sonarqube` (порт 8014, статический анализ через SonarQube) — он не входит в основную линейку v2/v3, но активен в `docker-compose.yml`.

Дашборд метрик: порт 9000 (запуск отдельно: `python mcp_metrics.py`).

## Интеграция с yaxunit-stack (прогон тестов в реальной 1С)

`mcp-testing` умеет не только генерировать тесты, но и запускать их в
настоящем кластере 1С через опциональный `yaxunit-stack`. Связь между
стеками идёт по двум каналам:

- **сеть** `1c-suite-net` (объявлена в основном compose, импортируется
  yaxunit-stack как external) — раннер 1С виден как `onec-server:8019`;
- **shared volume** `yaxunit-payloads` — `mcp-testing` складывает туда
  XML-выгрузки, раннер читает по тому же пути. Это убирает передачу
  байтов конфигурации через LLM-контекст.

Tools:

| Tool | Что делает |
|---|---|
| `test_runner_health()` | Готовность раннера + проверка обоих концов shared volume |
| `test_run_path(config_path, tests_path, mode)` | **Основной**. Прогнать тесты по путям внутри `/workspace`. Не передаёт байты через LLM. |
| `test_run(archive_base64, mode)` | LEGACY. Тот же прогон через base64-zip. Оставлен для shim-скриптов и ручных curl. |
| `test_run_status(run_id)` | Полные детали прогона, включая JUnit XML и лог |
| `test_run_list()` | Последние ~100 прогонов, summary без payload |

Чтобы поднять стек тестирования:

```bash
cd yaxunit-stack
docker compose --profile testing up -d --build
# проверка
curl http://localhost:8019/health    # должен вернуть payloads_volume_mounted=true
```

Sanity-check сквозного пути (после поднятия обоих стеков):

```bash
python3 scripts/smoke_yaxunit.py --mode file
# или --mode server, если есть серверная community-лицензия
```

Скрипт берёт демо-данные из `yaxunit-stack/sandbox/demo-{config,tests}`,
копирует во временный каталог под `workspace/`, дёргает `test_run_path`
и проверяет, что вернулось `status: passed`. Полезно после пересборки
любого из стеков, чтобы убедиться, что shared volume и сеть не отвалились.

Подробный workflow для агента — в скилле `workspace/.opencode/skills/test-generate/SKILL.md`.

### Грабли при первом запуске

Две вещи, которые отнимут много времени, если про них не знать:

**1. Лицензия 1С нужна и для file-mode.** Шаг 2 пайплайна (`DESIGNER /LoadConfigFromFiles`) использует утилиту платформы, которая требует лицензию. Без лицензии в логе шага будет `License not found. Software protection key or acquired software license not found!`. Получение лицензии описано в `yaxunit-stack/README.md` — именно с подъёмом ragent в `license-helper` перед формой запроса, иначе developer.1c.ru выдаст только клиентскую (personal), которая не подходит.

**2. На Windows + Docker Desktop используйте `127.0.0.1`, а не `localhost`** при подключении к MCP-серверам из Python-скриптов. После пересборки контейнеров на одном порту иногда параллельно остаются слушать два процесса: `wslrelay` на IPv6 (`::1`) и `com.docker.backend` (рабочий). Python через httpx по умолчанию идёт на IPv6 → попадает на зависший relay → получает невнятный 503 на любой запрос. Curl ходит иначе и попадает в нужный. `smoke_yaxunit.py` по дефолту использует `127.0.0.1`; для других скриптов помогает либо явный `127.0.0.1`, либо перезапуск Docker Desktop (он переподнимает relay-процессы корректно).

## Установка

### 1. Подготовка

```bash
cp .env.example .env
# Заполните OPENROUTER_API_KEY (или ANTHROPIC_API_KEY),
# ONEC_AI_TOKEN (для Напарника),
# ONEC_BASE_URL/USER/PASSWORD (для REST-прокси, опционально).
# ОБЯЗАТЕЛЬНО проверьте, что ONEC_READ_ONLY=true для непроверенной базы.
```

Положите данные в нужные папки:

```bash
1c-config-xml/        # XML-выгрузка вашей конфигурации 1С
its-articles/         # PDF-статьи ИТС (Ctrl+P → Save as PDF на its.1c.ru)
platform-help-data/   # .hbk файлы синтакс-помощника платформы
sonar-plugins/        # sonar-bsl-plugin-community.jar (см. sonar-plugins/README.md)
```

### 2. Проверка готовности окружения

Перед запуском проверьте, что всё настроено корректно. Скрипт сразу покажет,
чего не хватает и как исправить — вместо того чтобы ловить непонятные ошибки
при старте контейнеров.

```bash
# Linux/macOS
make check-prereqs
# или напрямую:
python3 scripts/check_prereqs.py

# Windows
py scripts\check_prereqs.py
```

Скрипт проверит: наличие Docker, валидность `.env`, заполнение токенов,
наличие XML-выгрузки, `.hbk`-файлов, `.jar`-плагина SonarQube и (на Windows)
корректность разделителя в `COMPOSE_FILE`.

### 3. Сборка и запуск

```bash
docker compose up -d --build
```

При первом запуске:

- `metadata-indexer` распарсит XML конфигурации в Neo4j
- `help-indexer` распарсит `.hbk` справки в Qdrant (`platform_help`)
- `its-indexer` распарсит PDF-статьи в Qdrant (`its_articles`,
  **dense + sparse BM25**)
- `code-indexer` проиндексирует workspace `.bsl` файлы

После завершения индексации все MCP-серверы будут доступны на своих
портах, и OpenCode подцепит их через `mcp-config.json`.

### 4. Защита MCP-серверов общим секретом (задача 3.2)

По умолчанию SSE-эндпоинты MCP-серверов поднимаются **без
аутентификации** — любой процесс в docker-сети может вызвать любой
инструмент (`sonar_scan_code`, `http_service_call`, `platform_help_*`
и т.д.). Для локальной разработки это удобно, но для общего/удалённого
кластера — небезопасно, особенно после того, как появился
`mcp-rest-proxy`, умеющий ходить в живую 1С.

**Чтобы включить аутентификацию:**

1. Сгенерируйте секрет (не короче 32 символов):

   ```bash
   # Linux/macOS
   openssl rand -hex 32

   # Windows PowerShell
   -join ((1..32) | %{'{0:x2}' -f (Get-Random -Max 256)})
   ```

2. Запишите его в `.env`:

   ```env
   MCP_SHARED_SECRET=<сгенерированный секрет>
   ```

3. Перезапустите весь стек:

   ```bash
   docker compose down
   docker compose up -d
   ```

**Что изменится:**

- Все MCP-серверы начнут требовать заголовок
  `Authorization: Bearer <secret>` (или альтернативный `X-MCP-Secret`)
  на каждый запрос к `/sse` и `/messages/*`. Без него — `401 Unauthorized`.
- OpenCode, оркестратор, workspace-watcher и триггер реиндексации
  автоматически подставят заголовок: они читают ту же переменную.
- В логах контейнеров при старте появится
  `[mcp-auth] <name>: auth ENABLED (secret length=N)`.

**Если оставить `MCP_SHARED_SECRET` пустым** — серверы стартуют как
раньше, без middleware, и в логе каждого контейнера будет WARNING
`MCP_SHARED_SECRET не задан, SSE-эндпоинт открыт без аутентификации`.

**Самопроверка:** после запуска со секретом выполните:

```bash
python3 scripts/smoke_auth.py
```

Скрипт пройдётся по всем 10 MCP-серверам и убедится, что без заголовка
они возвращают 401, а с правильным — 200.

### 5. Аудит-лог REST-прокси (задача 3.3)

`mcp-rest-proxy` — единственный сервис в стеке, который умеет ходить в
*живую* базу 1С. На каждый реальный вызов tool'а (`odata_list_entities`,
`odata_get`, `odata_get_by_key`, `odata_metadata`, `http_service_call`,
`test_connection`) пишется одна строка в JSONL-файл
`/data/audit/rest-proxy.jsonl` внутри контейнера. Это отдельный от
метрик поток: метрики отвечают на "сколько и как быстро", аудит — на
"кто, когда, куда именно, с каким результатом".

**Где смотреть:**

```bash
# Внутри контейнера
docker compose exec mcp-rest-proxy tail -f /data/audit/rest-proxy.jsonl

# С хоста через утилиту-фильтр
python3 scripts/audit_tail.py --tail 20
python3 scripts/audit_tail.py --status error --since 1h
python3 scripts/audit_tail.py --tool http_service_call --pretty
```

**Что в записи:**

```json
{
  "ts": "2026-04-19T14:30:15.234Z",
  "tool": "http_service_call",
  "params": {"service_path": "orders", "method": "GET", "query_params": "id=..."},
  "read_only_mode": true,
  "onec_url": "http://host.docker.internal/base/hs/api/orders?id=...",
  "http_method": "GET",
  "http_code": 200,
  "duration_ms": 123,
  "status": "ok",
  "response_size": 4521,
  "error": null,
  "remote_ip": "172.18.0.5",
  "mcp_session_id": "ab12cd34..."
}
```

Поле `status` принимает три значения:

- `ok` — вызов прошёл, последний HTTP-код 2xx (или tool вообще не ходил
  в 1С, например `connection_info`);
- `error` — сетевая ошибка, таймаут, или 4xx/5xx от 1С;
- `blocked` — запрос не отправлялся: сработала защита read-only или
  SSRF-фильтр (`_check_method_allowed` / `_check_url_safe`). Это
  полезно видеть отдельно — так ловятся попытки модифицирующих
  операций в безопасном режиме.

**Тела запросов и ответов по умолчанию НЕ пишутся.** Там могут быть
ФИО контрагентов, суммы, договорные реквизиты — то, чему не место в
логе, читаемом всеми у кого есть доступ к docker-volume. Включить
можно флагом `REST_PROXY_AUDIT_INCLUDE_BODY=true` в `.env` — тогда в
запись добавляются поля `request_body`, `response_body` (первые 50 KB)
и, если tool сделал несколько HTTP-запросов, `http_trail`.

**Ротация:** при достижении `REST_PROXY_AUDIT_MAX_BYTES` (по умолчанию
10 MB) файл переименовывается в `.1`, старые — в `.2`, и т.д. до
`REST_PROXY_AUDIT_BACKUP_COUNT` (по умолчанию 5). Суммарно на диске в
худшем случае ~50 MB.

**Отключить аудит:** `REST_PROXY_AUDIT_ENABLED=false` в `.env`. Tools
не оборачиваются вовсе — нулевой оверхед. Полезно для E2E-прогонов,
где аудитовый мусор мешает.

### 6. Проверка гибридного поиска

```bash
# Должен показать its_collection_kind: "hybrid"
curl -X POST http://localhost:8003/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"platform_help_stats","arguments":{}}}'

# Гибридный поиск с точным термином
curl -X POST http://localhost:8003/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"its_search","arguments":{"query":"#std466","limit":5}}}'
# В ответе search_type должен быть "hybrid"
```

### 7. Eval harness (задача 3.4)

Папка `evals/` — инструмент для того, чтобы мерить не деградирует ли
поиск по справке платформы при изменениях в индексации, запросах, моделях
эмбеддингов или ранжировании. Это **не юнит-тест** — это набор реальных
запросов с ожидаемыми результатами, прогоняемый через живой MCP-сервер
(`mcp-platform-help`) по SSE-транспорту и MCP SDK.

**Когда запускать:**

- перед значимыми изменениями в `mcp-platform-help` (модели, параметры
  гибридного поиска, структура чанков) — снять baseline;
- после — сравнить с прошлым отчётом через `diff` двух markdown-файлов;
- руками, при подозрении на деградацию или после переиндексации.

**Запуск:**

```bash
# Собрать runner один раз
docker compose --profile evals build eval-runner

# Полный прогон шаблонного датасета (10 примеров)
docker compose --profile evals run --rm eval-runner

# Или через хост-обёртку (короче, поддерживает --limit и --dataset)
python3 scripts/eval.py                   # все 10 примеров
python3 scripts/eval.py --limit 3         # первые 3 — быстрая санчек

# Посмотреть свежий отчёт
ls -t evals/reports/ | head -4            # json+md последнего прогона
```

Отчёты падают в `evals/reports/platform_help_YYYYMMDD_HHMMSS.{json,md}`
через bind-volume `./evals:/app/evals`. Таймстемп в имени — чтобы не
затирать историю; два последних отчёта удобно диффать:
`diff evals/reports/platform_help_*.md`.

**Формат датасета** — JSONL, описан в `evals/schema.md`. Шаблонный
`evals/datasets/platform_help.jsonl` — 10 примеров со **смешанным
ground truth**: обязательные hard-предикаты (напр. `name_in_top_k`
для известных методов: `СтрРазделить`, `НачалоДня`, `ЗафиксироватьТранзакцию`)
и мягкие soft-предикаты для размытых запросов (`any_hit_kind`,
`full_name_contains`). Как добавлять свои — `evals/datasets/README.md`.

**Что считается в отчёте:**

- **Hard pass-rate** — доля примеров, где все hard-предикаты прошли.
  Основная метрика качества; `exit 0` только при 100%.
- **Soft pass-rate** — справочно, для мониторинга.
- **Recall@1 / Recall@5 / Recall@10** — по примерам с hard `name_in_top_k`.
- **MRR** — среднее `1/rank`, там же.
- **Латентность** — min/median/p95/max по `call_tool` (без учёта
  одноразового `initialize`).
- **Распределение режимов поиска** — `hybrid` / `dense_only` /
  `builtin_fallback`. Если `dense_only` вдруг начал доминировать —
  значит отвалился Qdrant или sparse-модель.

**Архитектурные моменты, которые стоит знать:**

- Runner — отдельный контейнер (`python:3.12-slim` + `mcp[cli]`), не
  постоянный сервис. Профиль `evals` в compose — это `docker compose
  --profile evals run`, обычный `docker compose up` его не трогает.
- Одна SSE-сессия на весь прогон датасета. Если поднимать новую сессию
  на каждый пример, platform-help будет повторно грузить dense/BM25
  модели и каждый вызов добавит 10–20 с.
- `MCP_SHARED_SECRET` пробрасывается из `.env` через compose
  (в `environment` сервиса), runner сам формирует `Authorization: Bearer`.
  При пустом секрете — работает без header (локальный dev-режим 3.2).

**Unit-тесты runner'а:** `python3 evals/runner/tests.py` — 5 блоков
без сети (predicates / metrics / парсер датасета / mrr-info /
run_one + report через FakeSession). Полезно запускать перед
коммитом изменений в `evals/runner/`.

### 8. Smoke-тест MCP через SSE (задача 3.5)

`scripts/smoke_mcp.py` — end-to-end проверка, что все 10 MCP-серверов
реально отвечают через SSE + MCP SDK, а не просто слушают порт. Для
каждого сервера открывается полноценная сессия (`sse_client` +
`ClientSession.initialize`), вызывается один безопасный read-only
tool, результат валидируется (`not isError`, непустой ответ, для
JSON-tools — `json.loads` + семантическая проверка).

**Зачем поверх `smoke_auth.py`:** `smoke_auth.py` (3.2) проверяет
только HTTP-уровень — 401 без заголовка, 200 с правильным Bearer.
Он не умеет сказать «FastMCP-приложение внутри живое и tools реально
работают». `smoke_mcp.py` (3.5) закрывает именно этот уровень. Два
скрипта взаимодополняющие: сначала auth-smoke подтверждает, что
middleware настроена правильно, затем mcp-smoke — что все tools
доступны и не падают.

**Что проверяется для каждого сервера:**

| Сервер                  | Tool                        | Аргументы                                   |
|-------------------------|-----------------------------|---------------------------------------------|
| mcp-metadata-graph      | `metadata_stats`            | `{}`                                        |
| mcp-bsl-checker         | `bsl_check_code`            | минимальный валидный BSL                    |
| mcp-platform-help       | `platform_help_stats`       | `{}` — плюс проверка `total_points > 0`     |
| mcp-1c-naparnik         | `naparnik_check_connection` | `{}` — без реального запроса в code.1c.ai   |
| mcp-code-templates      | `templates_count`           | `{}`                                        |
| mcp-query-builder       | `query_validate`            | `ВЫБРАТЬ 1 КАК Поле`                        |
| mcp-testing             | `test_runner_health`        | `{}`                                        |
| mcp-code-rag            | `code_search`               | `query="процедура", limit=1`                |
| mcp-rest-proxy          | `connection_info`           | `{}` — только читает конфиг, НЕ ходит в 1С  |
| mcp-sonarqube           | `sonar_list_projects`       | `{}`                                        |

Все tools идемпотентны: не запускают scans, не пишут в Neo4j, не ходят
в живую 1С, не тратят токены платных сервисов.

**Запуск:**

Два способа — через docker-профиль (не требует Python на хосте) или
с хоста напрямую (быстрее, если у вас есть Python 3.10+).

```bash
# ─── Способ 1: через docker-compose (рекомендуется) ───────────────
# Стек должен быть поднят:  docker compose up -d
# Первый раз билдится образ smoke-runner (~30 секунд), дальше — мгновенно.
docker compose --profile smoke run --rm smoke-runner

# Те же флаги передаются как аргументы:
docker compose --profile smoke run --rm smoke-runner \
  python3 /app/scripts/smoke_mcp.py --config /app/mcp-config.json \
  --only mcp-platform-help --verbose

# ─── Способ 2: с хоста (нужен Python 3.10+ с mcp[cli] и httpx) ────
python3 scripts/smoke_mcp.py                  # весь стек
python3 scripts/smoke_mcp.py --only mcp-platform-help,mcp-code-rag
python3 scripts/smoke_mcp.py --verbose
python3 scripts/smoke_mcp.py --host 192.168.1.10 --timeout 60
```

Разница между способами только в том, как адресуются серверы:
в контейнере ходим по docker-DNS (`mcp-metadata-graph:8001` и т.д. —
режим включается флагом `MCP_USE_DOCKER_NAMES=1` из compose), с хоста —
на `localhost:8001..8014` по опубликованным портам.

Для добавления в Makefile/CI:

```makefile
smoke:
	docker compose --profile smoke run --rm smoke-runner
```

**Exit-code:** `0` только если все выбранные сервера ответили корректно.
При любой недоступности, isError, пустом ответе или нарушении семантики
(platform_help с нулевой коллекцией, например) — `1`. Пригодно для CI.

**Типичный вывод:**

```
Host: localhost
Secret: задан (length=64)
Проб: 11 из 11
Timeout на пробу: 30.0s
────────────────────────────────────────────────────────────────────
… mcp-metadata-graph:8001  →  metadata_stats()
✓ mcp-metadata-graph    :8001  metadata_stats           0.28s  keys=5
✓ mcp-bsl-checker       :8002  bsl_check_code           0.91s  text ok (412 chars)
✓ mcp-platform-help     :8003  platform_help_stats      0.19s  kind=hybrid points=40976
... и так далее ...
────────────────────────────────────────────────────────────────────
Pass: 11/11   Fail: 0   Total time: 8.3s

ИТОГ: PASS
```

**Архитектурные моменты:**

- Общая инфраструктура с `smoke_auth.py` вынесена в `scripts/_smoke_common.py`:
  список `MCP_SERVERS`, загрузка секрета по иерархии `--secret` → env → `.env`,
  санити-чек соответствия списка с `1c-mcp-suite/mcp-config.json`.
- MCP-клиент переиспользуется из `evals/runner/mcp_client.py` — тот же
  `MCPSession` с корректной обработкой headers, таймаутов и закрытия сессии.
- Отдельная SSE-сессия на каждый сервер (последовательно, не параллельно).
  Параллельный запуск быстрее на 3–5 секунд, но усложняет диагностику при
  падении и провоцирует гонку на медленных машинах при старте FastMCP.
- Для UNREACHABLE разворачивается ExceptionGroup (MCP SDK пакует ошибки
  соединения через anyio.TaskGroup) — в отчёте видна реальная причина,
  не «unhandled errors in a TaskGroup».

**Попутный фикс `smoke_auth.py` (3.2):** переписан `_probe` на
`client.stream("GET", ...)`, который возвращает `Response` сразу после
заголовков и не гонится с SSE keep-alive. Раньше в некоторых окружениях
`httpx` давал `ReadTimeout` ДО того, как успевал прочитать `status_code=401`,
из-за чего probe «без заголовка» интерпретировался как «стрим пошёл» и
выдавал false-FAIL. Теперь все три пробы стабильны.

## Troubleshooting

### Smoke-скрипты с хоста возвращают `503` на каждый сервер

**Симптом:** `python scripts\smoke_auth.py` показывает `503` для всех
10 MCP-серверов, при этом `curl.exe -i http://localhost:8001/sse`
из той же PowerShell-сессии корректно отвечает `401`.

**Причина:** На машине настроен системный HTTP-прокси (типичный сценарий —
v2rayN, Clash, Shadowsocks или корпоративный прокси на `127.0.0.1:10809`
или похожем порту). Python `httpx` по умолчанию подхватывает системный
прокси Windows из реестра (даже если переменные `HTTP_PROXY`/`HTTPS_PROXY`
не выставлены). Все запросы на `localhost:8001..8014` уходят через прокси,
который не умеет форвардить в loopback и отвечает `503`. `curl.exe`
системный прокси не использует, поэтому работает.

**Проверка:**

```powershell
python -c "import urllib.request; print(urllib.request.getproxies())"
# Если вывело {'http': 'http://127.0.0.1:NNNNN', ...} — диагноз подтверждён
```

**Фикс:** установить `NO_PROXY=localhost,127.0.0.1` для текущей
PowerShell-сессии или системно для пользователя.

```powershell
# Только текущая сессия:
$env:NO_PROXY = "localhost,127.0.0.1"

# Постоянно для пользователя (нужно перезапустить PowerShell):
[System.Environment]::SetEnvironmentVariable("NO_PROXY", "localhost,127.0.0.1", "User")
```

После этого `smoke_auth.py` и `smoke_mcp.py` пойдут в localhost напрямую.

Внутри docker-контейнеров (включая `smoke-runner`, `eval-runner`) системный
прокси Windows не виден — поэтому проблема возникает только при запуске
скриптов с хоста.

### Контейнер `mcp-orchestrator` остался после применения патча 1.4

**Симптом:** в `docker compose ps` виден `mcp-orchestrator` с возрастом
несколько часов/дней, при том что в `docker-compose.yml` такого сервиса
больше нет.

**Причина:** Compose не удаляет контейнеры от исчезнувших сервисов
автоматически при `up -d --build`.

**Фикс:**

```powershell
docker rm -f mcp-orchestrator
# или полный рестарт стека:
docker compose down ; docker compose up -d --build
```

## Миграция с v2 / v2.1 на v3

Если у вас уже работала установка v2 или v2.1:

1. **Замените файлы.** Перенесите содержимое этой папки поверх вашей,
   сохранив `.env` и каталоги с данными (`1c-config-xml`, `its-articles`,
   `platform-help-data`, `workspace`, `custom-templates`).

2. **Пересоберите образы** (нужно для новых зависимостей в
   `requirements-embeddings.txt`):

   ```bash
   docker compose build --no-cache mcp-platform-help its-indexer help-indexer mcp-code-rag
   ```

3. **Перезапустите.** При первом запуске сервер `mcp-platform-help`
   обнаружит, что коллекция `its_articles` в старом плоском формате, и
   будет работать в режиме `legacy_dense` через fallback. Чтобы
   получить гибридный поиск, перезапустите индексатор:

   ```bash
   docker compose run --rm its-indexer
   ```

   Он сам удалит старую коллекцию и создаст новую с двумя векторами.
   Если по какой-то причине автоматическая миграция не сработает,
   используйте флаг:

   ```bash
   docker compose run --rm -e ITS_FORCE_REINDEX=true its-indexer
   ```

4. **Все остальные слои** (пагинация metadata-graph, REST-прокси,
   оркестратор, code-rag, тесты, шаблоны) уже в составе образа и
   работают сразу после `docker compose up -d --build`.

## Ссылки на исходные пакеты

- v2: `1c-mcp-suite-v2/README.md` — детали по query-builder, testing,
  code-rag, оркестратору, метрикам, кэшу
- v2.1: `1c-mcp-suite-v2.1/README.md` — детали по пагинации, REST-прокси,
  трёхслойной защите read-only

Эти README остались валидной справочной документацией, просто их
содержимое теперь скопировано в один пакет, и пути в командах установки
больше не нужны — всё уже разложено по местам.

## Лицензия

MIT

## Пример промпта
Допиши YAxUnit-тесты для функции Факториал из модуля АукОбщийКлиент.

Тестовые случаи:
- Факториал(0) = 1
- Факториал(1) = 1
- Факториал(5) = 120
- Факториал(-1) — должно бросить исключение

Имя тестового модуля: Тест_Аукционы_АукОбщийКлиент.

Шаги:
1. Создай файлы Тест_Аукционы_АукОбщийКлиент.xml и
   Тест_Аукционы_АукОбщийКлиент/Ext/Module.bsl в
   /workspace/tests-extension/CommonModules/ по шаблону SKILL.md
   (UTF-8 BOM, CRLF, <InternalInfo/>, свежий uuid).
2. Зарегистрируй модуль в /workspace/tests-extension/Configuration.xml
   (<ChildObjects>) и в /workspace/tests-extension/ConfigDumpInfo.xml.
3. Запусти test_run_path с config_path=/workspace,
   tests_path=/workspace/tests-extension, mode=server.
4. Получив run_id, опрашивай test_run_status каждые 30 секунд.
5. Покажи итоговый отчёт.
