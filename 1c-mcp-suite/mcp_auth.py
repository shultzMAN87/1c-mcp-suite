"""
Shared-secret аутентификация для MCP SSE-серверов (задача 3.2).

Предыстория: FastMCP по умолчанию поднимает SSE-приложение без какой-либо
аутентификации — любой процесс в docker-network может вызвать любой tool
(`sonar_scan_code`, `http_service_call`, `platform_help_*` и т.д.).
Для dev-кластера этого хватало, но после индексации всей справки платформы
(4.7) и появления tools, потенциально ходящих в живую 1С, это стало
реальной дырой.

Решение: простой pre-shared secret через HTTP-заголовок. Ни OAuth, ни JWT —
overkill для внутренней docker-network. Одного секрета, загружаемого из
env `MCP_SHARED_SECRET`, достаточно, чтобы отсечь всё, кроме явно
сконфигурированных клиентов.

Режимы по умолчанию (согласовано):
- env не задан          → middleware не навешивается, в лог идёт warning.
                          Это сохраняет обратную совместимость для локальной
                          разработки — `docker compose up` работает без
                          .env-правок.
- env задан (непустой)  → middleware активна, SSE-эндпоинты требуют
                          совпадающий заголовок, иначе 401.

Протокол:
- Заголовок `Authorization: Bearer <secret>` (основной; совпадает с тем,
  как opencode ожидает видеть его в `mcp-config.json → headers`).
- Заголовок `X-MCP-Secret: <secret>` (альтернативный; удобен для curl и
  простых скриптов, где Bearer вводит в заблуждение про OAuth).
- Любой из двух подходит; приоритет у `Authorization`, если заданы оба.

Что middleware пропускает без проверки:
- `GET /` — FastMCP иногда туда кладёт health-пинг, и keepalive от
  docker-compose должен работать без секрета.
- `GET /health` — для docker healthcheck, если когда-нибудь появится.
- `OPTIONS *` — CORS preflight, в заголовках секрета быть не может
  по определению.

Что проверяется: всё остальное, включая `/sse` и `/messages/*`.

Клиентская часть: `build_client_headers()` отдаёт dict пригодный для
прямой передачи в `sse_client(url, headers=...)` — MCP >=1.0.0
поддерживает этот параметр нативно.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from typing import Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ENV_VAR = "MCP_SHARED_SECRET"
HEADER_AUTHORIZATION = "authorization"  # Starlette lowercases headers
HEADER_X_SECRET = "x-mcp-secret"
BEARER_PREFIX = "bearer "

# Пути, которые middleware НЕ проверяет. /sse и /messages/* — оба
# под защитой. /messages/ в FastMCP SSE-транспорте приходит как
# POST с query-string ?session_id=..., секрет для него нужен,
# иначе атакующий, угадав session_id, может послать произвольный
# JSON-RPC.
PUBLIC_PATHS = frozenset({"/", "/health"})


# ─── Серверная сторона ───────────────────────────────────────────────────


class SharedSecretMiddleware:
    """
    ASGI-middleware: проверяет заголовок с общим секретом на каждом
    HTTP-запросе к MCP-серверу. Работает поверх SSE-приложения FastMCP
    (которое внутри Starlette).

    Параметры:
        app:       обёртываемое ASGI-приложение (обычно `mcp.sse_app()`).
        secret:    pre-shared secret. Пустая строка/None = middleware
                   не навешивается вообще (см. `wrap_sse_app`).
        server_name: имя сервера для логов — чтобы в объединённом stdout
                   было видно, кто вернул 401.
    """

    def __init__(self, app: ASGIApp, secret: str, server_name: str = "mcp") -> None:
        if not secret:
            # Защита от случайного прямого инстанцирования без секрета —
            # публичный API ходит через wrap_sse_app(), и тот принимает
            # такое решение централизованно.
            raise ValueError(
                "SharedSecretMiddleware: secret обязателен. "
                "Если секрет не задан, просто не оборачивайте приложение."
            )
        self.app = app
        self.secret = secret
        self.server_name = server_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # WebSocket и lifespan — пропускаем. FastMCP SSE-транспорт
            # чистый HTTP + Server-Sent Events, WS не использует.
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        path = scope.get("path", "")

        if method == "OPTIONS" or path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        provided = _extract_secret(scope)
        if provided is None:
            await self._deny(send, reason="missing_credentials", path=path)
            return

        # hmac.compare_digest — защита от timing-side-channel. На длинах
        # 32-64 байта разница смешная, но привычка полезная и бесплатная.
        if not hmac.compare_digest(provided, self.secret):
            await self._deny(send, reason="invalid_credentials", path=path)
            return

        await self.app(scope, receive, send)

    async def _deny(self, send: Send, *, reason: str, path: str) -> None:
        # Короткий лог в stderr — не раскрываем ни секрет, ни IP
        # (Docker-network, всё равно только внутренние адреса).
        sys.stderr.write(
            f"[mcp-auth] {self.server_name}: 401 {reason} on {path}\n"
        )
        response = JSONResponse(
            {"error": "unauthorized", "reason": reason},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="mcp"'},
        )
        await response(
            {"type": "http", "method": "GET", "path": path, "headers": []},
            _empty_receive,
            send,
        )


async def _empty_receive() -> dict:
    # Stub для JSONResponse: он не читает body, но ASGI требует receive.
    return {"type": "http.disconnect"}


def _extract_secret(scope: Scope) -> str | None:
    """
    Достаёт секрет из заголовков ASGI scope. Возвращает None, если
    заголовка нет или формат невалидный.

    ASGI headers — список кортежей (name_bytes, value_bytes), имя всегда
    в нижнем регистре (по спеке).
    """
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers") or ()
    auth_value: bytes | None = None
    x_secret_value: bytes | None = None

    for name, value in headers:
        if name == HEADER_AUTHORIZATION.encode():
            auth_value = value
        elif name == HEADER_X_SECRET.encode():
            x_secret_value = value

    if auth_value is not None:
        try:
            decoded = auth_value.decode("latin-1")
        except UnicodeDecodeError:
            return None
        # Bearer-префикс case-insensitive по RFC 6750.
        if decoded.lower().startswith(BEARER_PREFIX):
            return decoded[len(BEARER_PREFIX):].strip() or None
        # Не Bearer — возможно, пользователь положил голый секрет.
        # Не поддерживаем это молча: атакующему проще угадать схему.
        return None

    if x_secret_value is not None:
        try:
            return x_secret_value.decode("latin-1").strip() or None
        except UnicodeDecodeError:
            return None

    return None


def wrap_sse_app(app: ASGIApp, server_name: str = "mcp") -> ASGIApp:
    """
    Основная публичная функция для серверной стороны.
    Вызывается в `start.py` и в `__main__` автономных серверов.

    Читает env `MCP_SHARED_SECRET`. Если пусто — возвращает app как есть
    и печатает warning. Если заполнено — оборачивает в middleware и
    печатает подтверждение со стартующим именем сервера.
    """
    secret = os.environ.get(ENV_VAR, "").strip()
    if not secret:
        sys.stderr.write(
            f"[mcp-auth] {server_name}: WARNING — {ENV_VAR} не задан, "
            f"SSE-эндпоинт открыт без аутентификации. "
            f"Для продакшена задайте секрет в .env "
            f"(openssl rand -hex 32).\n"
        )
        return app

    # Маскируем секрет в логе — достаточно показать длину, чтобы человек
    # мог сверить, что подгрузилось «что-то» правильной длины.
    sys.stderr.write(
        f"[mcp-auth] {server_name}: auth ENABLED (secret length={len(secret)})\n"
    )
    return SharedSecretMiddleware(app, secret=secret, server_name=server_name)


# ─── Клиентская сторона ──────────────────────────────────────────────────


def build_client_headers() -> dict[str, str]:
    """
    Хелпер для кода, который открывает SSE-соединения к нашим же
    MCP-серверам (оркестратор, watcher, code_reindex_trigger).

    Возвращает:
        {"Authorization": "Bearer <secret>"} если env задан,
        пустой dict иначе. В обоих случаях можно без условий
        передавать в `sse_client(url, headers=...)`.
    """
    secret = os.environ.get(ENV_VAR, "").strip()
    if not secret:
        return {}
    return {"Authorization": f"Bearer {secret}"}


# ─── CLI-самотест ────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Мини-тест: запустите `MCP_SHARED_SECRET=test python mcp_auth.py`
    # чтобы убедиться, что модуль импортируется и логика экстракции
    # работает. Не заменяет smoke_auth.py, но полезно в отладке.
    tests = [
        ([(b"authorization", b"Bearer abc")], "abc"),
        ([(b"authorization", b"bearer abc")], "abc"),  # lowercase ok
        ([(b"authorization", b"Basic abc")], None),
        ([(b"x-mcp-secret", b"xyz")], "xyz"),
        ([(b"authorization", b"Bearer xxx"), (b"x-mcp-secret", b"yyy")], "xxx"),
        ([], None),
        ([(b"authorization", b"Bearer   padded  ")], "padded"),
    ]
    failed = 0
    for headers, expected in tests:
        scope = {"type": "http", "headers": headers}
        got = _extract_secret(scope)
        status = "✓" if got == expected else "✗"
        if got != expected:
            failed += 1
        print(f"{status} headers={headers} expected={expected!r} got={got!r}")
    print(
        json.dumps(
            {"passed": len(tests) - failed, "failed": failed, "total": len(tests)},
            indent=2,
        )
    )
    sys.exit(1 if failed else 0)
