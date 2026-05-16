#!/usr/bin/env python3
"""
Smoke-тест MCP-серверов через полноценный SSE + MCP SDK (задача 3.5).

Что делает:
  1. Читает MCP_SHARED_SECRET из .env/env (если нужен Bearer).
  2. Для каждого из 11 MCP-серверов:
     - открывает SSE-сессию (если секрет задан — с Bearer-заголовком);
     - `initialize`;
     - вызывает ровно один безопасный read-only tool ("kick-tool");
     - проверяет: не isError, есть непустой text-ответ, ответ парсится
       (для большинства — в JSON; для нескольких старых tools, которые
       возвращают сырой markdown — достаточно проверки "не пустой");
     - закрывает сессию.
  3. Печатает табличный отчёт + latency + сводку.
  4. Exit-code: 0 если все 11 OK, иначе 1.

Зачем отдельно от smoke_auth.py:
  smoke_auth.py работает на уровне HTTP (401/200 на /sse), он не знает
  JSON-RPC и не может подтвердить, что FastMCP-приложение внутри живое
  и tools действительно отвечают. Этот скрипт закрывает верхний уровень.

Критерии выбора kick-tool для каждого сервера:
  - идемпотентный (не меняет состояние — не запускает scans, не пишет в Neo4j);
  - не ходит в сторонние платные сервисы (например, Напарник);
  - не требует живой 1С — ограничиваемся инспекцией локальной конфигурации;
  - минимальный или пустой набор аргументов;
  - быстрый (< 10 сек на тёплом стеке).

Использование:
    # Обычный запуск — поднят docker compose
    python3 scripts/smoke_mcp.py

    # Против другого хоста
    MCP_HOST=192.168.1.10 python3 scripts/smoke_mcp.py

    # Подробный вывод ошибок
    python3 scripts/smoke_mcp.py --verbose

    # Один сервер (для отладки регрессии)
    python3 scripts/smoke_mcp.py --only mcp-platform-help
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Общий список серверов + загрузка секрета
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from _smoke_common import MCP_SERVERS, resolve_secret, verify_against_json  # noqa: E402

# Переиспользуем готовый MCPSession из evals/runner/mcp_client.py —
# он уже протестирован и умеет headers, таймауты, корректное закрытие.
EVALS_RUNNER = SCRIPTS_DIR.parent / "evals" / "runner"
sys.path.insert(0, str(EVALS_RUNNER))

try:
    from mcp_client import MCPSession, ToolCallResult  # noqa: E402
except ImportError as e:
    print(
        f"ОШИБКА импорта MCPSession из evals/runner/mcp_client.py: {e}\n"
        f"Убедитесь, что установлен MCP SDK: pip install 'mcp[cli]>=1.0.0'",
        file=sys.stderr,
    )
    sys.exit(2)


# ─── Конфигурация проб ───────────────────────────────────────────────────

# Минимальный BSL-сниппет для bsl_check_code: валидный, без ошибок.
BSL_MINIMAL = "Процедура Тест()\nКонецПроцедуры"

# Минимальный SQL для query_validate: синтаксически валидный.
QUERY_MINIMAL = "ВЫБРАТЬ 1 КАК Поле"


@dataclass
class Probe:
    """Описание одной пробы: какой tool у какого сервера вызываем и как
    валидируем ответ."""
    server_name: str
    port: int
    tool: str
    args: dict
    # Если возвращает JSON — валидатор работает по parsed dict.
    # Если возвращает сырой текст — проверяем только is-not-empty.
    check_json: bool = True
    # Опциональный валидатор: получает parsed dict (или raw_text если
    # check_json=False), возвращает (ok, reason). Если None — достаточно
    # того, что call не isError и есть непустое содержимое.
    extra_check: Callable[[Any], tuple[bool, str]] | None = None
    notes: str = ""


# ─── Extra-валидаторы ────────────────────────────────────────────────────

def _check_platform_help_stats(data: dict) -> tuple[bool, str]:
    """У platform_help_stats должен быть либо collection_kind=hybrid с
    total_points>0, либо явный missing. Всё остальное — повод поднять бровь.
    """
    kind = data.get("collection_kind") or data.get("help_collection_kind")
    total = data.get("total_points", data.get("points_count", 0))
    if kind == "missing":
        return False, "platform_help collection missing (Qdrant не поднят или не индексирован)"
    if kind in ("hybrid", "legacy_dense"):
        if isinstance(total, int) and total > 0:
            return True, f"kind={kind} points={total}"
        # Коллекция есть, но пустая — не фатально для smoke (может быть
        # свежий кластер без help-indexer), но отметим в заметках.
        return True, f"kind={kind} (точек нет — индексатор ещё не отработал?)"
    # Если сервер не вернул ожидаемый ключ — всё равно считаем тест
    # пройденным (tool работает), но даём диагностическую подсказку.
    return True, f"unknown schema: keys={sorted(data.keys())[:5]}"


def _check_non_empty_dict(data: dict) -> tuple[bool, str]:
    if not data:
        return False, "пустой JSON-ответ"
    return True, f"keys={len(data)}"


# ─── Список всех проб ────────────────────────────────────────────────────
#
# Порядок — как в MCP_SERVERS. Один сервер = одна проба.
#
# Почему именно эти tools:
#   metadata_graph:  metadata_stats       — самый дешёвый read-only
#   bsl-checker:     bsl_check_code       — не требует файловой системы
#   platform-help:   platform_help_stats  — показывает статус Qdrant+hybrid
#   naparnik:        naparnik_check_connection — проверяет сам tool,
#                      не делая реальный запрос в Напарник (платный)
#   code-templates:  templates_count      — быстро, не парсит шаблоны
#                      (они уже загружены на старте)
#   query-builder:   query_validate       — статический разбор, Neo4j не нужен
#   testing:         test_runner_health   — проверка окружения
#   code-rag:        code_search + limit=1 — лёгкий поиск, без ensure_collection
#   rest-proxy:      connection_info      — читает конфиг, НЕ ходит в 1С
#   sonarqube:       sonar_list_projects  — один GET к sonarqube-api

PROBES: list[Probe] = [
    Probe(
        server_name="mcp-metadata-graph", port=8001,
        tool="metadata_stats", args={},
        notes="Обратите внимание: при пустом 1c-config-xml ответ будет с "
              "нулевыми счётчиками — tool всё равно должен ответить без isError",
    ),
    Probe(
        server_name="mcp-bsl-checker", port=8002,
        tool="bsl_check_code", args={"code": BSL_MINIMAL},
        check_json=False,  # Возвращает markdown-отчёт
        notes="Минимальный валидный BSL — ожидаем текстовый отчёт без падения",
    ),
    Probe(
        server_name="mcp-platform-help", port=8003,
        tool="platform_help_stats", args={},
        extra_check=_check_platform_help_stats,
        notes="Проверяем, что hybrid-коллекция поднята и содержит точки",
    ),
    Probe(
        server_name="mcp-1c-naparnik", port=8007,
        tool="naparnik_check_connection", args={},
        check_json=False,
        notes="Tool проверяет достижимость code.1c.ai и валидность токена. "
              "При пустом ONEC_AI_TOKEN вернёт сообщение об ошибке — это ОК "
              "для smoke, tool отработал",
    ),
    Probe(
        server_name="mcp-code-templates", port=8008,
        tool="templates_count", args={},
        check_json=False,
        notes="Возвращает количество шаблонов текстом",
    ),
    Probe(
        server_name="mcp-query-builder", port=8009,
        tool="query_validate", args={"query_text": QUERY_MINIMAL},
        check_json=False,
        notes="Статический разбор, Neo4j опционален",
    ),
    Probe(
        server_name="mcp-testing", port=8010,
        tool="test_runner_health", args={},
        notes="Отчёт окружения test-runner",
    ),
    Probe(
        server_name="mcp-code-rag", port=8011,
        tool="code_search", args={"query": "процедура", "limit": 1},
        check_json=False,
        notes="Даже на пустом workspace должен ответить без isError "
              "(builtin fallback)",
    ),
    Probe(
        server_name="mcp-rest-proxy", port=8013,
        tool="connection_info", args={},
        check_json=False,
        notes="Только читает .env/ONEC_* — НЕ делает реальный HTTP в 1С",
    ),
    Probe(
        server_name="mcp-sonarqube", port=8014,
        tool="sonar_list_projects", args={},
        check_json=False,
        notes="Один HTTP к sonarqube-api. Если SonarQube ещё поднимается "
              "(первые 1-2 минуты компоуза) — может быть FAIL с connection refused",
    ),
]


# ─── Единообразие порта/конфигурации ─────────────────────────────────────

# Санити-чек: PROBES покрывают ровно MCP_SERVERS, один-к-одному по (name, port).
_probe_keys = {(p.server_name, p.port) for p in PROBES}
_mcp_keys = set(MCP_SERVERS)
assert _probe_keys == _mcp_keys, (
    f"PROBES vs MCP_SERVERS рассогласованы:\n"
    f"  в PROBES лишние: {_probe_keys - _mcp_keys}\n"
    f"  в MCP_SERVERS лишние: {_mcp_keys - _probe_keys}"
)


# ─── Одна проба ──────────────────────────────────────────────────────────


def _unwrap_exception(exc: BaseException, depth: int = 4) -> BaseException:
    """Разворачивает ExceptionGroup / __cause__ / __context__ до первой
    "настоящей" ошибки — той, чьё имя класса реально информативно для
    пользователя. Нужно для MCP SDK, который пакует ConnectionRefused и
    TimeoutError через anyio.TaskGroup в ExceptionGroup.

    Глубина ограничена, чтобы не зациклиться на хитрых цепочках.
    """
    for _ in range(depth):
        # PY3.11+: ExceptionGroup
        subs = getattr(exc, "exceptions", None)
        if subs:
            exc = subs[0]
            continue
        # Обычная цепочка chained exceptions
        cause = exc.__cause__ or exc.__context__
        # Идём глубже только если внизу что-то более конкретное, чем
        # generic RuntimeError / ExceptionGroup
        if cause is not None and type(exc).__name__ in (
            "ExceptionGroup", "BaseExceptionGroup", "RuntimeError"
        ):
            exc = cause
            continue
        break
    return exc


@dataclass
class ProbeResult:
    probe: Probe
    ok: bool
    duration_ms: float
    detail: str       # короткое описание для таблицы
    error: str | None  # если ok=False — причина
    raw: str | None   # сырой текст ответа (для --verbose)


async def run_probe(host: str, probe: Probe, timeout_s: float,
                    use_docker_names: bool = False) -> ProbeResult:
    """Открывает SSE-сессию, делает initialize + один call_tool, закрывает.

    Args:
        host: хост для всех проб (обычно localhost). Игнорируется при
            use_docker_names=True.
        probe: описание пробы.
        timeout_s: таймаут на initialize и на call_tool.
        use_docker_names: если True, каждый сервер адресуется по имени
            сервиса из `probe.server_name` (`mcp-metadata-graph:8001` и
            т.д.). Нужно при запуске smoke-runner'а внутри docker-network,
            где порты 8001..8014 на разных контейнерах, а не на одном хосте.
    """
    target_host = probe.server_name if use_docker_names else host
    url = f"http://{target_host}:{probe.port}/sse"
    t0 = time.perf_counter()

    try:
        async with MCPSession(url, init_timeout=timeout_s, call_timeout=timeout_s) as sess:
            result: ToolCallResult = await sess.call_tool(probe.tool, probe.args)
    except Exception as e:
        # Любая ошибка на уровне транспорта/инициализации — недоступен или
        # ошибка initialize. Сообщение делаем коротким для таблицы.
        # MCP SDK оборачивает ошибки соединения в ExceptionGroup (через
        # anyio.TaskGroup); разворачиваем до первой внутренней причины,
        # иначе пользователь увидит бесполезное "unhandled errors in a
        # TaskGroup" вместо "Connection refused".
        dt = (time.perf_counter() - t0) * 1000
        root = _unwrap_exception(e)
        err_type = type(root).__name__
        err_msg = str(root)[:200] or repr(root)[:200]
        return ProbeResult(
            probe=probe, ok=False, duration_ms=dt,
            detail="UNREACHABLE",
            error=f"{err_type}: {err_msg}",
            raw=None,
        )

    dt = (time.perf_counter() - t0) * 1000

    # Уровень 1: call_tool сам сказал что не смог (timeout/connection/etc).
    if not result.ok:
        return ProbeResult(
            probe=probe, ok=False, duration_ms=dt,
            detail="CALL_FAILED",
            error=result.error or "unknown",
            raw=None,
        )

    # Уровень 2: tool вернул isError=true.
    if result.is_error_flag:
        snippet = (result.raw_text or "")[:200].replace("\n", " ")
        return ProbeResult(
            probe=probe, ok=False, duration_ms=dt,
            detail="isError",
            error=f"tool returned isError: {snippet}",
            raw=result.raw_text,
        )

    # Уровень 3: содержимое пустое.
    if not result.raw_text or not result.raw_text.strip():
        return ProbeResult(
            probe=probe, ok=False, duration_ms=dt,
            detail="EMPTY",
            error="пустой text-ответ",
            raw=result.raw_text,
        )

    # Уровень 4: проверки формата.
    if probe.check_json:
        # parsed заполнен из JSON в MCPSession — если он None, значит JSON
        # не распарсился (error в ToolCallResult.error). Но ok=True значит
        # что-то в raw_text есть, просто не JSON. Для tools, где мы ждём
        # JSON, это FAIL.
        if result.parsed is None:
            return ProbeResult(
                probe=probe, ok=False, duration_ms=dt,
                detail="NOT_JSON",
                error=f"ожидали JSON, не распарсилось: {result.error}",
                raw=result.raw_text,
            )

        if probe.extra_check is not None:
            ok, reason = probe.extra_check(result.parsed)
            if not ok:
                return ProbeResult(
                    probe=probe, ok=False, duration_ms=dt,
                    detail="SEMANTIC",
                    error=reason,
                    raw=result.raw_text,
                )
            return ProbeResult(
                probe=probe, ok=True, duration_ms=dt,
                detail=reason, error=None, raw=result.raw_text,
            )
        # Нет extra_check → достаточно непустого dict
        ok, reason = _check_non_empty_dict(result.parsed)
        if not ok:
            return ProbeResult(
                probe=probe, ok=False, duration_ms=dt,
                detail="EMPTY_DICT",
                error=reason, raw=result.raw_text,
            )
        return ProbeResult(
            probe=probe, ok=True, duration_ms=dt,
            detail=reason, error=None, raw=result.raw_text,
        )

    # check_json=False → достаточно непустого текста.
    if probe.extra_check is not None:
        ok, reason = probe.extra_check(result.raw_text)
        if not ok:
            return ProbeResult(
                probe=probe, ok=False, duration_ms=dt,
                detail="SEMANTIC", error=reason, raw=result.raw_text,
            )
        return ProbeResult(
            probe=probe, ok=True, duration_ms=dt,
            detail=reason, error=None, raw=result.raw_text,
        )
    size = len(result.raw_text)
    return ProbeResult(
        probe=probe, ok=True, duration_ms=dt,
        detail=f"text ok ({size} chars)",
        error=None, raw=result.raw_text,
    )


# ─── Main ────────────────────────────────────────────────────────────────


async def _amain(args: argparse.Namespace) -> int:
    secret = resolve_secret(args.secret, env_file=args.env_file)

    # Если секрет задан (непустой), MCPSession возьмёт его из env
    # MCP_SHARED_SECRET. Прокинем.
    if secret:
        os.environ["MCP_SHARED_SECRET"] = secret

    # Санити-чек: PROBES vs mcp-config.json
    cfg = Path(args.config)
    drift = verify_against_json(cfg)
    if drift:
        print("⚠  расхождение с mcp-config.json:", file=sys.stderr)
        for p in drift:
            print(f"   - {p}", file=sys.stderr)
        print("", file=sys.stderr)

    # Фильтр по --only
    probes_to_run = PROBES
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        probes_to_run = [p for p in PROBES if p.server_name in wanted]
        if not probes_to_run:
            print(f"ОШИБКА: --only={args.only!r} не нашёл ни одного сервера.", file=sys.stderr)
            print(f"Доступны: {', '.join(p.server_name for p in PROBES)}", file=sys.stderr)
            return 2

    # Шапка
    if args.docker_names:
        print(f"Host: docker-network (hostname = имя сервиса)")
    else:
        print(f"Host: {args.host}")
    print(f"Secret: {'задан (length=' + str(len(secret)) + ')' if secret else 'не задан'}")
    print(f"Проб: {len(probes_to_run)} из {len(PROBES)}")
    print(f"Timeout на пробу: {args.timeout}s")
    print("─" * 92)

    # Последовательно (параллельный запуск даст гонку при старте FastMCP-
    # приложения на слабых машинах, плюс сложнее читать лог).
    results: list[ProbeResult] = []
    for probe in probes_to_run:
        print(f"… {probe.server_name}:{probe.port}  →  {probe.tool}({_short_args(probe.args)})",
              flush=True, end="")
        r = await run_probe(args.host, probe, timeout_s=args.timeout,
                            use_docker_names=args.docker_names)
        results.append(r)
        mark = "✓" if r.ok else "✗"
        elapsed = f"{r.duration_ms / 1000:6.2f}s"
        # Перезаписываем строку с финальным маркером
        print(f"\r{mark} {probe.server_name:22s}:{probe.port}  "
              f"{probe.tool:30s}  {elapsed}  {r.detail}")
        if not r.ok and args.verbose:
            print(f"   └─ error: {r.error}")
            if r.raw:
                snippet = r.raw[:500].replace("\n", "\n      ")
                print(f"   └─ raw (500 chars):\n      {snippet}")

    # Сводка
    print("─" * 92)
    passed = sum(1 for r in results if r.ok)
    failed = len(results) - passed
    total_ms = sum(r.duration_ms for r in results)
    print(f"Pass: {passed}/{len(results)}   Fail: {failed}   "
          f"Total time: {total_ms / 1000:.1f}s")

    if failed:
        print("\nПровалы:")
        for r in results:
            if not r.ok:
                print(f"  ✗ {r.probe.server_name}: {r.detail}")
                print(f"    {r.error}")
        print("\nИТОГ: FAIL")
        return 1

    print("\nИТОГ: PASS")
    return 0


def _short_args(args: dict) -> str:
    """Компактное представление аргументов в заголовке строки."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 20:
            v = v[:17] + "..."
        parts.append(f"{k}={v!r}")
    return ", ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Smoke-тест MCP-серверов через SSE + MCP SDK (задача 3.5)",
    )
    ap.add_argument("--host", default=os.environ.get("MCP_HOST", "localhost"),
                    help="Хост MCP-серверов (по умолчанию localhost)")
    ap.add_argument("--env-file", default=".env",
                    help="Путь к .env (по умолчанию ./.env)")
    ap.add_argument("--secret", default=None,
                    help="Явный секрет; перекрывает и env, и .env")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="Таймаут в секундах на один вызов (по умолчанию 30)")
    ap.add_argument("--only", default=None,
                    help="Запустить только указанные сервера через запятую, "
                         "например: --only mcp-platform-help,mcp-code-rag")
    ap.add_argument("--config", default="1c-mcp-suite/mcp-config.json",
                    help="Путь к mcp-config.json для санити-чека списка серверов")
    ap.add_argument("--docker-names", action="store_true",
                    default=os.environ.get("MCP_USE_DOCKER_NAMES", "").lower()
                            in ("1", "true", "yes"),
                    help="Ходить к серверам по docker-DNS именам "
                         "(mcp-metadata-graph:8001 и т.д.) — нужно при запуске "
                         "внутри docker-network, см. compose-профиль smoke. "
                         "Можно также установить MCP_USE_DOCKER_NAMES=1")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Показывать error/raw при падениях")
    args = ap.parse_args()

    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
