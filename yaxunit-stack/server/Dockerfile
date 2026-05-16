# =====================================================
# Сервер 1С:Предприятие (ragent)
#
# ВХОДНЫЕ АРТЕФАКТЫ:
#   ./distr/  — сюда положить дистрибутив сервера 1С для Linux x86_64,
#               скачанный из личного кабинета https://releases.1c.ru
#               Ожидается ОДИН из вариантов:
#                 а) архив tar.gz — например `server64_8_3_23_1865.tar.gz`
#                    или `deb64_8_3_23_1865.tar.gz`, внутри набор *.deb
#                 б) уже распакованные .deb файлы
# =====================================================
FROM ubuntu:22.04

ARG ONEC_VERSION=8.3.23.1865
ENV ONEC_VERSION=${ONEC_VERSION} \
    DEBIAN_FRONTEND=noninteractive \
    LANG=ru_RU.UTF-8 \
    LC_ALL=ru_RU.UTF-8

# 1. Системные зависимости для работы сервера 1С + локаль ru_RU
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        locales \
        tzdata \
        wget \
        tar \
        unzip \
        psmisc \
        procps \
        lsb-release \
        dbus \
        unixodbc \
        hostname \
        libgsf-1-114 \
        libglib2.0-0 \
        libxml2 \
        libmagickwand-6.q16-6 \
        libfontconfig1 \
        libfreetype6 \
        libwebkit2gtk-4.0-37 \
        libenchant-2-2 \
        imagemagick \
        fonts-liberation \
        xfonts-utils \
        dmidecode \
        lshw \
        udev \
        iproute2 \
 && sed -i 's/^# *\(ru_RU.UTF-8\)/\1/' /etc/locale.gen \
 && locale-gen \
 && update-locale LANG=ru_RU.UTF-8 \
 && rm -rf /var/lib/apt/lists/*

# 2. Копируем дистрибутив 1С и устанавливаем его
# Принимаем оба варианта: tar.gz-архив или набор .deb
COPY ./install-1c.sh /usr/local/bin/install-1c.sh
COPY ./distr/ /tmp/distr/

RUN chmod +x /usr/local/bin/install-1c.sh \
 && /usr/local/bin/install-1c.sh server \
 && rm -rf /tmp/distr /var/lib/apt/lists/*

# 3. Создаём симлинк current -> 8.3.X (удобно в конфигах Apache и скриптах)
RUN set -eux; \
    cd /opt/1cv8/x86_64; \
    latest="$(ls -d 8.3.* 2>/dev/null | sort -V | tail -n1)"; \
    [ -n "$latest" ] || { echo "ОШИБКА: /opt/1cv8/x86_64/8.3.* не найден после установки"; exit 1; }; \
    ln -sfn "$latest" current

# 3.5. Отключаем поиск аппаратных ключей защиты (HASP).
#      Критично для получения программной лицензии внутри Docker:
#      без этого "сбор информации о компьютере" висит минутами
#      на переборе сетевых HASP-ключей и падает по таймауту с
#      "Ошибка обращения к Центру Лицензирования" (LicenseBaseUI.cpp:1683).
RUN mkdir -p /opt/1cv8/conf \
 && echo "UseHwLicenses=0" >> /opt/1cv8/conf/conf.cfg

# 3.6. Подмена dmidecode заглушкой, возвращающей статические данные.
#      В Docker Desktop на Windows/WSL2 нет /dev/mem и /sys/firmware/dmi,
#      реальный dmidecode падает с "/dev/mem: No such file", и 1С виснет
#      в "сборе информации о компьютере" при получении лицензии сервером.
#      Заглушка отдаёт стабильные выдуманные значения → ragent получает
#      "отпечаток компьютера" и привязывает к нему community-лицензию.
#      ВАЖНО: значения в dmidecode-stub после получения лицензии менять
#      нельзя — иначе 1С воспримет это как "ключевые параметры изменились"
#      и потребует получить лицензию заново.
COPY dmidecode-stub /usr/local/sbin/dmidecode
RUN chmod +x /usr/local/sbin/dmidecode \
 && mv /usr/sbin/dmidecode /usr/sbin/dmidecode.real \
 && ln -sf /usr/local/sbin/dmidecode /usr/sbin/dmidecode

# 3.7. Явный nethasp.ini, запрещающий поиск сетевых HASP-ключей.
#      Без него HASP-библиотека пытается резолвить имена серверов
#      License Manager по TCP/IP/IPX/NetBIOS, получает таймауты DNS
#      и ragent уходит в длительный retry-цикл при каждом обращении
#      пользователя к серверной базе. UseHwLicenses=0 из conf.cfg
#      это сам по себе не всегда отключает.
#      Файл должен быть читаем пользователем usr1cv8 — иначе будет
#      проигнорирован без ошибок.
COPY nethasp.ini /opt/1cv8/conf/nethasp.ini
RUN chmod 644 /opt/1cv8/conf/nethasp.ini /opt/1cv8/conf/conf.cfg

# 4. Каталог данных сервера (пакет 1С уже создаёт usr1cv8:grp1cv8)
RUN mkdir -p /home/usr1cv8/.1cv8/1C/1cv8/conf \
 && chown -R usr1cv8:grp1cv8 /home/usr1cv8

# 4.1. Общесистемный каталог программных лицензий 1С.
#      Создаётся с правами usr1cv8:grp1cv8 775 (setgid на директории),
#      чтобы:
#        - ragent от usr1cv8 мог писать в него при получении лицензии,
#        - клиент (пользователь onec, входит в grp1cv8) мог читать,
#        - новые файлы автоматически наследовали группу grp1cv8.
#      В runtime этот каталог перекрывается bind mount'ом
#      ./licenses из docker-compose.yml — содержимое каталога
#      становится папкой проекта на хосте. Но права на точку
#      монтирования, созданную здесь, сохраняются и определяют,
#      с какими правами 1С сможет создавать файлы внутри.
RUN mkdir -p /var/1C/licenses \
 && chown -R usr1cv8:grp1cv8 /var/1C \
 && chmod -R g+ws /var/1C/licenses

USER usr1cv8
WORKDIR /home/usr1cv8

EXPOSE 1540 1541 1560-1591

# Запускаем агент сервера в foreground (без -daemon — это PID 1 в контейнере)
CMD ["/opt/1cv8/x86_64/current/ragent", \
     "-port", "1540", \
     "-regport", "1541", \
     "-range", "1560:1591"]
