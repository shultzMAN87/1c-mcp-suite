#!/bin/bash
# =====================================================
# Entrypoint license-helper
#
# Разовый контейнер для получения серверной программной лицензии
# через GUI-диалог 1С под пользователем usr1cv8 (тот же, что и ragent
# в основном сервисе app).
#
# Запускает тот же VNC/noVNC стек, что и onec-client, но в HOME
# пользователя usr1cv8 — чтобы полученный .lic файл сохранился в
# /home/usr1cv8/.1cv8/1C/1cv8/conf/, т.е. внутри volume srvinfo.
# =====================================================
set -e

: "${VNC_GEOMETRY:=1280x800}"
: "${VNC_DEPTH:=24}"
: "${VNC_DISPLAY:=:0}"
VNC_PORT=5900
NOVNC_PORT=6080

# HOME должен быть именно /home/usr1cv8 — от этого зависит,
# где 1С сохранит полученную программную лицензию.
export HOME=/home/usr1cv8

echo ">>> [license-helper] Запуск VNC-сессии ${VNC_DISPLAY} (${VNC_GEOMETRY}x${VNC_DEPTH})"
echo ">>> [license-helper] HOME=${HOME}  USER=$(id -un)  UID=$(id -u)"

# Чистим возможные stale-файлы от предыдущего запуска.
# Том srvinfo смонтирован в /home/usr1cv8/.1cv8 — НЕ трогаем его,
# а вот /home/usr1cv8/.vnc/*.pid могут быть от прошлой попытки.
rm -rf /tmp/.X* /tmp/.X11-unix 2>/dev/null || true
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix 2>/dev/null || true

rm -f "${HOME}/.vnc/"*.pid "${HOME}/.vnc/"*.log 2>/dev/null || true

# --- TigerVNC ---
if [ -n "${VNC_PASSWORD:-}" ]; then
    echo ">>> [license-helper] VNC с паролем"
    echo "${VNC_PASSWORD}" | vncpasswd -f > "${HOME}/.vnc/passwd"
    chmod 600 "${HOME}/.vnc/passwd"
    tigervncserver "${VNC_DISPLAY}" \
        -geometry "${VNC_GEOMETRY}" \
        -depth "${VNC_DEPTH}" \
        -localhost no \
        -rfbauth "${HOME}/.vnc/passwd" \
        -xstartup "${HOME}/.vnc/xstartup"
else
    echo ">>> [license-helper] VNC БЕЗ ПАРОЛЯ (только для локальной разработки!)"
    tigervncserver "${VNC_DISPLAY}" \
        -geometry "${VNC_GEOMETRY}" \
        -depth "${VNC_DEPTH}" \
        -localhost no \
        -SecurityTypes None \
        --I-KNOW-THIS-IS-INSECURE \
        -xstartup "${HOME}/.vnc/xstartup"
fi

# Ждём поднятия 5900
for i in 1 2 3 4 5 6 7 8 9 10; do
    if ss -tln 2>/dev/null | grep -q ":${VNC_PORT}" \
       || netstat -tln 2>/dev/null | grep -q ":${VNC_PORT}"; then
        break
    fi
    sleep 0.3
done

# --- Гарантия WM и терминала внутри сессии ---
export DISPLAY="${VNC_DISPLAY}"
sleep 0.5

if ! pgrep -u "$(id -u)" -x fluxbox >/dev/null 2>&1; then
    echo ">>> [license-helper] fluxbox не запущен wrapper'ом — стартуем вручную"
    xsetroot -solid '#2e3440' 2>/dev/null || true
    fluxbox >/tmp/fluxbox.log 2>&1 &
    sleep 0.5
fi

if ! pgrep -u "$(id -u)" -x xterm >/dev/null 2>&1; then
    echo ">>> [license-helper] Запуск xterm"
    xterm -geometry 120x30+50+50 \
          -fa 'Monospace' -fs 11 \
          -bg black -fg white \
          -title 'license-helper (usr1cv8)' \
          >/tmp/xterm.log 2>&1 &
fi

# --- noVNC ---
echo ">>> [license-helper] Запуск noVNC на порту ${NOVNC_PORT}"
websockify --web /usr/share/novnc "${NOVNC_PORT}" "localhost:${VNC_PORT}" &
NOVNC_PID=$!

echo ""
echo "============================================================"
echo "  LICENSE-HELPER готов."
echo ""
echo "  noVNC:  http://localhost:${NOVNC_PORT}/"
echo "  VNC:    vnc://localhost:${VNC_PORT}"
echo ""
echo "  Внутри VNC в xterm запустите:"
echo "      /opt/1cv8/x86_64/current/1cestart"
echo ""
echo "  В открывшемся окне 'Запуск 1С:Предприятия' нажмите"
echo "  'Добавить' → 'Создание новой информационной базы' →"
echo "  далее по мастеру. При создании базы платформа спросит"
echo "  лицензию — введите данные developer.1c.ru."
echo ""
echo "  Полученная лицензия сохранится в:"
echo "      /home/usr1cv8/.1cv8/1C/1cv8/conf/*.lic"
echo "  Это том srvinfo — ragent увидит её после запуска app."
echo ""
echo "  После получения лицензии:"
echo "      docker compose --profile license stop license-helper"
echo "      docker compose start app"
echo "============================================================"
echo ""

trap "echo '>>> [license-helper] Shutdown'; kill ${NOVNC_PID} 2>/dev/null; tigervncserver -kill ${VNC_DISPLAY} 2>/dev/null; exit 0" TERM INT

wait ${NOVNC_PID}
