#!/bin/bash
# =====================================================
# install-1c.sh
# Устанавливает дистрибутив 1С из /tmp/distr/
#
# Решает три главные проблемы типичного `apt-get install $(find *.deb)`:
#   1) Порядок: common должен ставиться ДО server/client/ws (иначе
#      неразрешённые зависимости и apt падает с exit 100).
#   2) Фильтрация по роли: если в одной папке оказались и серверный,
#      и клиентский дистрибутивы — они конфликтуют. Роль (server|client)
#      определяет, какие пакеты ставить, а какие явно пропустить.
#   3) Диагностика: при падении печатает полный список .deb, выбор
#      по ролям и причину apt-get, а не просто "exit 100".
#
# Использование:
#   install-1c.sh server   — только common + server (+ nls) пакеты
#   install-1c.sh client   — только common + client (+ nls) пакеты
#   install-1c.sh any      — всё подряд (не рекомендуется)
# =====================================================
set -euo pipefail

DISTR_DIR="/tmp/distr"
ROLE="${1:-any}"

echo ">>> [install-1c] роль: ${ROLE}"
echo ">>> [install-1c] содержимое ${DISTR_DIR}:"
ls -la "${DISTR_DIR}" || true

# --- 1. Распаковываем все архивы ---
# Поддерживаем .tar.gz / .tgz (старые раздачи 1С) и .zip (новые раздачи 8.3.24+).
# Делаем в два прохода, потому что у новых раздач часто двухуровневая структура:
# zip содержит вложенный tar.gz, который уже содержит .deb. Первый проход
# вытягивает первый уровень, второй — то, что появилось после первого.
shopt -s nullglob
for pass in 1 2; do
    expanded_any=0
    for f in "${DISTR_DIR}"/*.tar.gz "${DISTR_DIR}"/*.tgz; do
        [ -f "$f" ] || continue
        echo ">>> [install-1c] распаковка $(basename "$f") (tar.gz)"
        tar -xzf "$f" -C "${DISTR_DIR}/" && rm -f "$f"
        expanded_any=1
    done
    for f in "${DISTR_DIR}"/*.zip; do
        [ -f "$f" ] || continue
        echo ">>> [install-1c] распаковка $(basename "$f") (zip)"
        # -o = overwrite без вопросов; unzip входит в стандартные образы Debian/Ubuntu
        unzip -o -q "$f" -d "${DISTR_DIR}/" && rm -f "$f"
        expanded_any=1
    done
    # Если на этом проходе ничего не распаковали — выходим: дальше уже только .deb
    [ "$expanded_any" = "0" ] && break
done
shopt -u nullglob

# --- 2. Собираем полный список .deb ---
mapfile -t ALL_DEBS < <(find "${DISTR_DIR}" -type f -name '*.deb' | sort)

if [ "${#ALL_DEBS[@]}" -eq 0 ]; then
    echo "ОШИБКА: в ${DISTR_DIR} не найдено .deb-файлов."
    echo "Положите в distr/ tar.gz-архив или .deb дистрибутива 1С и пересоберите образ."
    exit 1
fi

echo ""
echo ">>> [install-1c] найдено .deb (всего ${#ALL_DEBS[@]}):"
for d in "${ALL_DEBS[@]}"; do
    echo "    $(basename "$d")"
done

# --- 3. Классификация .deb по имени файла ---
# Форматы имён в дистрибутиве 1С для Linux:
#   1c-enterprise-8.3.23.1865-common_8.3.23-1865_amd64.deb
#   1c-enterprise-8.3.23.1865-server_8.3.23-1865_amd64.deb
#   1c-enterprise-8.3.23.1865-client_8.3.23-1865_amd64.deb
#   1c-enterprise-8.3.23.1865-thin-client_8.3.23-1865_amd64.deb
#   1c-enterprise-8.3.23.1865-ws_8.3.23-1865_amd64.deb
#   *-common-nls, *-server-nls, *-client-nls и т.д.
classify() {
    local bn="$1"
    # Проверки от более специфичных к общим
    if [[ "$bn" =~ common[_-]nls ]]; then echo "common-nls"; return; fi
    if [[ "$bn" =~ thin-?client[_-]nls ]]; then echo "thin-nls"; return; fi
    if [[ "$bn" =~ server[_-]nls ]]; then echo "server-nls"; return; fi
    if [[ "$bn" =~ client[_-]nls ]]; then echo "client-nls"; return; fi
    if [[ "$bn" =~ ws[_-]nls ]]; then echo "ws-nls"; return; fi
    if [[ "$bn" =~ common ]]; then echo "common"; return; fi
    if [[ "$bn" =~ thin-?client ]]; then echo "thin"; return; fi
    if [[ "$bn" =~ server ]]; then echo "server"; return; fi
    if [[ "$bn" =~ client ]]; then echo "client"; return; fi
    if [[ "$bn" =~ ws ]]; then echo "ws"; return; fi
    echo "unknown"
}

# --- 4. Выбираем пакеты по роли ---
# Классы в порядке установки: common первым, nls-пакеты последними
case "${ROLE}" in
    server)
        # Серверный набор: common + server + ws + локализации.
        # thin-client НЕ ставим — он конфликтует с common (Conflicts в control-файле).
        WANT_CLASSES=(common server ws common-nls server-nls ws-nls)
        ;;
    client)
        # Клиентский набор: ТОЛЬКО толстый клиент (common + client).
        # thin-client НЕ берём по двум причинам:
        #   1) пакет thin-client объявляет Conflicts: common — нельзя вместе;
        #   2) thin-client-nls перезаписывает файлы client-nls (один и тот же
        #      /opt/1cv8/x86_64/<ver>/1cv8c_ar.res и другие .res-файлы).
        # Для задач автоматизации тестов нужен именно толстый клиент.
        WANT_CLASSES=(common client common-nls client-nls)
        ;;
    thin)
        # Отдельная роль для чистого тонкого клиента (standalone, без common).
        WANT_CLASSES=(thin thin-nls)
        ;;
    any|*)
        # "Всё подряд" — только диагностический режим, на практике даст
        # конфликты thin vs common. Оставлено для отладки.
        WANT_CLASSES=(common server client ws common-nls server-nls client-nls ws-nls)
        ;;
esac

echo ""
echo ">>> [install-1c] классы пакетов для роли '${ROLE}': ${WANT_CLASSES[*]}"

ORDERED=()
for want in "${WANT_CLASSES[@]}"; do
    for d in "${ALL_DEBS[@]}"; do
        bn="$(basename "$d")"
        cls="$(classify "$bn")"
        [ "$cls" = "$want" ] && ORDERED+=("$d")
    done
done

# Считаем пропущенные пакеты — понятный лог того, что отфильтровалось
SKIPPED=()
for d in "${ALL_DEBS[@]}"; do
    bn="$(basename "$d")"
    cls="$(classify "$bn")"
    hit=0
    for want in "${WANT_CLASSES[@]}"; do
        [ "$cls" = "$want" ] && { hit=1; break; }
    done
    [ $hit -eq 0 ] && SKIPPED+=("${bn} (класс: ${cls})")
done

if [ "${#SKIPPED[@]}" -gt 0 ]; then
    echo ""
    echo ">>> [install-1c] ПРОПУЩЕНО (не подходит для роли '${ROLE}'):"
    for s in "${SKIPPED[@]}"; do
        echo "    - $s"
    done
fi

if [ "${#ORDERED[@]}" -eq 0 ]; then
    echo ""
    echo "ОШИБКА: для роли '${ROLE}' не найдено ни одного подходящего .deb."
    echo "Убедитесь, что в distr/ лежит дистрибутив нужного типа:"
    echo "  - server: файлы *-common*.deb и *-server*.deb"
    echo "  - client: файлы *-common*.deb и *-client*.deb (или *-thin-client*.deb)"
    exit 1
fi

echo ""
echo ">>> [install-1c] порядок установки (${#ORDERED[@]} пакетов):"
for d in "${ORDERED[@]}"; do
    echo "    -> $(basename "$d")"
done
echo ""

# --- 5. Установка: dpkg -i в правильном порядке, apt-get -f добирает зависимости ---
export DEBIAN_FRONTEND=noninteractive
apt-get update

set +e
dpkg -i "${ORDERED[@]}"
DPKG_RC=$?
set -e

if [ $DPKG_RC -ne 0 ]; then
    echo ""
    echo ">>> [install-1c] dpkg завершился с кодом $DPKG_RC,"
    echo ">>> добираем зависимости через apt-get install -f"
    apt-get install -y --no-install-recommends -f
    # После -f install повторяем dpkg -i для финальной настройки
    dpkg -i "${ORDERED[@]}"
fi

echo ""
echo ">>> [install-1c] установка завершена"

# --- 6. Sanity-check ---
if ! ls -d /opt/1cv8/x86_64/8.3.* >/dev/null 2>&1; then
    echo "ОШИБКА: после установки /opt/1cv8/x86_64/8.3.* не найден."
    echo "Вероятно, .deb-файлы в distr/ — не от платформы 1С, либо роль выбрана неверно."
    exit 1
fi

echo ">>> [install-1c] установленная версия:"
ls -d /opt/1cv8/x86_64/8.3.*
