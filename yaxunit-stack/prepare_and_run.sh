#!/bin/bash
set -e

# КРИТИЧНО: при запуске через `docker compose exec -u root` env-переменные
# локали могут не унаследоваться, и `zip` начинает писать имена с
# кириллицей в escape-синтаксисе (#U041a...). Явно фиксируем UTF-8.
export LANG=ru_RU.UTF-8
export LC_ALL=ru_RU.UTF-8

# Подготовить рабочую папку
rm -rf /tmp/smoke
mkdir -p /tmp/smoke/config /tmp/smoke/tests

# Источник — bind mount ./sandbox (смонтирован в compose у сервиса client).
# НЕ использовать /tmp/demo-* — `docker cp` с Windows-хоста портит имена
# файлов с кириллицей. Bind mount сохраняет имена в UTF-8 как есть.
SRC_CONFIG="${SRC_CONFIG:-/sandbox/demo-config}"
SRC_TESTS="${SRC_TESTS:-/sandbox/demo-tests}"

if [ ! -d "$SRC_CONFIG" ] || [ ! -d "$SRC_TESTS" ]; then
    echo "ERROR: source dirs not found:"
    echo "  SRC_CONFIG=$SRC_CONFIG"
    echo "  SRC_TESTS=$SRC_TESTS"
    exit 1
fi

cp -r "$SRC_CONFIG"/* /tmp/smoke/config/
cp -r "$SRC_TESTS"/*  /tmp/smoke/tests/

# Установить zip если нет
command -v zip >/dev/null || (apt-get update -qq && apt-get install -y zip)

# Создать архив (zip тихо, без прогресса)
cd /tmp/smoke
zip -rq /tmp/smoke.zip config tests

# Показать содержимое — флаг -U оставляет UTF-8 имена как есть
# (иначе unzip транслирует их через текущую локаль и может выдать
# escape-синтаксис #U041a...)
echo "=== Archive contents ==="
unzip -Ul /tmp/smoke.zip | head -20

# Отправить в MCP
echo "=== Sending to MCP ==="
curl -s -X POST -F "archive=@/tmp/smoke.zip" http://localhost:8019/run_tests > /tmp/result.json

echo "=== Result ==="
python3 << 'PYEOF'
import json
with open("/tmp/result.json") as f:
    d = json.load(f)
for k, v in d.items():
    if k not in ("junit_xml", "log"):
        print(f"{k} = {v}")
PYEOF