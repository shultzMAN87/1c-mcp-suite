#!/bin/bash
# full_pipeline.sh — Unified CI pipeline for 1C YAxUnit tests
#
# Reads from env (set by mcp_server.py or CLI):
#   PIPELINE_MODE, SRC, TESTS, EXT_NAME, JUNIT_PATH, REPORT_DIR, RUN_ID, REF
#   ALLURE_RESULTS, PG_HOST, PG_USER, PG_PWD, SRV
set -uo pipefail

MODE="${PIPELINE_MODE:-file}"
TIMEOUT="${TIMEOUT:-300}"
SRC="${SRC:-/sandbox/demo-config}"
TESTS="${TESTS:-/sandbox/demo-tests}"
EXT_NAME="${EXT_NAME:-Tests}"
YAX="${YAX:-/opt/yaxunit/yaxunit.cfe}"
RUN_ID="${RUN_ID:-$(date +%s)}"
REPORT_DIR="${REPORT_DIR:-/tmp/pipeline_reports/$RUN_ID}"
JUNIT_PATH="${JUNIT_PATH:-$REPORT_DIR/junit.xml}"
LOG_DIR="${LOG_DIR:-/tmp/pipeline_logs/$RUN_ID}"
IB_DIR="${IB_DIR:-/tmp/pipeline_ib/$RUN_ID}"
ALLURE_RESULTS="${ALLURE_RESULTS:-/reports/allure-results/$RUN_ID}"

# Server mode params
SRV="${SRV:-onec-server}"
REF="${REF:-test_$RUN_ID}"
PG_HOST="${PG_HOST:-onec-postgres}"
PG_USER="${PG_USER:-${DB_USER:-postgres}}"
PG_PWD="${PG_PWD:-${DB_PWD:-postgres}}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)    MODE="$2";    shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# Find platform binary
PLATFORM=$(find /opt/1cv8 -name "1cv8" -type f 2>/dev/null | head -1)
[ -z "$PLATFORM" ] && { echo "FATAL: 1cv8 not found"; exit 2; }

# Find ibcmd — used to disable extension safe-mode programmatically.
# It ships alongside 1cv8 in the same version dir.
IBCMD=$(find /opt/1cv8 -name "ibcmd" -type f 2>/dev/null | head -1)
[ -z "$IBCMD" ] && { echo "FATAL: ibcmd not found"; exit 2; }

mkdir -p "$LOG_DIR" "$REPORT_DIR" "$ALLURE_RESULTS"

log() { echo "[$(date +%H:%M:%S)] $*"; }
STEP=0

run_step() {
    STEP=$((STEP + 1))
    local desc="$1"; shift
    log "=== [$STEP] $desc ==="
    "$@"
    local rc=$?
    if [ $rc -ne 0 ]; then
        log "FAIL step $STEP (exit=$rc)"
        return $rc
    fi
    return 0
}

log "Mode=$MODE  Ext=$EXT_NAME  Run=$RUN_ID"

# IB connection
if [ "$MODE" = "file" ]; then
    rm -rf "$IB_DIR"; mkdir -p "$IB_DIR"
    IB_ARG=(/F "$IB_DIR")
    run_step "CREATEINFOBASE (file)" \
        "$PLATFORM" CREATEINFOBASE "File=\"$IB_DIR\";Locale=\"ru_RU\";" \
        /OUT "$LOG_DIR/01_create.log" /DisableStartupDialogs /DisableStartupMessages || exit $?
else
    log "Drop existing DB $REF if any..."
    PGPASSWORD="$PG_PWD" dropdb -h "$PG_HOST" -U "$PG_USER" --if-exists "$REF" 2>/dev/null || true
    IB_ARG=(/S "$SRV/$REF")
    run_step "CREATEINFOBASE (server)" \
        "$PLATFORM" CREATEINFOBASE \
        "Srvr=\"$SRV\";Ref=\"$REF\";DBMS=\"PostgreSQL\";DBSrvr=\"$PG_HOST\";DB=\"$REF\";DBUID=\"$PG_USER\";DBPwd=\"$PG_PWD\";CrSQLDB=Y;SUsr=\"\";SPwd=\"\";Locale=\"ru_RU\";" \
        /OUT "$LOG_DIR/01_create.log" /DisableStartupDialogs /DisableStartupMessages || exit $?
fi

run_step "LoadConfigFromFiles main" \
    "$PLATFORM" DESIGNER "${IB_ARG[@]}" /LoadConfigFromFiles "$SRC" \
    /OUT "$LOG_DIR/02_loadcfg.log" /DisableStartupDialogs /DisableStartupMessages || exit $?

