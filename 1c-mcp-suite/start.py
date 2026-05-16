#!/usr/bin/env python3
"""
Точка входа для запуска MCP-серверов (v3).

ВАЖНО: этот скрипт рассчитан на запуск ВНУТРИ Docker-контейнера,
собранного через Dockerfile.python / Dockerfile.embeddings.
Контейнер копирует файлы вида mcp-metadata-graph/server.py в /app/mcp_metadata_graph.py
(с подчёркиваниями), что и делает их импортируемыми Python-модулями.

Локально, без Docker, импорт сломается — это by design. Используйте docker compose.
"""
import os
import sys
from pathlib import Path

import uvicorn

SERVERS = {
    "metadata-graph":   ("mcp_metadata_graph",  8001),
    "platform-help":    ("mcp_platform_help",   8003),
    "code-templates":   ("mcp_code_templates",  8008),
    "1c-naparnik":      ("mcp_1c_naparnik",     8007),
    "query-builder":    ("mcp_query_builder",   8009),
    "testing":          ("mcp_testing",         8010),
    "code-rag":         ("mcp_code_rag",        8011),
    "rest-proxy":       ("mcp_rest_proxy",      8013),
}


def _check_docker_environment() -> None:
    """Защита от запуска вне Docker — даём понятную ошибку вместо ImportError."""
    here = Path(__file__).resolve().parent
    if str(here) != "/app":
        sys.stderr.write(
            "ОШИБКА: start.py рассчитан на запуск ВНУТРИ Docker-контейнера.\n"
            "Файлы серверов копируются в /app с переименованием через Dockerfile,\n"
            "и без этого импорт по имени модуля невозможен.\n\n"
            "Используйте: docker compose up <service>\n"
        )
        sys.exit(2)


def _wrap_tools_with_metrics(mcp_obj, server_name: str) -> None:
    """Оборачивает все tools декоратором track из mcp_metrics."""
    os.environ.setdefault("MCP_SERVER_NAME", server_name)
    try:
        from mcp_metrics import track
    except Exception as e:
        sys.stderr.write(f"[metrics] mcp_metrics недоступен: {e}\n")
        return

    try:
        tools = getattr(mcp_obj._tool_manager, "_tools", {})
        wrapped = 0
        for tool in tools.values():
            if getattr(tool.fn, "__wrapped_by_track__", False):
                continue
            tool.fn = track(tool.fn)
            try:
                tool.fn.__wrapped_by_track__ = True
            except (AttributeError, TypeError):
                pass
            wrapped += 1
        print(f"[metrics] {server_name}: обёрнуто инструментов: {wrapped}", flush=True)
    except Exception as e:
        sys.stderr.write(f"[metrics] не удалось обернуть tools: {e}\n")


def _wrap_tools_with_audit(mcp_obj, server_name: str) -> None:
    """
    Оборачивает tools аудит-логом (задача 3.3).

    Вызывается ТОЛЬКО для rest-proxy: остальные MCP-серверы не ходят
    в живую 1С, их аудитировать нечего. Порядок обёрток важен: audit
    ставится ПОСЛЕ metrics, чтобы audit-декоратор видел исходное
    исключение и время без учёта накладных расходов метрик
    (track ловит исключение и перевыбрасывает — порядок не критичен,
    но так интуитивнее).
    """
    if server_name != "rest-proxy":
        return
    try:
        from audit_log import wrap_mcp_tools
    except Exception as e:
        sys.stderr.write(f"[audit] audit_log недоступен: {e}\n")
        return
    try:
        n = wrap_mcp_tools(mcp_obj)
        print(f"[audit] {server_name}: обёрнуто инструментов: {n}", flush=True)
    except Exception as e:
        sys.stderr.write(f"[audit] не удалось обернуть tools: {e}\n")


