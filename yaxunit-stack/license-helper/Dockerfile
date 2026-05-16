# =====================================================
# license-helper — сервисный контейнер для РАЗОВОГО
# получения программной серверной лицензии 1С:Предприятия.
#
# ЗАЧЕМ ЭТО НУЖНО:
#   В 1С 8.3.23 у серверного процесса ragent нет CLI-способа
#   получить программную лицензию через developer.1c.ru.
#   Единственный штатный путь — GUI-диалог клиента 1С, который
#   при привязке использует "отпечаток компьютера" того контейнера,
#   в котором он запущен (hostname + MAC + dmidecode).
#
#   Чтобы полученная лицензия считалась СЕРВЕРНОЙ (т.е. оказалась
#   привязана к fingerprint'у контейнера onec-server), GUI-клиент
#   должен быть запущен внутри контейнера, где:
#     - hostname = onec-server,
#     - MAC      = 02:42:ac:11:00:77,
#     - dmidecode отдаёт те же значения, что и в основном сервере,
#     - пользователь = usr1cv8 (тогда .lic файл ляжет в тот же
#       каталог ~/.1cv8/1C/1cv8/conf, который смонтирован в volume
#       `srvinfo` основного сервиса `app`).
#
# КАК ИСПОЛЬЗУЕТСЯ (кратко — подробности в README):
#   1. docker compose stop app
#   2. docker compose --profile license up -d license-helper
#   3. браузером в http://localhost:6080 → 1cestart → диалог
#      лицензирования → developer.1c.ru → лицензия падает в
#      /home/usr1cv8/.1cv8/1C/1cv8/conf/*.lic (том srvinfo)
#   4. docker compose --profile license stop license-helper
#   5. docker compose start app
#
# ВАЖНО:
#   - app должен быть остановлен, иначе конфликт hostname/MAC
#     в сети onec-net.
#   - Образ наследуется от серверного (onec-server-base) → в нём
#     уже есть dmidecode-stub, nethasp.ini, UseHwLicenses=0,
#     пользователь usr1cv8 и установленный серверный common.
#     Мы доставляем поверх ТОЛЬКО графический стек и клиентский
#     .deb (толстый клиент), не трогая остальное.
# =====================================================
ARG ONEC_IMAGE=onec-server-base
FROM ${ONEC_IMAGE}

USER root

ARG ONEC_VERSION=8.3.23.1865
ENV ONEC_VERSION=${ONEC_VERSION} \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:0

# 1. Графический стек + VNC/noVNC.
#    Набор тот же, что в client-vnc/Dockerfile, чтобы gui-клиент
#    вёл себя идентично (те же шрифты, тот же WM, те же libgtk).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        dbus-x11 \
        xvfb \
        tigervnc-standalone-server \
        tigervnc-common \
        tigervnc-tools \
        fluxbox \
        xterm \
        x11-xserver-utils \
        novnc \
        websockify \
        libgl1-mesa-dri \
        libgl1 \
        libgtk-3-0 \
        libenchant-2-2 \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/lib/x86_64-linux-gnu/libenchant-2.so.2 /usr/lib/x86_64-linux-gnu/libenchant.so.1

# 2. Устанавливаем КЛИЕНТСКИЙ дистрибутив 1С поверх уже стоящего
#    серверного common. Дистрибутив и install-1c.sh берём из
#    контекста client-vnc (см. additional_contexts в compose).
#
#    install-1c.sh в роли "client" увидит, что common уже стоит,
#    и корректно добавит client + client-nls поверх — общая
#    часть дистрибутива уже присутствует от серверного образа.
COPY --from=client-vnc-ctx install-1c.sh /usr/local/bin/install-1c.sh
COPY --from=client-vnc-ctx distr/ /tmp/distr/

RUN chmod +x /usr/local/bin/install-1c.sh \
 && /usr/local/bin/install-1c.sh client \
 && rm -rf /tmp/distr /var/lib/apt/lists/*

# 3. noVNC: страница по умолчанию
RUN ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html

# 4. Готовим ~/.vnc для пользователя usr1cv8.
#    КРИТИЧНО: весь VNC-стек (и, соответственно, процесс 1cv8)
#    должен работать от usr1cv8 — это тот же пользователь, от
#    которого ragent запускается в сервисе app. Только тогда
#    полученный .lic файл ляжет в /home/usr1cv8/.1cv8/1C/1cv8/conf/
#    (это volume srvinfo) и его увидит ragent после старта app.
RUN mkdir -p /home/usr1cv8/.vnc \
 && chown -R usr1cv8:grp1cv8 /home/usr1cv8/.vnc

# 5. Entrypoint и xstartup
COPY xstartup /home/usr1cv8/.vnc/xstartup
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /home/usr1cv8/.vnc/xstartup /usr/local/bin/docker-entrypoint.sh \
 && chown usr1cv8:grp1cv8 /home/usr1cv8/.vnc/xstartup

USER usr1cv8
WORKDIR /home/usr1cv8

EXPOSE 5900 6080

ENTRYPOINT ["docker-entrypoint.sh"]
