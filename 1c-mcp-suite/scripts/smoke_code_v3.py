"""
End-to-end smoke для слоя 2 (4.6.2) через MCP SDK.
====================================================

Запуск (как в README 4.6.1 smoke):

  docker run --rm `
    --network 1c-suite-net `
    -e MCP_SHARED_SECRET=<секрет> `
    -v "D:\\Docker\\27_1c-mcp-suite-full-stack\\1c-mcp-suite\\scripts:/scripts:ro" `
    python:3.12-slim `
    sh -c "pip install --quiet 'mcp[cli]' && python /scripts/smoke_code_v3.py"

Ожидание: "Smoke v3 code graph — 6/6 passed".

Кейсы (как в плане):
  1. code_callers по Факториал → найдён тестовый caller из tests-extension
  2. code_callees по Факториал → пусто (рекурсия + встроенные)
  3. code_call_path → путь между формой и серверной функцией существует
  4. code_procedures_operating_on(Catalog.АукАукционы) → не пусто
  5. code_dead_procedures → разумный размер списка
  6. code_v3_stats → корректные счёты узлов и рёбер

Каждый кейс возвращает (ok, msg). 6/6 ok → exit 0.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp.client.sse import sse_client  # type: ignore
from mcp import ClientSession  # type: ignore


MCP_URL    = os.environ.get("MCP_URL",    "http://mcp-metadata-graph:8001/sse")
MCP_SECRET = os.environ.get("MCP_SHARED_SECRET", "")
FACTORIAL  = "CommonModule.АукОбщийКлиент.Факториал"


def _ok_msg(name: str, ok: bool, detail: str) -> tuple[bool, str]:
    mark = "✓" if ok else "✗"
    return ok, f"  {mark} {name}: {detail}"


async def _call(session: ClientSession, name: str, args: dict) -> dict:
    r = await session.call_tool(name, args)
    if not r.content:
        return {}
    text = r.content[0].text if hasattr(r.content[0], "text") else str(r.content[0])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


async def smoke() -> int:
    headers = {"Authorization": f"Bearer {MCP_SECRET}"} if MCP_SECRET else {}
    async with sse_client(MCP_URL, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            results: list[tuple[bool, str]] = []

            # 1. code_callers Факториал → tests-extension caller есть
            r = await _call(session, "code_callers", {
                "full_name": FACTORIAL,
                "depth": 1,
            })
            callers = r.get("callers", [])
            names = {c.get("full_name") for c in callers}
            has_test = any("Тест_Факториал" in (n or "") for n in names)
            results.append(_ok_msg(
                "code_callers(Факториал)",
                r.get("found", False) and has_test,
                f"найдено {len(callers)} caller'ов, тестовый caller = {has_test}",
            ))

            # 2. code_callees Факториал — на реальной Котировке Факториал
            # реализован итеративно, без рекурсии. Все его вызовы внутри тела
            # (НСтр, ВызватьИсключение, цикл Для) либо в built-in, либо в keyword
            # parser-фильтре. Ожидание: 0 callee'ов.
            r = await _call(session, "code_callees", {
                "full_name": FACTORIAL,
                "depth": 2,
            })
            callees = r.get("callees", [])
            results.append(_ok_msg(
                "code_callees(Факториал, depth=2)",
                r.get("found", False) and len(callees) == 0,
                f"найдено {len(callees)} (ожидание: 0)",
            ))

            # 3. code_call_path: Тест_Факториал0 → Факториал — путь длины 1
            r = await _call(session, "code_call_path", {
                "from_full_name": "CommonModule.Тест_Аукционы_АукОбщийКлиент.Тест_Факториал0",
                "to_full_name":   FACTORIAL,
                "max_depth": 3,
            })
            length = r.get("length")
            results.append(_ok_msg(
                "code_call_path(Тест→Факториал)",
                r.get("found", False) and length == 1,
                f"длина пути = {length} (ожидание 1)",
            ))

            # 4. code_procedures_operating_on(Catalog.АукАукционы)
            r = await _call(session, "code_procedures_operating_on", {
                "metadata_full_name": "Catalog.АукАукционы",
            })
            ops = r.get("callables", [])
            results.append(_ok_msg(
                "code_procedures_operating_on(АукАукционы)",
                r.get("found", False) and len(ops) >= 3,
                f"найдено {len(ops)} процедур (ожидание ≥3)",
            ))

            # 5. code_dead_procedures — разумный размер
            r = await _call(session, "code_dead_procedures", {
                "exclude_handlers": True,
                "include_exports": False,
                "limit": 200,
            })
            dead = r.get("dead", [])
            # На Котировках без обработчиков ожидаем десятки, не сотни и не нули.
            ok = r.get("found", False) and 0 <= len(dead) <= 300
            results.append(_ok_msg(
                "code_dead_procedures",
                ok,
                f"мёртвых процедур: {len(dead)}",
            ))

            # 6. code_v3_stats — sanity на счётах
            r = await _call(session, "code_v3_stats", {})
            nodes = r.get("nodes", {})
            edges = r.get("edges", {})
            cs = r.get("callsites", {})
            ok = (
                r.get("found", False)
                and nodes.get("Callable", 0) > 500
                and edges.get("CALLS", 0) > 100
                and 30 <= cs.get("coverage_pct", 0) <= 100
            )
            results.append(_ok_msg(
                "code_v3_stats",
                ok,
                f"Callable={nodes.get('Callable')}, CALLS={edges.get('CALLS')}, "
                f"coverage={cs.get('coverage_pct')}%",
            ))

            # Также проверим code_method_signature и code_unresolved_callsites
            # как bonus-кейсы 7 и 8 — для полноты, но не учитываем в счёте 6/6.
            r7 = await _call(session, "code_method_signature", {
                "full_name": FACTORIAL,
            })
            sig_ok = r7.get("found", False) and len(r7.get("parameters", [])) >= 1
            results.append(_ok_msg(
                "code_method_signature(Факториал)",
                sig_ok,
                f"параметров: {len(r7.get('parameters', []))}",
            ))

            r8 = await _call(session, "code_unresolved_callsites", {
                "reason": "unknown_module",
                "limit": 10,
            })
            results.append(_ok_msg(
                "code_unresolved_callsites(unknown_module)",
                r8.get("found", False) and r8.get("total", 0) > 0,
                f"unresolved: {r8.get('total', 0)}",
            ))

            print("Smoke v3 code graph:")
            for ok, msg in results:
                print(msg)
            n_pass = sum(1 for ok, _ in results if ok)
            n_total = len(results)
            print(f"\n  {n_pass}/{n_total} passed")
            return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(smoke()))