def _install_audit_session_middleware(app, server_name: str):
    """
    Оборачивает ASGI-приложение в минимальный middleware, который
    кладёт remote_ip и mcp_session_id в contextvars до вызова tool'а.

    Делаем это только для rest-proxy — чтобы не плодить лишнюю работу
    в остальных серверах.
    """
    if server_name != "rest-proxy":
        return app
    try:
        from audit_log import set_session
    except Exception as e:
        sys.stderr.write(f"[audit] set_session недоступен: {e}\n")
        return app

    async def middleware(scope, receive, send):
        if scope.get("type") == "http":
            client = scope.get("client") or ("", 0)
            ip = client[0] if client else ""
            # Извлекаем session_id: FastMCP SSE кладёт его как query param
            # session_id=... на /messages/<id>, а на initial GET /sse
            # идентификатор ещё не сформирован. Пробуем оба места,
            # молча проглатываем любые ошибки.
            sid = ""
            try:
                qs = (scope.get("query_string") or b"").decode("latin-1")
                for part in qs.split("&"):
                    if part.startswith("session_id="):
                        sid = part.split("=", 1)[1]
                        break
            except Exception:
                pass
            try:
                set_session(remote_ip=ip, session_id=sid)
            except Exception:
                pass
        await app(scope, receive, send)

    return middleware


def _start_metrics_dashboard_async() -> None:
    """Поднимает HTTP-дашборд метрик в отдельном потоке."""
    if os.environ.get("METRICS_DASHBOARD", "true").lower() not in ("true", "1", "yes"):
        return
    try:
        import threading
        from mcp_metrics import get_dashboard_app
    except Exception as e:
        sys.stderr.write(f"[metrics] dashboard недоступен: {e}\n")
        return

    dash_port = int(os.environ.get("METRICS_PORT", "9000"))

    def _run():
        try:
            app = get_dashboard_app()
            uvicorn.run(app, host="0.0.0.0", port=dash_port, log_level="warning")
        except Exception as e:
            sys.stderr.write(f"[metrics] dashboard упал: {e}\n")

    threading.Thread(target=_run, daemon=True, name="metrics-dashboard").start()
    print(f"[metrics] dashboard: http://0.0.0.0:{dash_port}")


def main():
    _check_docker_environment()

    if len(sys.argv) < 2 or sys.argv[1] not in SERVERS:
        print(f"Usage: python start.py <{'|'.join(SERVERS.keys())}>")
        for name, (module, port) in SERVERS.items():
            print(f"  {name:16s} -> port {port}")
        sys.exit(1)

    name = sys.argv[1]
    module, default_port = SERVERS[name]
    port = int(os.environ.get("MCP_PORT", default_port))

    print(f"Starting {name} on port {port}...", flush=True)
    mod = __import__(module)

    for init_func in ("_load_all", "_load_builtin_reference", "_load_templates", "_load_builtin"):
        if hasattr(mod, init_func):
            getattr(mod, init_func)()

    mcp_obj = mod.mcp
    mcp_obj.settings.transport_security.enable_dns_rebinding_protection = False

    _wrap_tools_with_metrics(mcp_obj, name)
    _wrap_tools_with_audit(mcp_obj, name)
    _start_metrics_dashboard_async()

    app = mcp_obj.sse_app()

    # Задача 3.2: оборачиваем SSE-приложение в shared-secret-middleware.
    # Если MCP_SHARED_SECRET пустой — wrap_sse_app вернёт app как есть
    # (с warning в stderr). См. 1c-mcp-suite/mcp_auth.py.
    try:
        from mcp_auth import wrap_sse_app
        app = wrap_sse_app(app, server_name=name)
    except Exception as e:
        sys.stderr.write(f"[mcp-auth] wrap_sse_app failed: {e}\n")

    # Задача 3.3: middleware, который кладёт remote_ip/session_id в
    # contextvars до вызова tool'а (no-op для не-rest-proxy).
    app = _install_audit_session_middleware(app, name)

    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
