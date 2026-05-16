# Плагины SonarQube для 1С

Положите сюда jar-файл плагина **sonar-communitybsl-plugin** (исторически
назывался `sonar-bsl-plugin-community`) — он добавляет поддержку языка 1С BSL
в SonarQube. Без него SonarQube видит `.bsl` как plain text и возвращает
0 issues на любом коде.

## Простой путь — установщик

```bash
python3 scripts/install_sonar_bsl_plugin.py
```

Скрипт:

1. если в этой папке уже лежит подходящий jar (любой версии) — не трогает;
2. если нет — качает последний релиз с GitHub и кладёт сюда;
3. печатает дальнейшие шаги (рестарт SonarQube + provision Quality Gate).

Опции:

```bash
# принудительно перекачать
python3 scripts/install_sonar_bsl_plugin.py --force

# жёстко закрепить версию (для CI)
python3 scripts/install_sonar_bsl_plugin.py --version 1.18.0

# только проверить (exit 0/1) — годится для скриптов
python3 scripts/install_sonar_bsl_plugin.py --check
```

## Ручной путь

1. Откройте https://github.com/1c-syntax/sonar-bsl-plugin-community/releases
2. Скачайте последний `sonar-communitybsl-plugin-<версия>.jar`
3. Скопируйте файл в эту папку (`./sonar-plugins/`)
4. Перезапустите контейнер SonarQube:

```bash
docker compose restart sonarqube
```

Папка монтируется в контейнер как
`/opt/sonarqube/extensions/plugins` (read-only).

## Первичная настройка SonarQube

1. Откройте http://localhost:9001  (логин/пароль по умолчанию: `admin` / `admin`)

   > Почему `9001`, а не `9000` как в документации SonarQube? Порт `9000` на хосте
   > занят нашим сервером метрик MCP (`mcp_metrics.py`), поэтому в compose-файле
   > SonarQube проброшен на `9001:9000` — внутри контейнера он по-прежнему слушает
   > `9000`, снаружи доступен на `9001`.

2. Смените пароль.
3. **My Account → Security → Generate Token** — создайте токен.

   > Важно: выберите тип **User Token** (префикс `squ_`). НЕ выбирайте
   > "Global Analysis Token" (префикс `sqa_`) — у него урезанные права, он
   > умеет только запускать анализ и падает на `sonar_list_projects`
   > и других административных операциях.

4. Запишите токен в `.env` файл рядом с `docker-compose.yml`:

```
SONAR_TOKEN=squ_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

5. Пересоздайте mcp-sonarqube (именно `up`, не `restart` — `restart` не
   перечитывает `.env`):

```bash
docker compose up -d --force-recreate mcp-sonarqube
```

6. Проверьте, что токен подхватился:

```bash
docker exec mcp-sonarqube env | grep SONAR_TOKEN
```

## Quality Gate '1C BSL'

Дефолтный Quality Gate `Sonar way` завязан на coverage / new_code, у нас
этих данных нет — поэтому он вечно `OK` и `sonar_quality_gate` MCP-tool как
валидатор бесполезен. Создайте gate `1C BSL` со включёнными BSL-правилами:

```bash
# Базовая установка
python3 scripts/provision_sonar_quality_gate.py

# Полная: gate + дефолтным сделать + привязать к существующим 1c-agent-* проектам
python3 scripts/provision_sonar_quality_gate.py --set-default --bind-existing
```

Скрипт идемпотентный — можно запускать многократно. Если состав условий
изменился (правки в `RULESET` внутри скрипта) — добавьте `--recreate`.

## Smoke-тест BSL-анализа

После установки плагина и Quality Gate проверьте, что весь путь работает:

```bash
python3 scripts/smoke_sonar_bsl.py
```

Скрипт шлёт в `mcp-sonarqube` маркерный BSL-сниппет с заведомо-проблемной
функцией и проверяет, что:

- issues_total > 0 (значит, плагин что-то нашёл)
- среди rule-id есть BSL-правила (а не только общие)

Если smoke падает — в выводе есть точный диагноз вероятной причины.

После этого агент сможет вызывать `sonar_scan_code`, `sonar_quality_gate`
и остальные инструменты MCP-сервера SonarQube — и получать осмысленный
сигнал, а не вечный `0 issues / OK`.
