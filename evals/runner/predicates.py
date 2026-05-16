"""
Предикаты eval-runner'а.

Каждый тип предиката — отдельная функция `_pred_<type>(pred, result)`,
возвращающая `PredicateOutcome(passed, detail)`. `detail` идёт в JSON-отчёт
и помогает разобраться, ПОЧЕМУ предикат провалился.

Формат результата MCP-tool'а ожидается как dict с ключом `results`
(список хитов с полями `name_ru`, `name_en`, `full_name`, `kind` и т.д.)
— так отдают и `platform_help_search`, и `platform_help_lookup`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PredicateOutcome:
    """Результат одной проверки."""
    type: str
    passed: bool
    detail: dict[str, Any]
    match_rank: int | None = None


def _hits(result: Any) -> list[dict]:
    if not isinstance(result, dict):
        return []
    r = result.get("results")
    if isinstance(r, list):
        return [h for h in r if isinstance(h, dict)]
    return []


def _norm(s: Any) -> str:
    if not isinstance(s, str):
        return ""
    return s.strip().casefold()


def _pred_non_empty(pred: dict, result: Any) -> PredicateOutcome:
    hits = _hits(result)
    return PredicateOutcome(
        type="non_empty",
        passed=len(hits) > 0,
        detail={"hits_count": len(hits)},
    )


def _pred_results_count_at_least(pred: dict, result: Any) -> PredicateOutcome:
    min_n = int(pred.get("min", 1))
    hits = _hits(result)
    return PredicateOutcome(
        type="results_count_at_least",
        passed=len(hits) >= min_n,
        detail={"min": min_n, "actual": len(hits)},
    )


def _pred_name_in_top_k(pred: dict, result: Any) -> PredicateOutcome:
    k = max(1, int(pred.get("k", 5)))
    values = pred.get("values") or []
    if not isinstance(values, list) or not values:
        return PredicateOutcome(
            type="name_in_top_k",
            passed=False,
            detail={"error": "values must be non-empty list"},
        )
    wanted = {_norm(v) for v in values if isinstance(v, str)}

    hits = _hits(result)[:k]
    match_rank: int | None = None
    matched_name: str | None = None

    for idx, h in enumerate(hits, start=1):
        candidates = (
            _norm(h.get("name_ru")),
            _norm(h.get("name_en")),
            _norm(h.get("full_name")),
        )
        for c in candidates:
            if c and c in wanted:
                match_rank = idx
                matched_name = c
                break
        if match_rank is not None:
            break

    return PredicateOutcome(
        type="name_in_top_k",
        passed=match_rank is not None,
        detail={
            "k": k,
            "values": values,
            "match_rank": match_rank,
            "matched_name": matched_name,
            "hits_examined": len(hits),
        },
        match_rank=match_rank,
    )


def _pred_any_hit_kind(pred: dict, result: Any) -> PredicateOutcome:
    k = max(1, int(pred.get("k", 5)))
    kinds = pred.get("kinds") or []
    if not isinstance(kinds, list) or not kinds:
        return PredicateOutcome(
            type="any_hit_kind",
            passed=False,
            detail={"error": "kinds must be non-empty list"},
        )
    wanted = {_norm(v) for v in kinds if isinstance(v, str)}

    hits = _hits(result)[:k]
    found_kinds = [_norm(h.get("kind")) for h in hits]
    hit_idx = None
    for idx, k_found in enumerate(found_kinds, start=1):
        if k_found in wanted:
            hit_idx = idx
            break

    return PredicateOutcome(
        type="any_hit_kind",
        passed=hit_idx is not None,
        detail={
            "k": k,
            "kinds": kinds,
            "first_match_rank": hit_idx,
            "observed_kinds": found_kinds,
        },
    )


def _pred_full_name_contains(pred: dict, result: Any) -> PredicateOutcome:
    k = max(1, int(pred.get("k", 5)))
    substr = pred.get("substr") or ""
    if not isinstance(substr, str) or not substr:
        return PredicateOutcome(
            type="full_name_contains",
            passed=False,
            detail={"error": "substr must be non-empty string"},
        )
    needle = _norm(substr)

    hits = _hits(result)[:k]
    match_rank = None
    matched_full_name = None
    for idx, h in enumerate(hits, start=1):
        fn = _norm(h.get("full_name"))
        if needle in fn:
            match_rank = idx
            matched_full_name = h.get("full_name")
            break

    return PredicateOutcome(
        type="full_name_contains",
        passed=match_rank is not None,
        detail={
            "k": k,
            "substr": substr,
            "match_rank": match_rank,
            "matched_full_name": matched_full_name,
        },
    )


_HANDLERS = {
    "non_empty": _pred_non_empty,
    "results_count_at_least": _pred_results_count_at_least,
    "name_in_top_k": _pred_name_in_top_k,
    "any_hit_kind": _pred_any_hit_kind,
    "full_name_contains": _pred_full_name_contains,
}


def evaluate(pred: dict, result: Any) -> PredicateOutcome:
    t = pred.get("type", "")
    handler = _HANDLERS.get(t)
    if handler is None:
        return PredicateOutcome(
            type=t or "unknown",
            passed=False,
            detail={"error": "unknown_predicate_type", "raw": pred},
        )
    try:
        return handler(pred, result)
    except Exception as e:
        return PredicateOutcome(
            type=t,
            passed=False,
            detail={"error": f"{type(e).__name__}: {e}", "raw": pred},
        )
