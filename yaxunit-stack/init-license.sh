#!/bin/bash
# init-license.sh — переносит файл лицензии 1С из корня проекта в
# каталог ./licenses/ (где его подхватит docker compose через bind mount).
#
# Использование (один раз перед первым `docker compose up`):
#   ./init-license.sh                  # ищет ./*.lic в корне
#   ./init-license.sh /path/to/foo.lic # явный путь
#
# Если в ./licenses/ уже лежит license-backup.lic — спросит, перезаписать ли.
set -euo pipefail

DEST_DIR="$(cd "$(dirname "$0")" && pwd)/licenses"
DEST="$DEST_DIR/license-backup.lic"

if [ $# -ge 1 ]; then
    SRC="$1"
else
    # Найти первый .lic в корне (но не в licenses/)
    SRC="$(find "$(dirname "$0")" -maxdepth 1 -name '*.lic' -type f | head -1 || true)"
fi

if [ -z "${SRC:-}" ] || [ ! -f "$SRC" ]; then
    echo "ОШИБКА: .lic файл не найден."
    echo "Положите файл лицензии в корень проекта или укажите путь:"
    echo "    ./init-license.sh /path/to/license.lic"
    exit 1
fi

mkdir -p "$DEST_DIR"

if [ -f "$DEST" ]; then
    read -r -p "Файл $DEST уже существует. Перезаписать? [y/N] " ans
    case "$ans" in [yY]|[yY][eE][sS]) ;; *) echo "Отменено."; exit 0 ;; esac
fi

install -m 600 "$SRC" "$DEST"
echo "OK: $SRC -> $DEST"

# Если источник лежит в корне проекта — удалим, чтобы случайно не закоммитить
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC_ABS="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")"
if [ "$(dirname "$SRC_ABS")" = "$PROJECT_ROOT" ]; then
    rm -f "$SRC_ABS"
    echo "Исходник $SRC_ABS удалён из корня (приватные данные)."
fi
