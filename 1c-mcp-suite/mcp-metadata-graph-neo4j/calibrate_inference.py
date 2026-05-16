"""Калибровка type inference (задача 4.6.4) на реальной Котировке.

Воспроизводимый замер «до/после» для расширенного dataflow + inter-procedural
propagation. В отличие от calibrate.py (тот меряет парсер), этот меряет РЕЗОЛВ:
coverage %, разбивку reason_counts и — главное — разбивку группы
`unknown_module` по природе module_ref (параметр / локал с присваиванием /
локал без присваивания). См. PLAN_4_6_4.md раздел 2.

Запуск:
    python calibrate_inference.py /path/to/workspace

Внимание: без живой Neo4j индекс собирается по именам папок
(metadata_objects неполон, common_module_props пуст), поэтому абсолютные
числа НИЖЕ стендовых. Значимы пропорции и дельта «до/после».
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from bsl_parser import walk_workspace_bsl, iter_calls
from bsl_resolver import (
    build_index_from_modules, build_call_graph, infer_local_types,
)


# Имя переменной/идентификатора BSL — для поиска присваиваний и параметров.
_RE_ASSIGN_LHS = re.compile(
    r'(?<![А-Яа-яЁёA-Za-z0-9_.])'
    r'(?P<var>[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z0-9_]*)\s*='
    r'(?![=<>])'  # не ==, не <=, не >=
)


def _assigned_vars(body_text: str) -> set[str]:
    """Множество имён переменных, которым в теле процедуры что-то присваивается
    (любым выражением, не только Plural.X.Method)."""
    return {m.group("var") for m in _RE_ASSIGN_LHS.finditer(body_text)}


def classify_unknown_module(proc, module_ref: str, typed_vars: set[str]) -> str:
    """Классифицирует природу module_ref у unresolved-callsite'а с reason=unknown_module.

    Группы (по PLAN_4_6_4.md раздел 2):
      • 'param'                — module_ref это имя параметра caller-процедуры
      • 'local_untyped_assign' — module_ref это локал, которому что-то присваивается,
                                 но тип не выведен
      • 'no_assignment'        — module_ref без присваивания в теле (глобал / неявный
                                 ЭтотОбъект / длинная цепочка) — статически нерешаемо
    """
    param_names = {p.name for p in proc.parameters}
    if module_ref in param_names:
        return "param"
    assigned = _assigned_vars(proc.body_text)
    if module_ref in assigned and module_ref not in typed_vars:
        return "local_untyped_assign"
    if module_ref in assigned:
        # присвоен И типизирован, но всё равно unknown_module — редкий случай
        # (например тип выведен, но KIND_TO_MODULE_ROLE его не знает). Считаем
        # как local_untyped_assign — он «достижим» при усилении dataflow.
        return "local_untyped_assign"
    return "no_assignment"


def measure(root: Path) -> dict:
    """Один полный замер. Возвращает dict с метриками."""
    modules = walk_workspace_bsl(root)
    index = build_index_from_modules(modules)
    cg = build_call_graph(modules, index)
    stats = cg["stats"]

    resolved = stats["resolved"]
    unresolved = stats["unresolved"]
    total = resolved + unresolved
    coverage = (100.0 * resolved / total) if total else 0.0

    # Разбивка unknown_module по группам. Нужен повторный проход по callsite'ам:
    # для каждого unresolved с reason=unknown_module находим proc и module_ref.
    proc_by_caller: dict[str, object] = {}
    for m in modules:
        for proc in m.procedures:
            proc_by_caller[f"{m.module_id}.{proc.name}"] = proc

    # Кэш typed_vars по caller_id (infer_local_types может быть дорогим).
    typed_cache: dict[str, set[str]] = {}

    group_counts = {"param": 0, "local_untyped_assign": 0, "no_assignment": 0}
    for cs in cg["callsite_nodes"]:
        if cs["resolved"] or cs.get("reason") != "unknown_module":
            continue
        caller_id = cs["caller_id"]
        proc = proc_by_caller.get(caller_id)
        if proc is None:
            continue
        if caller_id not in typed_cache:
            typed_cache[caller_id] = set(infer_local_types(proc).keys())
        grp = classify_unknown_module(proc, cs["module_ref"], typed_cache[caller_id])
        group_counts[grp] += 1

    # :INFERRED_TYPE / :Type — появятся после этапа D.
    n_inferred_type = sum(1 for e in cg["edges"] if e["rel"] == "INFERRED_TYPE")
    n_type_nodes = len(cg.get("type_nodes", []))
    fixpoint_iterations = stats.get("fixpoint_iterations")

    return {
        "modules": len(modules),
        "callables": stats["callable_nodes"],
        "resolved": resolved,
        "unresolved": unresolved,
        "skipped": stats["skipped"],
        "coverage": coverage,
        "reason_counts": dict(stats["reason_counts"]),
        "unknown_module_groups": group_counts,
        "inferred_type_edges": n_inferred_type,
        "type_nodes": n_type_nodes,
        "fixpoint_iterations": fixpoint_iterations,
    }


def print_report(r: dict, baseline: dict | None = None) -> None:
    print(f"Модулей:    {r['modules']}")
    print(f"Callable:   {r['callables']}")
    print()
    print(f"resolved:   {r['resolved']}")
    print(f"unresolved: {r['unresolved']}")
    print(f"skipped:    {r['skipped']}")
    cov = r["coverage"]
    if baseline:
        delta = cov - baseline["coverage"]
        print(f"coverage:   {cov:.2f}%  (было {baseline['coverage']:.2f}%, "
              f"дельта {delta:+.2f} п.п.)")
    else:
        print(f"coverage:   {cov:.2f}%")
    print()
    print("reason_counts:")
    for reason, cnt in sorted(r["reason_counts"].items(), key=lambda x: -x[1]):
        print(f"  {reason:34} {cnt}")
    print()
    print("разбивка unknown_module по природе module_ref:")
    g = r["unknown_module_groups"]
    base_g = baseline["unknown_module_groups"] if baseline else {}
    for key, label in [
        ("param",                "параметр caller'а (inter-procedural)"),
        ("local_untyped_assign", "локал с присваиванием, тип не выведен"),
        ("no_assignment",        "без присваивания (статически нерешаемо)"),
    ]:
        cur = g.get(key, 0)
        if baseline:
            d = cur - base_g.get(key, 0)
            print(f"  {label:46} {cur:5}  ({d:+d})")
        else:
            print(f"  {label:46} {cur:5}")
    print()
    print(f":INFERRED_TYPE рёбер: {r['inferred_type_edges']}")
    print(f":Type-узлов слоя 2:   {r['type_nodes']}")
    if r["fixpoint_iterations"] is not None:
        print(f"итераций фикс-пойнта: {r['fixpoint_iterations']}")


def main(root: Path, baseline_path: Path | None = None,
         save_path: Path | None = None) -> None:
    print(f"Workspace: {root}")
    print("=" * 60)
    r = measure(root)

    baseline = None
    if baseline_path and baseline_path.exists():
        import json
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        print(f"(сравнение с baseline: {baseline_path.name})")
        print("=" * 60)

    print_report(r, baseline)

    if save_path:
        import json
        save_path.write_text(json.dumps(r, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print()
        print(f"Замер сохранён в {save_path}")


if __name__ == "__main__":
    # Использование:
    #   python calibrate_inference.py <workspace>
    #   python calibrate_inference.py <workspace> --save baseline.json
    #   python calibrate_inference.py <workspace> --baseline baseline.json
    args = sys.argv[1:]
    if not args:
        print("usage: python calibrate_inference.py <workspace> "
              "[--save FILE | --baseline FILE]")
        sys.exit(1)
    ws = Path(args[0]).resolve()
    baseline_p = None
    save_p = None
    if "--baseline" in args:
        baseline_p = Path(args[args.index("--baseline") + 1])
    if "--save" in args:
        save_p = Path(args[args.index("--save") + 1])
    main(ws, baseline_p, save_p)
