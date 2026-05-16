# 1C YAxUnit MCP Stack

HTTP-сервер для запуска юнит-тестов 1С (через YAxUnit), предназначенный для использования AI-агентами. Агент собирает архив с конфигурацией и тестами, отправляет на `/run_tests`, получает JUnit-отчёт.

## Что вы делаете перед первым запуском

Только три вещи руками:

1. **Дистрибутивы 1С** — положить `.deb` пакеты в `client-vnc/distr/` и `server/distr/` (см. README в этих папках).
2. **Лицензия 1С** — серверная community-лицензия должна оказаться в `./licenses/license-backup.lic`.
   - Если файл лежит в корне проекта — выполните `./init-license.sh` (он перенесёт его в `licenses/` и удалит исходник во избежание утечки в git).
   - Если лицензии нет — см. секцию «Получение лицензии» ниже.
3. Один раз выполнить:
   ```bash
   docker compose up -d --build
   ```

Через 5–10 минут (первая сборка качает базовые образы и собирает 1С) MCP-сервер доступен на `http://localhost:8019`.

## Что разворачивается

| Сервис | Порт | Назначение |
|--------|------|------------|
| `client` (MCP + VNC) | **8019** | MCP HTTP API — основная точка входа для AI-агента |
| `client` (noVNC) | **6080** | Веб-VNC для отладки (Конфигуратор глазами) |
| `allure-ui` | **5253** | Дашборд с историей прогонов |
| `allure` | 5252 | Allure REST API |
| `app` | 1540, 1541, 1545, 1560-1591 | Кластер 1С (ragent + RAS) |
| `postgres` | 5432 | PostgreSQL для серверных ИБ |
| `pgadmin` | 5050 | Web UI для postgres |

> Порты `5900/6080/8019` физически проброшены сервисом `app` — `client` сидит в его сетевом неймспейсе через `network_mode: service:app`.

## API MCP-сервера

### `POST /run_tests`

Принимает multipart-форму с полем `archive` — zip-файлом следующей структуры:

```
archive.zip
├── config/                      ← основная конфигурация (выгрузка в XML)
│   ├── Configuration.xml
│   └── ...
├── tests/                       ← расширение с тестами (выгрузка в XML)
│   ├── Configuration.xml        ← <n>Tests</n> или любое имя, читается автоматически
│   └── ...
└── yaxunit.json                 ← опционально, настройки запуска
```

Имя расширения тестов **читается из `tests/Configuration.xml`** — может быть любым, в т.ч. кириллическим.

Дополнительное поле формы:
- `mode` — `"file"` или `"server"` (по умолчанию из env `PIPELINE_MODE`, в compose — `server`)

Возвращает JSON:

```json
{
  "run_id": "abc123def456",
  "status": "passed",
  "tests": 5,
  "failures": 0,
  "errors": 0,
  "duration_sec": 38.4,
  "extension": "Tests",
  "mode": "server",
  "junit_xml": "<?xml ... полный JUnit XML ...>",
  "log": "<?... вывод pipeline ...>"
}
```

### Прочие эндпоинты

- `GET /runs` — список последних прогонов (без `junit_xml`/`log`)
- `GET /runs/{run_id}` — полные детали прогона
- `GET /runs/{run_id}/junit.xml` — чистый JUnit XML
- `GET /health` — проверка готовности

## Пример использования агентом

```bash
cd /path/to/project
zip -r mytest.zip config/ tests/
curl -X POST -F "archive=@mytest.zip" http://localhost:8019/run_tests | jq
open http://localhost:5253        # Allure
```

Удобный shim для локальной разработки лежит рядом: `prepare_and_run.sh` (запаковывает `sandbox/` в zip и шлёт в MCP) и `show_log.py` (печатает результат).

## Получение лицензии

> **Подробная пошаговая инструкция со скриншотами и трюками — в
> [TROUBLESHOOTING-LICENSE.md](./TROUBLESHOOTING-LICENSE.md)**.

Если файла `.lic` ещё нет:

```bash
docker compose stop app client
docker compose --profile license up -d --build license-helper
```

Затем в http://localhost:6080 выполнить процедуру (подробности — в комментариях к сервису `license-helper` в `docker-compose.yml` или в TROUBLESHOOTING-LICENSE.md).

**КРИТИЧНО**: для серверного режима лицензия должна быть **серверная**. Чтобы её получить, перед открытием формы запроса лицензии обязательно запустите ragent внутри license-helper:

```bash
docker compose exec -d license-helper /opt/1cv8/x86_64/current/ragent \
    -port 1540 -regport 1541 -range 1560:1591
```

Без этого developer.1c.ru выдаст только клиентскую (personal) лицензию, и серверный режим работать не будет.

После получения и копирования файла:

```bash
docker compose --profile license stop license-helper
docker compose up -d
```

## Режимы прогона

`PIPELINE_MODE` в compose у сервиса `client`:

- **`server`** (по умолчанию) — клиент-серверная ИБ через PostgreSQL. Ближе к продакшну. Требует **серверной** лицензии.
- **`file`** — файловая ИБ в `/tmp`. Быстрее, не требует postgres, работает с любой community-лицензией.

## Архитектурные особенности

### Шаг 7.5 пайплайна — снятие безопасного режима у расширений

На платформе 8.3.23.1865 ключи `-SafeMode-` / `-UnsafeActionProtection-` у `DESIGNER /LoadCfg` молча игнорируются. Расширение всё равно создаётся с «Безопасный режим = Истина», и YAxUnit падает с «Расширение подключено в безопасном режиме. Чтение конфигурационного файла недоступно».

Решение — утилита `ibcmd` (часть платформы), которая пишет свойства расширения напрямую в метаданные ИБ:

```bash
ibcmd extension --db-path=<dir> update --name=YAXUNIT --safe-mode=no --unsafe-action-protection=no
# или для server-mode:
ibcmd extension --dbms=PostgreSQL --db-server=... --db-name=... update --name=YAXUNIT --safe-mode=no --unsafe-action-protection=no
```

Имя расширения — `YAXUNIT` (всегда uppercase в метаданных .cfe), даже если в `/LoadCfg -Extension YAxUnit` указано иначе.

### Allure через HTTP API

Pipeline отдаёт JUnit XML напрямую в Allure API (`POST /allure-docker-service/send-results?project_id=default`), а не через сканирование файловой системы — так надёжнее для произвольных путей и форматов.

## Известные нюансы

- При `--force-recreate client` теряется содержимое `/tmp/` контейнера — нужно повторно скопировать `prepare_and_run.sh`, `show_log.py` и тестовые данные:
  ```bash
  docker cp ./sandbox/demo-config onec-client:/tmp/demo-config
  docker cp ./sandbox/demo-tests  onec-client:/tmp/demo-tests
  docker cp ./prepare_and_run.sh  onec-client:/tmp/prepare_and_run.sh
  docker cp ./show_log.py         onec-client:/tmp/show_log.py
  ```
- При `--force-recreate app` recreate'ятся также `client` (зависит от его сети) и нужны пересоздания всех вспомогательных сервисов.

## Удалить всё и начать сначала

```bash
docker compose --profile license down -v --remove-orphans
```

Это снесёт все контейнеры обоих профилей и **все volumes** (`pgdata`, `srvinfo`, `yaxunit-reports`, `allure-reports` — то есть базы 1С, кэш кластера, истории прогонов и отчёты Allure). Лицензия в `./licenses/` и дистрибутивы в `*/distr/` останутся (это bind mount'ы / build context).
