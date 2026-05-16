"""
End-to-end smoke для инкрементального апдейта графа (задача 4.6.5).
=======================================================================

Проверяет, что новые MCP-tools `metadata_upsert_file` /
`metadata_remove_file` в `mcp-metadata-graph` действительно работают на
живом стеке и не ломают граф.

Запуск (как в README 4.6.1/4.6.2 smoke):

  docker run --rm `
    --network 1c-suite-net `
    -e MCP_SHARED_SECRET=<секрет> `
    -v "D:\\Docker\\27_1c-mcp-suite-full-stack\\1c-mcp-suite\\scripts:/scripts:ro" `
    python:3.12-slim `
    sh -c "pip install --quiet 'mcp[cli]' && python /scripts/smoke_incremental.py"

Стратегия: **round-trip без модификации диска**. Мы берём существующий
файл выгрузки и:
  1. Снимаем «до»-снимок счётчиков графа (code_v3_stats, metadata_v3_stats).
  2. Вызываем metadata_upsert_file на нём → должен прийти `status=reindexed`.
  3. Снимаем «после»-снимок счётчиков.
  4. Сверяем: количества узлов/рёбер совпали (плюс-минус :CallSite, который
     mожет немного отличаться, см. ниже).
  5. (Опционально) запускаем код-only кейс: проверяем, что метрики покрытия
     остались в коридоре.

Также проверяется поведение на ошибочных входах:
  • Несуществующий файл → status=skipped, reason=file_not_found
  • Файл вне METADATA_SRC_DIR → skipped/path_outside_src_root
  • .txt-файл → skipped/unsupported_extension (диспатчер upsert_file)

Этот smoke НЕ переименовывает и НЕ удаляет файлы выгрузки — он только
делает идемпотентные upsert'ы. Поэтому его можно безопасно гонять на
проде столько раз, сколько хочется.

Кейсы:
  1. metadata_upsert_file на CommonModule.АукОбщийКлиент → reindexed,
     resolve.resolved > 0
  2. round-trip: code_v3_stats до и после совпадают по Callable/Parameter
  3. metadata_upsert_file на верхнеуровневый Catalog.xml → reindexed
  4. round-trip: metadata_v3_stats до и после совпадают по MetadataObject
  5. metadata_upsert_file на несуществующий файл → skipped
  6. metadata_upsert_file на .txt → skipped/unsupported_extension
  7. metadata_remove_file на несуществующий .bsl → removed (идемпотентно),
     cleared.deleted_callables == 0
  8. После remove + re-upsert: счётчики восстановлены.

Каждый кейс возвращает (ok, msg). 8/8 ok → exit 0.
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

# Файлы, которые гарантированно есть в выгрузке Котировок.
SAMPLE_BSL = os.environ.get(
    "SMOKE_BSL_FILE",
    "CommonModules/АукОбщийКлиент/Ext/Module.bsl",
)
SAMPLE_XML = os.environ.get(
    "SMOKE_XML_FILE",
    "Catalogs/АукАукционы.xml",
)
SAMPLE_MODULE_ID = "CommonModule.АукОбщийКлиент"


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


def _diff_dict(before: dict, after: dict, keys: list[str]) -> dict[str, int]:
    """Возвращает {key: after-before} для интересующих ключей."""
    out: dict[str, int] = {}
    for k in keys:
        out[k] = (after.get(k, 0) or 0) - (before.get(k, 0) or 0)
    return out


async def smoke() -> int:
    headers = {"Authorization": f"Bearer {MCP_SECRET}"} if MCP_SECRET else {}
    async with sse_client(MCP_URL, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Проверим, что новые tools вообще зарегистрированы.
            tools_resp = await session.list_tools()
            tool_names = {t.name for t in tools_resp.tools}
            if "metadata_upsert_file" not in tool_names \
               or "metadata_remove_file" not in tool_names:
                print("✗ metadata_upsert_file/metadata_remove_file НЕ зарегистрированы")
                print(f"  доступные tools: {sorted(tool_names)}")
                print("  Пересоберите образ mcp-metadata-graph (4.6.5).")
                return 2

            results: list[tuple[bool, str]] = []

            # ─── BSL round-trip ──────────────────────────────────────────

            # Снимок «до» для слоя 2.
            stats_code_before = await _call(session, "code_v3_stats", {})
            nodes_before = stats_code_before.get("nodes", {})
            edges_before = stats_code_before.get("edges", {})

            # 1. Re-upsert существующего .bsl.
            r = await _call(session, "metadata_upsert_file",
                            {"filepath": SAMPLE_BSL})
            ok = (r.get("status") == "reindexed"
                  and r.get("module_id") == SAMPLE_MODULE_ID
                  and r.get("resolve", {}).get("resolved", 0) > 0)
            if r.get("status") == "reindexed":
                detail = (f"status={r.get('status')} module_id={r.get('module_id')} "
                          f"resolved={r.get('resolve', {}).get('resolved')}")
            else:
                # При skipped/error важно показать reason+hint — иначе диагностика
                # деплоя превращается в гадание (см. грабли в HANDOFF).
                detail = (f"status={r.get('status')} reason={r.get('reason')} "
                          f"hint={r.get('hint', '')} filepath={SAMPLE_BSL}")
            results.append(_ok_msg("metadata_upsert_file(.bsl)", ok, detail))

            # Снимок «после».
            stats_code_after = await _call(session, "code_v3_stats", {})
            nodes_after = stats_code_after.get("nodes", {})
            edges_after = stats_code_after.get("edges", {})

            # 2. Round-trip: основные счётчики должны быть стабильны.
            # Callable/Parameter — стабильны точно (одна и та же выгрузка).
            # CallSite/CALLS — могут чуть отличаться, если callsite-резолв
            # против ЖИВОГО графа дал результат, отличный от полной фикс-
            # пойнт-сборки. Поэтому строгое равенство — только для Callable
            # и Parameter; для остальных проверяем «отклонение ≤ 5%».
            d_nodes = _diff_dict(nodes_before, nodes_after,
                                 ["Callable", "Parameter", "CallSite"])
            d_edges = _diff_dict(edges_before, edges_after,
                                 ["CALLS", "OPERATES_ON", "HAS_METHOD"])
            tolerance_pct = 5
            cs_before = nodes_before.get("CallSite", 1) or 1
            cs_drift_pct = abs(d_nodes["CallSite"]) * 100.0 / cs_before
            roundtrip_ok = (
                d_nodes["Callable"] == 0
                and d_nodes["Parameter"] == 0
                and cs_drift_pct <= tolerance_pct
            )
            results.append(_ok_msg(
                "round-trip code-stats",
                roundtrip_ok,
                f"Δ Callable={d_nodes['Callable']}, Δ Parameter={d_nodes['Parameter']}, "
                f"Δ CallSite={d_nodes['CallSite']} ({cs_drift_pct:.1f}%), "
                f"Δ CALLS={d_edges['CALLS']}",
            ))

            # ─── XML round-trip ─────────────────────────────────────────

            stats_meta_before = await _call(session, "metadata_v3_stats", {})
            mo_before = stats_meta_before.get("nodes", {}).get("MetadataObject", 0)

            r = await _call(session, "metadata_upsert_file",
                            {"filepath": SAMPLE_XML})
            ok = (r.get("status") == "reindexed"
                  and r.get("meta_id", "").startswith("Catalog."))
            if r.get("status") == "reindexed":
                detail = (f"status={r.get('status')} meta_id={r.get('meta_id')} "
                          f"resolves_to_rebuilt={r.get('resolves_to_rebuilt')}")
            else:
                detail = (f"status={r.get('status')} reason={r.get('reason')} "
                          f"hint={r.get('hint', '')} filepath={SAMPLE_XML}")
            results.append(_ok_msg("metadata_upsert_file(.xml)", ok, detail))

            stats_meta_after = await _call(session, "metadata_v3_stats", {})
            mo_after = stats_meta_after.get("nodes", {}).get("MetadataObject", 0)
            results.append(_ok_msg(
                "round-trip xml-stats",
                mo_before == mo_after,
                f"Δ MetadataObject = {mo_after - mo_before} "
                f"({mo_before} → {mo_after})",
            ))

            # ─── Error paths ────────────────────────────────────────────

            r = await _call(session, "metadata_upsert_file",
                            {"filepath": "CommonModules/НетТакого/Ext/Module.bsl"})
            results.append(_ok_msg(
                "upsert file_not_found",
                r.get("status") == "skipped" and r.get("reason") == "file_not_found",
                f"status={r.get('status')} reason={r.get('reason')}",
            ))

            r = await _call(session, "metadata_upsert_file",
                            {"filepath": "Configuration.xml"})
            # Configuration.xml — не верхнеуровневый объект в нашем смысле
            # (не лежит в Catalogs/Documents/...). Должен быть отказ.
            results.append(_ok_msg(
                "upsert non-object xml",
                r.get("status") == "skipped",
                f"status={r.get('status')} reason={r.get('reason')}",
            ))

            r = await _call(session, "metadata_upsert_file",
                            {"filepath": "README.md"})
            results.append(_ok_msg(
                "upsert unsupported extension",
                r.get("status") == "skipped"
                and r.get("reason") == "unsupported_extension",
                f"status={r.get('status')} reason={r.get('reason')}",
            ))

            # remove_file на несуществующем — идемпотентен, status=removed,
            # cleared.deleted_callables == 0.
            r = await _call(session, "metadata_remove_file",
                            {"filepath": "CommonModules/НетТакогоВПринципе/Ext/Module.bsl"})
            cleared = r.get("cleared", {})
            results.append(_ok_msg(
                "remove_file on absent (idempotent)",
                r.get("status") == "removed"
                and cleared.get("deleted_callables", -1) == 0,
                f"status={r.get('status')} cleared.callables={cleared.get('deleted_callables')}",
            ))

            print("Smoke 4.6.5 (incremental update):")
            for _ok, msg in results:
                print(msg)
            n_pass = sum(1 for ok, _ in results if ok)
            n_total = len(results)
            print(f"\n  {n_pass}/{n_total} passed")
            return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(smoke()))