run_step "UpdateDBCfg main" \
    "$PLATFORM" DESIGNER "${IB_ARG[@]}" /UpdateDBCfg \
    /OUT "$LOG_DIR/03_updatedb.log" /DisableStartupDialogs /DisableStartupMessages || exit $?

run_step "LoadCfg YAxUnit (no safe mode)" \
    "$PLATFORM" DESIGNER "${IB_ARG[@]}" \
    /LoadCfg "$YAX" -Extension YAxUnit -SafeMode- -UnsafeActionProtection- \
    /OUT "$LOG_DIR/04_yax.log" /DisableStartupDialogs /DisableStartupMessages || exit $?

run_step "UpdateDBCfg YAxUnit" \
    "$PLATFORM" DESIGNER "${IB_ARG[@]}" \
    /UpdateDBCfg -Extension YAxUnit -SafeMode- -UnsafeActionProtection- \
    /OUT "$LOG_DIR/05_yax_upd.log" /DisableStartupDialogs /DisableStartupMessages || exit $?

# Tests extension — name comes from env (may be Cyrillic)
run_step "LoadConfigFromFiles $EXT_NAME (no safe mode)" \
    "$PLATFORM" DESIGNER "${IB_ARG[@]}" \
    /LoadConfigFromFiles "$TESTS" -Extension "$EXT_NAME" \
    -SafeMode- -UnsafeActionProtection- \
    /OUT "$LOG_DIR/06_tests.log" /DisableStartupDialogs /DisableStartupMessages || exit $?

run_step "UpdateDBCfg $EXT_NAME" \
    "$PLATFORM" DESIGNER "${IB_ARG[@]}" \
    /UpdateDBCfg -Extension "$EXT_NAME" -SafeMode- -UnsafeActionProtection- \
    /OUT "$LOG_DIR/07_tests_upd.log" /DisableStartupDialogs /DisableStartupMessages || exit $?

# === ШАГ 7.5: ФИКС БЕЗОПАСНОГО РЕЖИМА РАСШИРЕНИЙ через ibcmd ==========
# DESIGNER /LoadCfg с ключами -SafeMode-/-UnsafeActionProtection-
# на платформе 8.3.23.1865 их молча игнорирует — расширение всё равно
# создаётся с "Безопасный режим = Истина", и YAxUnit падает на чтении
# конфига с ошибкой "Расширение подключено в безопасном режиме.
# Чтение конфигурационного файла недоступно".
#
# Единственный рабочий способ снять флаги из CLI — утилита ibcmd:
# она пишет свойства расширения напрямую в метаданные ИБ.
#   * file-mode:   ibcmd extension --db-path=<dir> update ...
#   * server-mode: ibcmd extension --dbms=PostgreSQL --db-server=...
#                                   --db-name=... update ...
# На 8.3.23 ibcmd в server-mode корректно работает с базой даже когда
# ragent её держит — никакой остановки rphost/RAS не требуется.
log "=== [7.5] Disable extension safe-mode via ibcmd ==="
if [ "$MODE" = "file" ]; then
    IBCMD_ARGS=(--db-path="$IB_DIR")
else
    IBCMD_ARGS=(
        --dbms=PostgreSQL
        --db-server="$PG_HOST"
        --db-name="$REF"
        --db-user="$PG_USER"
        --db-pwd="$PG_PWD"
    )
fi

# Note: inside the .cfe the extension declares itself as "YAXUNIT"
# (uppercase), even though we load it with -Extension YAxUnit on the
# DESIGNER command line. ibcmd matches by the actual metadata name,
# so use YAXUNIT here.
for EXT in "YAXUNIT" "$EXT_NAME"; do
    log "  -> ibcmd extension update --name=$EXT --safe-mode=no --unsafe-action-protection=no"
    "$IBCMD" extension "${IBCMD_ARGS[@]}" update \
        --name="$EXT" \
        --safe-mode=no \
        --unsafe-action-protection=no \
        >"$LOG_DIR/075_ibcmd_${EXT}.log" 2>&1
    rc=$?
    if [ $rc -ne 0 ]; then
        log "WARN: ibcmd extension update for '$EXT' exited with $rc"
        cat "$LOG_DIR/075_ibcmd_${EXT}.log" || true
    fi
done

log "  -> ibcmd extension list (verification):"
"$IBCMD" extension "${IBCMD_ARGS[@]}" list 2>&1 | tee "$LOG_DIR/075_ibcmd_list.log" || true

