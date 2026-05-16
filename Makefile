# Makefile для 1C MCP Suite.
#
# Основной способ запуска — docker compose, но часть повседневных операций
# удобнее собрать в короткие команды. На Windows использовать через WSL или
# напрямую запускать python3/py.

.PHONY: help check-prereqs up down build restart logs clean

help:
	@echo "1C MCP Suite — доступные команды:"
	@echo ""
	@echo "  make check-prereqs   Проверить готовность окружения к запуску"
	@echo "  make up              Поднять весь стек (docker compose up -d)"
	@echo "  make down            Остановить стек"
	@echo "  make build           Пересобрать образы"
	@echo "  make restart         Перезапустить (down + up)"
	@echo "  make logs            Следить за логами всех сервисов"
	@echo "  make clean           Остановить и удалить volume'ы (ОСТОРОЖНО: удалит данные)"
	@echo ""

check-prereqs:
	@python3 scripts/check_prereqs.py

up:
	docker compose up -d --build

down:
	docker compose down

build:
	docker compose build

restart:
	docker compose down
	docker compose up -d

logs:
	docker compose logs -f

clean:
	docker compose down -v
