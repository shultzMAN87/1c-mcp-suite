#!/usr/bin/env python3
"""
Бенчмарк: прямой вызов MCP-tool vs обёртка через оркестратор
(задача 1.4 из PLAN.md).

ЧТО СРАВНИВАЕМ
==============
Для 5 типовых задач меряем два пути получения одного и того же ответа:

  Путь A  (Direct):     opencode → SSE → mcp-tool → ответ
                        — один round-trip, LLM не участвует.

  Путь B  (Orchestrator): opencode → SSE → orchestrator.ask_agent → HTTP →
                         OpenRouter → LLM (regex-парсинг <tool_call>) → …
                         — несколько round-trip'ов, два слоя LLM.

Для каждого пути меряем:
  - duration_ms   — время от call_tool до ответа
  - ok / fail     — не-isError и непустой text
  - size_chars    — размер сырого ответа (характеризует избыточность)

Что НЕ меряем (осознанно):
  - Качество LLM-ответа семантически — у нас нет LLM-as-judge, а ручная
    оценка выходит за рамки бенчмарка. Сырые ответы сохраняются в JSON,
    их можно глазами сравнить постфактум.
  - Токены / $ — OpenRouter API не всегда возвращает точный usage для
    всех моделей, и прямой путь токенов не тратит вообще. Разница по цене
    очевидна без замера: путь A стоит $0, путь B — деньги.

ЗАПУСК
======
    # С хоста (нужен pip install 'mcp[cli]' httpx):
    python3 scripts/orchestrator_benchmark.py

    # Или через docker-runner:
    docker compose --profile smoke run --rm smoke-runner \\
      python3 /app/scripts/orchestrator_benchmark.py --docker-names

    # Только одну задачу (для отладки):
    python3 scripts/orchestrator_benchmark.py --only metadata_stats

ЦЕНА
====
5 задач × 1 прогон через orchestrator = ~5 LLM-запросов минимум
(больше, если агент делает несколько итераций). На Sonnet 4.5 ~ $0.05-0.40
за полный прогон. Прямой путь бесплатен. Флаг `--skip-agent` позволяет
прогнать только direct-часть и убедиться что бенчмарк запускается, без
трат.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

# Переиспользуем общий stack
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from _smoke_common import resolve_secret  # noqa: E402

EVALS_RUNNER = SCRIPTS_DIR.parent / "evals" / "runner"
sys.path.insert(0, str(EVALS_RUNNER))

try:
    from mcp_client import MCPSession, ToolCallResult  # noqa: E402
except ImportError as e:
    print(f"ОШИБКА импорта MCPSession: {e}\n"
          f"Установите: pip install 'mcp[cli]>=1.0.0'", file=sys.stderr)
    sys.exit(2)


# ─── Пары задач ──────────────────────────────────────────────────────────
#
# Каждая задача — это сопоставимая пара (direct-вызов, agent-вызов),
# где обе должны прийти к одинаковому содержательному результату.
#
# `direct_server`/`direct_port` — адрес MCP-сервера для прямого вызова
# `agent_server`/`agent_port` — всегда orchestrator (8012)
# `agent_name` — кто из AGENTS подходит по набору tools

ORCHESTRATOR_SERVER = "mcp-orchestrator"
ORCHESTRATOR_PORT = 8012


@dataclass
class TaskPair:
    name: str
    description: str
    # Direct path
    direct_server: str
    direct_port: int
    direct_tool: str
    direct_args: dict
    # Agent path
    agent_name: str
    agent_task: str


TASKS: list[TaskPair] = [
    TaskPair(
        name="metadata_stats",
        description="Получить статистику графа метаданных",
        direct_server="mcp-metadata-graph", direct_port=8001,
        direct_tool="metadata_stats", direct_args={},
        agent_name="analyst",
        agent_task=(
            "Узнай у MCP-инструмента metadata_stats текущую статистику графа "
            "метаданных. Верни краткую сводку в 1-2 предложения: сколько объектов, "
            "связей, подсистем. НЕ вызывай другие инструменты."
        ),
    ),
    TaskPair(
        name="platform_help_stats",
        description="Получить статистику коллекции справки платформы",
        direct_server="mcp-platform-help", direct_port=8003,
        direct_tool="platform_help_stats", direct_args={},
        # coder не имеет platform_help_stats, но ему доступен platform_help_search.
        # architect имеет platform_help_search, но не metadata_stats ни help_stats.
        # Ни у одного агента нет явно platform_help_stats — поэтому агент будет
        # вынужден как-то выкрутиться. Для чистоты бенчмарка используем другой
        # tool, который есть и у direct, и у агента: platform_help_search.
        # Переопределяем direct_tool:
        agent_name="coder",
        agent_task=(
            "Через platform_help_search найди всё про метод 'СтрРазделить'. "
            "Ответь одним предложением: сколько результатов и какой главный."
        ),
    ),
    TaskPair(
        name="query_validate",
        description="Валидация запроса 1С",
        direct_server="mcp-query-builder", direct_port=8009,
        direct_tool="query_validate", direct_args={"query_text": "ВЫБРАТЬ 1 КАК Поле"},
        agent_name="reviewer",
        agent_task=(
            "Провалидируй запрос: `ВЫБРАТЬ 1 КАК Поле`. "
            "Используй инструмент query_validate. Верни одно слово: OK или ERRORS."
        ),
    ),
    TaskPair(
        name="bsl_check_code",
        description="Проверка BSL-кода",
        direct_server="mcp-bsl-checker", direct_port=8002,
        direct_tool="bsl_check_code",
        direct_args={"code": "Процедура Тест()\nКонецПроцедуры"},
        agent_name="reviewer",
        agent_task=(
            "Проверь этот BSL-код через bsl_check_code:\n"
            "```\nПроцедура Тест()\nКонецПроцедуры\n```\n"
            "Верни одно слово: OK или список ошибок."
        ),
    ),
    TaskPair(
        name="templates_search",
        description="Поиск шаблона кода",
        direct_server="mcp-code-templates", direct_port=8008,
        direct_tool="templates_search",
        direct_args={"query": "цикл по ссылкам", "limit": 3},
        agent_name="coder",
        agent_task=(
            "Найди через templates_search шаблон для цикла по ссылкам. "
            "Верни одно предложение: нашёлся ли подходящий шаблон и его имя."
        ),
    ),
]

# Спец-override для platform_help_stats кейса: direct_tool != direct_tool
# в изначальном списке — используем platform_help_search как парный.
TASKS[1] = TaskPair(
    name="platform_help_search",
    description="Поиск в справке платформы",
    direct_server="mcp-platform-help", direct_port=8003,
    direct_tool="platform_help_search",
    direct_args={"query": "СтрРазделить", "limit": 3},
    agent_name="coder",
    agent_task=(
        "Через platform_help_search найди всё про метод 'СтрРазделить' (limit=3). "
        "Ответь одним предложением: сколько результатов."
    ),
)


# ─── Результат и замер ───────────────────────────────────────────────────


@dataclass
class PathResult:
    path: str             # "direct" | "agent"
    ok: bool
    duration_ms: float
    size_chars: int
    error: str | None
    raw_response: str | None


@dataclass
class TaskResult:
    task: TaskPair
    direct: PathResult | None = None
    agent: PathResult | None = None

    def overhead_ratio(self) -> float | None:
        """Agent-path latency / Direct-path latency. None при ошибке."""
        if (self.direct is None or self.agent is None
                or not self.direct.ok or not self.agent.ok
                or self.direct.duration_ms <= 0):
            return None
        return self.agent.duration_ms / self.direct.duration_ms


async def run_direct(task: TaskPair, host: str, timeout: float,
                     use_docker_names: bool) -> PathResult:
    """Прямой вызов MCP-tool на конкретном сервере."""
    target = task.direct_server if use_docker_names else host
    url = f"http://{target}:{task.direct_port}/sse"

    t0 = time.perf_counter()
    try:
        async with MCPSession(url, init_timeout=timeout, call_timeout=timeout) as sess:
            result = await sess.call_tool(task.direct_tool, task.direct_args)
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        return PathResult(
            path="direct", ok=False, duration_ms=dt, size_chars=0,
            error=f"{type(e).__name__}: {str(e)[:200]}",
            raw_response=None,
        )
    dt = (time.perf_counter() - t0) * 1000

    if not result.ok:
        return PathResult(path="direct", ok=False, duration_ms=dt,
                          size_chars=0, error=result.error, raw_response=None)
    if result.is_error_flag:
        return PathResult(path="direct", ok=False, duration_ms=dt,
                          size_chars=len(result.raw_text or ""),
                          error="isError", raw_response=result.raw_text)
    text = result.raw_text or ""
    return PathResult(path="direct", ok=True, duration_ms=dt,
                      size_chars=len(text), error=None, raw_response=text)


async def run_agent(task: TaskPair, host: str, timeout: float,
                    use_docker_names: bool) -> PathResult:
    """Вызов через orchestrator.ask_agent — тот сам зовёт LLM и tools."""
    target = ORCHESTRATOR_SERVER if use_docker_names else host
    url = f"http://{target}:{ORCHESTRATOR_PORT}/sse"

    t0 = time.perf_counter()
    try:
        async with MCPSession(url, init_timeout=timeout, call_timeout=timeout) as sess:
            # ask_agent(agent, task, context="")
            result = await sess.call_tool("ask_agent", {
                "agent": task.agent_name,
                "task": task.agent_task,
            })
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        return PathResult(
            path="agent", ok=False, duration_ms=dt, size_chars=0,
            error=f"{type(e).__name__}: {str(e)[:200]}",
            raw_response=None,
        )
    dt = (time.perf_counter() - t0) * 1000

    if not result.ok:
        return PathResult(path="agent", ok=False, duration_ms=dt,
                          size_chars=0, error=result.error, raw_response=None)
    if result.is_error_flag:
        return PathResult(path="agent", ok=False, duration_ms=dt,
                          size_chars=len(result.raw_text or ""),
                          error="isError", raw_response=result.raw_text)
    text = result.raw_text or ""
    return PathResult(path="agent", ok=True, duration_ms=dt,
                      size_chars=len(text), error=None, raw_response=text)


# ─── Main ────────────────────────────────────────────────────────────────


def _fmt_ms(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms / 1000:.2f} s"


async def _amain(args: argparse.Namespace) -> int:
    secret = resolve_secret(args.secret, env_file=args.env_file)
    if secret:
        os.environ["MCP_SHARED_SECRET"] = secret

    tasks = TASKS
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        tasks = [t for t in TASKS if t.name in wanted]
        if not tasks:
            print(f"ОШИБКА: --only={args.only!r} не матчит ни одной задачи.",
                  file=sys.stderr)
            print(f"Доступны: {', '.join(t.name for t in TASKS)}", file=sys.stderr)
            return 2

    # Шапка
    print("=" * 80)
    print("ORCHESTRATOR BENCHMARK — Direct vs Agent (задача 1.4)")
    print("=" * 80)
    print(f"Host: {'docker-network' if args.docker_names else args.host}")
    print(f"Secret: {'задан' if secret else 'не задан'}")
    print(f"Tasks: {len(tasks)} из {len(TASKS)}")
    print(f"Timeout на путь: {args.timeout}s")
    if args.skip_agent:
        print("⚠  --skip-agent: путь через оркестратор пропускается")
    print()

    results: list[TaskResult] = []
    for idx, task in enumerate(tasks, start=1):
        print(f"[{idx}/{len(tasks)}] {task.name} — {task.description}")
        tr = TaskResult(task=task)

        # Direct
        print(f"  → direct: {task.direct_server}:{task.direct_port} "
              f"→ {task.direct_tool}(...)", flush=True, end="")
        d = await run_direct(task, args.host, args.timeout, args.docker_names)
        tr.direct = d
        mark = "✓" if d.ok else "✗"
        print(f"  {mark} {_fmt_ms(d.duration_ms):>10s}  "
              f"{d.size_chars if d.ok else d.error} chars")
        if not d.ok:
            print(f"      error: {d.error}")

        # Agent
        if args.skip_agent:
            print(f"  → agent:  (пропущен)")
        else:
            print(f"  → agent:  ask_agent({task.agent_name!r}, '{task.agent_task[:60]}...')",
                  flush=True, end="")
            a = await run_agent(task, args.host, args.timeout, args.docker_names)
            tr.agent = a
            mark = "✓" if a.ok else "✗"
            print(f"  {mark} {_fmt_ms(a.duration_ms):>10s}  "
                  f"{a.size_chars if a.ok else a.error} chars")
            if not a.ok:
                print(f"      error: {a.error}")

            # Overhead
            ratio = tr.overhead_ratio()
            if ratio is not None:
                print(f"  → overhead: {ratio:.1f}x")

        print()
        results.append(tr)

    # Итоги
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"{'Task':<24} {'Direct':>12} {'Agent':>12} {'Overhead':>10}  {'Status':<10}")
    print("-" * 80)
    overheads = []
    for tr in results:
        d = tr.direct
        a = tr.agent
        dstr = _fmt_ms(d.duration_ms) if d and d.ok else ("—" if not d else "ERR")
        astr = (_fmt_ms(a.duration_ms) if a and a.ok
                else ("skip" if args.skip_agent else ("—" if not a else "ERR")))
        ratio = tr.overhead_ratio()
        rstr = f"{ratio:.1f}x" if ratio is not None else "—"
        if ratio is not None:
            overheads.append(ratio)
        status = "ok" if (d and d.ok and (args.skip_agent or (a and a.ok))) else "partial"
        print(f"{tr.task.name:<24} {dstr:>12} {astr:>12} {rstr:>10}  {status:<10}")

    print("-" * 80)
    if overheads:
        # Медиана и среднее — для устойчивости к выбросам
        avg = sum(overheads) / len(overheads)
        med = sorted(overheads)[len(overheads) // 2]
        min_o = min(overheads)
        max_o = max(overheads)
        print(f"Overhead (N={len(overheads)}): "
              f"min={min_o:.1f}x  median={med:.1f}x  avg={avg:.1f}x  max={max_o:.1f}x")
        print()
        print("Интерпретация:")
        print(f"  Через оркестратор типовая задача выполняется в {med:.1f} раза дольше")
        print(f"  прямого вызова MCP-tool'а (по медиане {len(overheads)} задач).")
        print(f"  Это объективный архитектурный overhead: LLM-inference + regex-парсинг")
        print(f"  <tool_call> + вторая LLM-итерация на финальный ответ.")
    else:
        print("Нет успешных пар direct+agent — overhead не посчитан.")

    # Сохраняем сырые ответы
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        # Делаем JSON-сериализуемый snapshot
        payload = {
            "host": args.host,
            "docker_names": args.docker_names,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tasks": [
                {
                    "name": tr.task.name,
                    "description": tr.task.description,
                    "direct": asdict(tr.direct) if tr.direct else None,
                    "agent": asdict(tr.agent) if tr.agent else None,
                    "overhead_ratio": tr.overhead_ratio(),
                }
                for tr in results
            ],
            "overhead_summary": {
                "n": len(overheads),
                "min": min(overheads) if overheads else None,
                "median": sorted(overheads)[len(overheads) // 2] if overheads else None,
                "avg": (sum(overheads) / len(overheads)) if overheads else None,
                "max": max(overheads) if overheads else None,
            } if overheads else None,
        }
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nСырой отчёт сохранён: {report_path}")

    # Exit code: 0 если все успешные, 1 если хоть один fail
    any_fail = any(
        (r.direct is None or not r.direct.ok)
        or (not args.skip_agent and (r.agent is None or not r.agent.ok))
        for r in results
    )
    return 1 if any_fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Direct-vs-Orchestrator benchmark (задача 1.4)"
    )
    ap.add_argument("--host", default=os.environ.get("MCP_HOST", "localhost"),
                    help="Хост MCP-серверов (default: localhost)")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--secret", default=None)
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="Таймаут на один путь (default: 300s — у агента "
                         "может быть до 5 итераций, каждая до минуты)")
    ap.add_argument("--only", default=None,
                    help="Только указанные задачи через запятую")
    ap.add_argument("--docker-names", action="store_true",
                    default=os.environ.get("MCP_USE_DOCKER_NAMES", "").lower()
                            in ("1", "true", "yes"),
                    help="Ходить по docker-DNS именам (для smoke-runner)")
    ap.add_argument("--skip-agent", action="store_true",
                    help="Не прогонять путь через оркестратор (без LLM-трат)")
    ap.add_argument("--report", default=None,
                    help="Путь к JSON-отчёту с сырыми ответами "
                         "(default: не сохраняем)")
    args = ap.parse_args()

    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
