"""
Smoke-тест v3 tools метаданного графа.
=======================================

Запуск:
  # Локально (порты на хосте, как для других smoke):
  python3 scripts/smoke_metadata_v3.py --local

  # В стеке (через docker compose run в профиле smoke):
  docker compose --profile smoke run --rm smoke-runner \
      python3 -m scripts.smoke_metadata_v3

Проверяет 6 семантических кейсов на реальном графе:
  1. metadata_v3_stats возвращает >0 узлов :Attribute и :Type — значит,
     новый indexer отработал и схема ушла от 69-узловой v2.
  2. metadata_attribute_type на 'Catalog.АукАукционы', 'ВидАукциона'
     возвращает type kind=CatalogRef, target=Catalog.АукВидыАукционов.
  3. metadata_referrers на 'Catalog.АукВидыАукционов' содержит реквизит
     'ВидАукциона' у Catalog.АукАукционы.
  4. metadata_find_link_path между ними находит хотя бы один путь.
  5. metadata_object_attributes для регистра возвращает атрибуты с разными
     role (dimension/resource).
  6. metadata_subsystem_tree возвращает хотя бы одну подсистему.

Exit-code:
  0 = все 6 кейсов прошли
  1 = хотя бы один failed (с описанием в stdout)
  2 = транспортные ошибки (нет SSE, нет auth, etc.)

ВАЖНО: тест предполагает выгрузку Котировок в /data/1c-src. Если её нет
или индексер не отработал — будет failed с понятным сообщением, но не crash.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

try:
    from mcp.client.sse import sse_client
    from mcp import ClientSession
except ImportError:
    print("ERROR: пакет mcp[cli] не установлен.")
    print("В среде smoke-runner устанавливается из requirements.txt.")
    sys.exit(2)


# ─── Конфигурация подключения ─────────────────────────────────────────────

def _resolve_url(local: bool) -> str:
    """Возвращает URL SSE-эндпоинта metadata-graph."""
    if local or os.environ.get("MCP_LOCAL"):
        return os.environ.get("METADATA_GRAPH_URL", "http://localhost:8001/sse")
    # В docker network (smoke-runner ходит по docker-DNS)
    return os.environ.get("METADATA_GRAPH_URL", "http://mcp-metadata-graph:8001/sse")


def _resolve_headers() -> dict:
    secret = os.environ.get("MCP_SHARED_SECRET", "")
    if secret:
        return {"Authorization": f"Bearer {secret}"}
    return {}


# ─── Один кейс ────────────────────────────────────────────────────────────

class Case:
    def __init__(self, name: str, tool: str, args: dict, check):
        self.name  = name
        self.tool  = tool
        self.args  = args
        self.check = check    # callable(parsed_json) → (passed: bool, note: str)


def _parse_tool_result(result) -> Any:
    """Берём из tool-результата text-блок и парсим как JSON."""
    if hasattr(result, "isError") and result.isError:
        return {"_error": "tool returned isError=true",
                "content": [{"type": c.type, "text": getattr(c, "text", "")}
                            for c in (result.content or [])]}
    text = ""
    for c in (result.content or []):
        if getattr(c, "type", None) == "text":
            text = c.text
            break
    if not text:
        return {"_error": "пустой текст ответа"}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        return {"_error": f"не-JSON ответ: {e}", "raw_head": text[:200]}


# ─── Чек-функции ──────────────────────────────────────────────────────────

def check_v3_stats(d):
    if "_error" in d:
        return False, f"tool error: {d['_error']}"
    nodes = d.get("nodes", {})
    if nodes.get("Attribute", 0) <= 0:
        return False, f"Attribute узлов: {nodes.get('Attribute', 0)} (ожидаем >0). " \
                      "Индексер v3 не отработал?"
    if nodes.get("Type", 0) <= 0:
        return False, f"Type узлов: {nodes.get('Type', 0)} (ожидаем >0)"
    if d.get("schema_version") != "v3":
        return False, f"schema_version={d.get('schema_version')} (ожидаем v3)"
    return True, (f"Attr={nodes.get('Attribute')}, Type={nodes.get('Type')}, "
                  f"Form={nodes.get('Form')}, EnumValue={nodes.get('EnumValue')}")


def check_attribute_type_audit(d):
    if not d.get("found"):
        return False, f"объект/реквизит не найден: {d.get('error')}"
    types = d.get("types", [])
    if not types:
        return False, "у реквизита нет типов"
    catref = [t for t in types if t.get("kind") == "CatalogRef"]
    if not catref:
        return False, f"среди типов нет CatalogRef: {types}"
    if "АукВидыАукционов" not in (catref[0].get("target") or ""):
        return False, f"target не указывает на АукВидыАукционов: {catref[0]}"
    return True, f"типы: {[(t['kind'], t.get('target')) for t in types]}"


def check_referrers(d):
    if "error" in d:
        return False, d["error"]
    items = d.get("items", [])
    if not items:
        return False, "items пуст — никто на АукВидыАукционов не ссылается?"
    has_main = any(
        i.get("owner_object", "").endswith("АукАукционы") and
        i.get("attr_name") == "ВидАукциона"
        for i in items
    )
    if not has_main:
        return False, f"не найдено ребро АукАукционы.ВидАукциона. items={items[:3]}"
    return True, f"найдено {len(items)} ссылающихся реквизитов"


def check_find_link_path(d):
    if not d.get("found"):
        return False, d.get("hint", "путь не найден")
    if d.get("paths_count", 0) < 1:
        return False, "путей 0"
    return True, f"путей: {d.get('paths_count')}; первый длиной {d['paths'][0]['length']}"


def check_register_attrs(d):
    if "error" in d:
        return False, d["error"]
    direct = d.get("direct_attributes", [])
    if not direct:
        return False, "у регистра нет direct_attributes"
    roles = {a.get("role") for a in direct}
    if "dimension" not in roles:
        return False, f"среди ролей нет 'dimension': {roles}"
    if "resource" not in roles:
        return False, f"среди ролей нет 'resource': {roles}"
    return True, f"роли: {sorted(roles)}, всего {len(direct)} реквизитов"


def check_subsystem_tree(d):
    if d.get("count", 0) < 1:
        return False, f"подсистем 0"
    return True, f"найдено {d['count']} элементов поддерева"


CASES = [
    Case("metadata_v3_stats — расширенная схема ушла от v2",
         "metadata_v3_stats", {},
         check_v3_stats),
    Case("metadata_attribute_type — Catalog.АукАукционы.ВидАукциона → CatalogRef",
         "metadata_attribute_type",
         {"object_full_name": "Catalog.АукАукционы", "attribute_name": "ВидАукциона"},
         check_attribute_type_audit),
    Case("metadata_referrers — кто ссылается на АукВидыАукционов",
         "metadata_referrers",
         {"object_full_name": "Catalog.АукВидыАукционов", "limit": 20},
         check_referrers),
    Case("metadata_find_link_path — путь между двумя справочниками",
         "metadata_find_link_path",
         {"from_object": "Catalog.АукАукционы",
          "to_object":   "Catalog.АукВидыАукционов", "max_depth": 4},
         check_find_link_path),
    Case("metadata_object_attributes — регистр имеет Dimension и Resource",
         "metadata_object_attributes",
         {"object_full_name": "InformationRegister.АукШаблоныСообщений"},
         check_register_attrs),
    Case("metadata_subsystem_tree — корневые подсистемы видны",
         "metadata_subsystem_tree", {},
         check_subsystem_tree),
]


# ─── Раннер ───────────────────────────────────────────────────────────────

async def run_case(session, c: Case) -> dict:
    try:
        result = await session.call_tool(c.tool, c.args)
    except Exception as e:
        return {"name": c.name, "passed": False,
                "note": f"transport: {type(e).__name__}: {e}",
                "transport_error": True}
    parsed = _parse_tool_result(result)
    passed, note = c.check(parsed)
    return {"name": c.name, "tool": c.tool, "passed": passed, "note": note,
            "transport_error": False}


async def main_async(url: str, headers: dict) -> int:
    try:
        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                results = []
                for c in CASES:
                    r = await run_case(session, c)
                    results.append(r)
    except Exception as e:
        print(f"FATAL: не удалось подключиться к {url}: {type(e).__name__}: {e}")
        return 2

    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed
    transport_err = any(r.get("transport_error") for r in results)

    print()
    print("=" * 70)
    print(f"Smoke v3 metadata graph — {passed}/{len(results)} passed")
    print("=" * 70)
    for r in results:
        mark = "✅" if r["passed"] else "❌"
        print(f"{mark} {r['name']}")
        print(f"   {r['note']}")
    print()

    if transport_err:
        return 2
    return 0 if failed == 0 else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true",
                        help="обращаться к localhost:8001 (для разработки)")
    args = parser.parse_args()
    url = _resolve_url(args.local)
    headers = _resolve_headers()
    print(f"Target: {url}")
    print(f"Auth:   {'Bearer' if headers else '(none)'}")
    sys.exit(asyncio.run(main_async(url, headers)))


if __name__ == "__main__":
    main()
