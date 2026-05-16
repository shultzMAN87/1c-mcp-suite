# =====================================================
# PostgresPro для 1С (ветка 1c-17)
# https://repo.postgrespro.ru/1c/1c-17/
# =====================================================
FROM debian:bookworm-slim

ARG PG_MAJOR=17
ENV PG_MAJOR=${PG_MAJOR} \
    PGDATA=/var/lib/postgresql/pgdata \
    LANG=ru_RU.UTF-8 \
    LC_ALL=ru_RU.UTF-8 \
    DEBIAN_FRONTEND=noninteractive

# 1. Базовые утилиты + локаль ru_RU.UTF-8 (необходима 1С)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
        gnupg \
        locales \
        tzdata \
        procps \
 && sed -i 's/^# *\(ru_RU.UTF-8\)/\1/' /etc/locale.gen \
 && locale-gen \
 && update-locale LANG=ru_RU.UTF-8 \
 && rm -rf /var/lib/apt/lists/*

# 2. Подключение репозитория PostgresPro 1c-17 и установка СУБД
RUN wget -O /tmp/pgpro-repo-add.sh https://repo.postgrespro.ru/1c/1c-${PG_MAJOR}/keys/pgpro-repo-add.sh \
 && chmod +x /tmp/pgpro-repo-add.sh \
 && /tmp/pgpro-repo-add.sh \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgrespro-1c-${PG_MAJOR} \
 && rm -f /tmp/pgpro-repo-add.sh \
 && rm -rf /var/lib/apt/lists/*

# 3. Каталог данных с правильными правами (postgres uid уже создан пакетом)
RUN mkdir -p ${PGDATA} \
 && chown -R postgres:postgres ${PGDATA} \
 && chmod 0700 ${PGDATA}

# 4. Entrypoint
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 5. Добавляем бинарники в PATH для удобства (pg_isready в healthcheck)
ENV PATH="/opt/pgpro/1c-${PG_MAJOR}/bin:${PATH}"

VOLUME ${PGDATA}
EXPOSE 5432

USER postgres
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["postgres"]