# === ШАГ 8: Конфиг YAxUnit ===
log "=== [8] Create YAxUnit config ==="
CONFIG_JSON="${CONFIG_JSON:-/tmp/yaxunit_${RUN_ID}.json}"
cat > "$CONFIG_JSON" << JSONEOF
{
  "reportPath": "$JUNIT_PATH",
  "reportFormat": "jUnit",
  "closeAfterTests": true,
  "showReport": false,
  "filter": {
    "extensions": ["$EXT_NAME"]
  }
}
JSONEOF
cat "$CONFIG_JSON"

# === ШАГ 9: Запуск ENTERPRISE ===
# Теперь, когда шаг 7.5 реально снял безопасный режим у расширений,
# YAxUnit может нормально читать конфиг из файла — возвращаемся к
# стандартному способу запуска (RunUnitTests=<path>), как в документации.
log "=== [9] Run ENTERPRISE (timeout=${TIMEOUT}s) ==="
timeout "$TIMEOUT" "$PLATFORM" ENTERPRISE "${IB_ARG[@]}" \
    /C "RunUnitTests=$CONFIG_JSON" \
    /OUT "$LOG_DIR/09_run.log" \
    /DisableStartupDialogs /DisableStartupMessages 2>&1
RUN_RC=$?

[ -f "$LOG_DIR/09_run.log" ] && cat "$LOG_DIR/09_run.log"

log "=== [10] Cleanup + Allure ==="
# Cleanup server IB
if [ "$MODE" = "server" ]; then
    PGPASSWORD="$PG_PWD" dropdb -h "$PG_HOST" -U "$PG_USER" --if-exists "$REF" 2>/dev/null || true
fi
# Cleanup file IB
[ "$MODE" = "file" ] && rm -rf "$IB_DIR"

if [ -f "$JUNIT_PATH" ]; then
    # Send results to Allure Docker Service via its HTTP API.
    # This is more reliable than the filesystem scan: the service
    # expects either native *-result.json files or JUnit files with
    # a specific suffix (*-testsuite.xml). Posting via /send-results
    # sidesteps all of that — Allure normalizes JUnit on its side.
    ALLURE_API="${ALLURE_API:-http://onec-allure:5050/allure-docker-service}"
    ALLURE_PROJECT="${ALLURE_PROJECT:-default}"
    log "Uploading JUnit to Allure API ($ALLURE_API, project=$ALLURE_PROJECT)..."
    # JUnit must be base64-encoded inside JSON per API contract.
    JUNIT_B64=$(base64 -w0 "$JUNIT_PATH")
    PAYLOAD=$(cat <<JSON
{"results":[{"file_name":"junit-${RUN_ID}.xml","content_base64":"${JUNIT_B64}"}]}
JSON
)
    HTTP_CODE=$(curl -sS -o /tmp/allure_send.log -w "%{http_code}" \
        -X POST "$ALLURE_API/send-results?project_id=$ALLURE_PROJECT" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" || echo "000")
    log "Allure /send-results HTTP=$HTTP_CODE"
    [ "$HTTP_CODE" != "200" ] && cat /tmp/allure_send.log || true

    # Trigger report generation so the dashboard refreshes now.
    curl -sS -o /dev/null -w "generate HTTP=%{http_code}\n" \
        "$ALLURE_API/generate-report?project_id=$ALLURE_PROJECT" || true

    # Legacy filesystem copy (kept as a fallback, harmless if API worked).
    cp "$JUNIT_PATH" "$ALLURE_RESULTS/junit.xml"
    # Реальная установленная версия платформы — извлекаем из пути
    # /opt/1cv8/x86_64/<ver>/1cv8. Так Allure-отчёт всегда показывает
    # фактическую версию, а не зашитую в скрипт (раньше тут было
    # hardcoded 8.3.23.1865, что путало после апгрейда на 8.3.24).
    PLATFORM_VER="$(basename "$(dirname "$PLATFORM")")"
    cat > "$ALLURE_RESULTS/environment.properties" << EOF
RunId=$RUN_ID
Mode=$MODE
Extension=$EXT_NAME
Platform=$PLATFORM_VER
YAxUnit=25.12
Timestamp=$(date -Iseconds)
EOF
    log "Allure results: $ALLURE_RESULTS"
    TESTS_NUM=$(grep -oP 'tests="\K[0-9]+' "$JUNIT_PATH" | head -1 || echo "?")
    log "OK pipeline complete: tests=$TESTS_NUM"
    exit 0
else
    log "ERROR: junit not produced"
    exit 2
fi