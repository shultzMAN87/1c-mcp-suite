#!/bin/bash
# =====================================================
# Entrypoint контейнера с толстым клиентом 1С + MCP сервером
#
# Запускает:
#   - TigerVNC-сервер на :0 (порт 5900)
#   - noVNC через websockify на порту 6080 (HTTP -> VNC)
#   - fluxbox как WM
#   - MCP HTTP сервер (FastAPI) на порту 8019
# =====================================================
set -e

: "${VNC_GEOMETRY:=1280x800}"
: "${VNC_DEPTH:=24}"
: "${VNC_DISPLAY:=:0}"
VNC_PORT=5900
NOVNC_PORT=6080
MCP_PORT=8019

echo ">>> [onec-client] Запуск VNC-сессии ${VNC_DISPLAY} (${VNC_GEOMETRY}x${VNC_DEPTH})"

# Чистим возможные stale-файлы
rm -rf /tmp/.X* /tmp/.X11-unix 2>/dev/null || true
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix 2>/dev/null || true
rm -f "${HOME}/.vnc/"*.pid "${HOME}/.vnc/"*.log 2>/dev/null || true

# --- Запуск TigerVNC ---
if [ -n "${VNC_PASSWORD}" ]; then
    echo ">>> [onec-client] Включён VNC с паролем"
    mkdir -p "${HOME}/.vnc"
    echo "${VNC_PASSWORD}" | vncpasswd -f > "${HOME}/.vnc/passwd"
    chmod 600 "${HOME}/.vnc/passwd"
    tigervncserver "${VNC_DISPLAY}" \
        -geometry "${VNC_GEOMETRY}" \
        -depth "${VNC_DEPTH}" \
        -localhost no \
        -rfbauth "${HOME}/.vnc/passwd" \
        -xstartup "${HOME}/.vnc/xstartup"
else
    echo ">>> [onec-client] VNC БЕЗ ПАРОЛЯ (только для локальной разработки!)"
    tigervncserver "${VNC_DISPLAY}" \
        -geometry "${VNC_GEOMETRY}" \
        -depth "${VNC_DEPTH}" \
        -localhost no \
        -SecurityTypes None \
        --I-KNOW-THIS-IS-INSECURE \
        -xstartup "${HOME}/.vnc/xstartup"
fi

# Подождём VNC
for i in 1 2 3 4 5 6 7 8 9 10; do
    if ss -tln 2>/dev/null | grep -q ":${VNC_PORT}" \
       || netstat -tln 2>/dev/null | grep -q ":${VNC_PORT}"; then
        break
    fi
    sleep 0.3
done

# --- Гарантируем fluxbox + xterm в VNC сессии ---
export DISPLAY="${VNC_DISPLAY}"
sleep 0.5

if ! pgrep -u "$(id -u)" -x fluxbox >/dev/null 2>&1; then
    echo ">>> [onec-client] fluxbox не запущен wrapper'ом — стартуем вручную"
    xsetroot -solid '#2e3440' 2>/dev/null || true
    fluxbox >/tmp/fluxbox.log 2>&1 &
    sleep 0.5
fi

if ! pgrep -u "$(id -u)" -x xterm >/dev/null 2>&1; then
    echo ">>> [onec-client] Запуск xterm"
    xterm -geometry 120x30+50+50 \
          -fa 'Monospace' -fs 11 \
          -bg black -fg white \
          -title '1C client terminal' \
          >/tmp/xterm.log 2>&1 &
fi

# --- Лицензия 1С: копируем в стандартный путь поиска платформы ---
# При batch-режиме DESIGNER ищет лицензию строго в:
#   ~/.1cv8/1C/1cv8/conf/   (для пользователя)
#   /opt/1cv8/x86_64/<ver>/conf/  (общесистемно)
# Volume /var/1C/licenses/ платформа сама не знает.
#
# `install -m 600` (вместо `cp -f`) — единообразно с entrypoint у app:
# выставляет правильные права при первом копировании, а при повторных
# запусках идемпотентно перезаписывает.
LICENSE_DIR="${HOME}/.1cv8/1C/1cv8/conf"
echo ">>> [onec-client] Установка лицензии в ${LICENSE_DIR}"
mkdir -p "${LICENSE_DIR}"
if compgen -G "/var/1C/licenses/*.lic" > /dev/null; then
    for f in /var/1C/licenses/*.lic; do
        install -m 600 "$f" "${LICENSE_DIR}/$(basename "$f")" 2>/dev/null || \
            echo ">>> [onec-client] license already RO-mounted, skip stage"
    done
    echo ">>> [onec-client] Лицензия скопирована"
else
    echo ">>> [onec-client] WARN: лицензии в /var/1C/licenses/ не найдены"
fi

# --- noVNC (веб-интерфейс на 6080) ---
echo ">>> [onec-client] Запуск noVNC на порту ${NOVNC_PORT}"
websockify --web /usr/share/novnc "${NOVNC_PORT}" "localhost:${VNC_PORT}" &
NOVNC_PID=$!

# --- MCP HTTP сервер на 8019 ---
echo ">>> [onec-client] Подготовка /tmp/runs"
mkdir -p /tmp/runs

echo ">>> [onec-client] Запуск MCP сервера на порту ${MCP_PORT}"
# КРИТИЧНО: явно передаём HOME/USER/DISPLAY, иначе MCP-процесс
# (запущенный через &) их теряет, и DESIGNER не находит лицензию.
HOME="${HOME}" USER="$(id -un)" DISPLAY="${VNC_DISPLAY}" \
    python3 /app/mcp_server.py >/tmp/mcp.log 2>&1 &
MCP_PID=$!
echo ">>> [onec-client] MCP запущен (PID=${MCP_PID}), API: http://localhost:${MCP_PORT}"

echo ""
echo "===================================================="
echo "  VNC:    vnc://localhost:${VNC_PORT}"
echo "  noVNC:  http://localhost:${NOVNC_PORT}/"
echo "  MCP:    http://localhost:${MCP_PORT}/health"
echo "===================================================="
echo ""

# Корректное завершение по сигналам
trap "echo '>>> [onec-client] Shutdown'; kill ${MCP_PID} ${NOVNC_PID} 2>/dev/null; tigervncserver -kill ${VNC_DISPLAY} 2>/dev/null; exit 0" TERM INT

# Ждём завершения noVNC (или сигнала)
wait ${NOVNC_PID}
