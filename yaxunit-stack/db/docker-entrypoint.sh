#!/bin/bash
# =====================================================
# Entrypoint для PostgresPro 1c-17
# - при первом запуске выполняет initdb с суперпользователем
#   ${POSTGRES_USER} и паролем ${POSTGRES_PASSWORD}
# - включает внешние подключения (listen_addresses = '*')
# - настраивает pg_hba.conf для подключений от сервера 1С
# =====================================================
set -e

PG_BINDIR="/opt/pgpro/1c-${PG_MAJOR}/bin"
: "${POSTGRES_USER:=postgres}"
: "${POSTGRES_PASSWORD:=postgres}"
: "${POSTGRES_DB:=postgres}"

# --- Первичная инициализация ---
if [ ! -s "${PGDATA}/PG_VERSION" ]; then
    echo ">>> [onec-postgres] Инициализация кластера в ${PGDATA}"

    # Пароль для суперпользователя передаём через pwfile (безопаснее, чем argv)
    PWFILE="$(mktemp)"
    printf '%s' "${POSTGRES_PASSWORD}" > "${PWFILE}"

    "${PG_BINDIR}/initdb" \
        --username="${POSTGRES_USER}" \
        --pwfile="${PWFILE}" \
        --auth-local=trust \
        --auth-host=md5 \
        --encoding=UTF8 \
        --locale=ru_RU.UTF-8 \
        --pgdata="${PGDATA}"

    rm -f "${PWFILE}"

    # Слушаем все интерфейсы — внутри docker-сети это безопасно
    {
        echo ""
        echo "# --- onec-docker overrides ---"
        echo "listen_addresses = '*'"
        echo "max_connections = 200"
        echo "shared_buffers = 256MB"
        echo "max_locks_per_transaction = 256"
        echo "standard_conforming_strings = off"
        echo "escape_string_warning = off"
    } >> "${PGDATA}/postgresql.conf"

    # Разрешаем подключения из docker-сетей
    {
        echo "# --- onec-docker overrides ---"
        echo "host all all 0.0.0.0/0 md5"
        echo "host all all ::/0      md5"
    } >> "${PGDATA}/pg_hba.conf"

    # Создаём БД, если отличается от postgres
    if [ "${POSTGRES_DB}" != "postgres" ]; then
        "${PG_BINDIR}/pg_ctl" -D "${PGDATA}" -o "-c listen_addresses=''" -w start
        "${PG_BINDIR}/psql" --username="${POSTGRES_USER}" -d postgres \
            -c "CREATE DATABASE \"${POSTGRES_DB}\";"
        "${PG_BINDIR}/pg_ctl" -D "${PGDATA}" -m fast -w stop
    fi

    echo ">>> [onec-postgres] Инициализация завершена"
fi

# --- Запуск ---
echo ">>> [onec-postgres] Запуск PostgresPro 1c-${PG_MAJOR}"
exec "${PG_BINDIR}/postgres" -D "${PGDATA}"
