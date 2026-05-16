# =====================================================
# Толстый клиент 1С:Предприятие + MCP HTTP сервер
# =====================================================
ARG ONEC_IMAGE=onec-server-base
FROM ${ONEC_IMAGE}

USER root

ARG ONEC_VERSION=8.3.23.1865
ENV ONEC_VERSION=${ONEC_VERSION} \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:0

# 1. Графический стек, VNC/noVNC + утилиты + Python + curl/zip
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        curl \
        zip \
        unzip \
        postgresql-client \
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
        iproute2 \
        procps \
        libgtk-3-0 \
        libenchant-2-2 \
        udev \
        dmidecode \
        lshw \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/lib/x86_64-linux-gnu/libenchant-2.so.2 /usr/lib/x86_64-linux-gnu/libenchant.so.1

# 2. Устанавливаем клиентский дистрибутив 1С
COPY ./install-1c.sh /usr/local/bin/install-1c.sh
COPY ./distr/ /tmp/distr/

RUN chmod +x /usr/local/bin/install-1c.sh \
 && /usr/local/bin/install-1c.sh client \
 && rm -rf /tmp/distr /var/lib/apt/lists/*

# 3. Симлинк current -> 8.3.X
RUN set -eux; \
    cd /opt/1cv8/x86_64; \
    latest="$(ls -d 8.3.* 2>/dev/null | sort -V | tail -n1)"; \
    [ -n "$latest" ] || { echo "ОШИБКА: /opt/1cv8/x86_64/8.3.* не найден"; exit 1; }; \
    ln -sfn "$latest" current

# 4. Отключаем поиск аппаратных ключей защиты (HASP).
# DisableUnsafeActionProtection=.* раньше тут был — на платформе 8.3.23
# флаг безопасного режима расширений через conf.cfg НЕ снимается;
# реально это решается через `ibcmd extension update --safe-mode=no`
# на шаге 7.5 пайплайна.
RUN mkdir -p /opt/1cv8/conf \
 && echo "UseHwLicenses=0" >> /opt/1cv8/conf/conf.cfg


# 4.5. Подмена dmidecode заглушкой
COPY dmidecode-stub /usr/local/sbin/dmidecode
RUN chmod +x /usr/local/sbin/dmidecode \
 && mv /usr/sbin/dmidecode /usr/sbin/dmidecode.real \
 && ln -sf /usr/local/sbin/dmidecode /usr/sbin/dmidecode

# 4.6. nethasp.ini
COPY nethasp.ini /opt/1cv8/conf/nethasp.ini

# 5. noVNC: vnc.html по умолчанию
RUN ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html

# 6. Непривилегированный пользователь onec
RUN useradd -m -s /bin/bash -G grp1cv8 onec \
#  && mkdir -p /home/onec/.vnc \
 && mkdir -p /home/onec/.vnc /home/onec/.1cv8/1C/1cv8/conf \
 && chown -R onec:onec /home/onec

# 7. Стартовый скрипт VNC и entrypoint
COPY xstartup /home/onec/.vnc/xstartup
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /home/onec/.vnc/xstartup /usr/local/bin/docker-entrypoint.sh \
 && chown onec:onec /home/onec/.vnc/xstartup \
 && sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh

# === 8. MCP HTTP сервер ===
RUN pip3 install --no-cache-dir --upgrade pip && \
    pip3 install --no-cache-dir \
        "fastapi==0.115.*" \
        "uvicorn[standard]==0.32.*" \
        "python-multipart>=0.0.9,<1.0"

# /reports получает 1777 (как /tmp), чтобы пользователь onec мог туда писать
# даже когда сюда монтируется volume извне с root-владельцем
RUN mkdir -p /app/scripts /reports/allure-results && \
    chmod -R 1777 /reports

COPY mcp_server.py /app/mcp_server.py
COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*.sh && \
    sed -i 's/\r$//' /app/scripts/*.sh && \
    chown -R onec:onec /app

# Скачивание YAxUnit
RUN /app/scripts/install_yaxunit.sh

USER onec
WORKDIR /home/onec

EXPOSE 5900 6080 8019

ENTRYPOINT ["docker-entrypoint.sh"]
