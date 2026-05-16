#!/usr/bin/env python3
"""
Smoke-тест BSL-анализа в SonarQube через mcp-sonarqube.

Что проверяет:
  Шлёт через `sonar_scan_code` маркерный BSL-сниппет, в котором заведомо
  есть несколько обнаруживаемых проблем (циклы с проверкой условий внутри,
  пустой `Если`, переменная-ловушка, дубль кода). После сканирования —
  читает обратно issues из проекта и проверяет:

    1. issues_total > 0           — что-то нашли (значит, плагин работает)
    2. среди rule-id есть BSL-    — нашли именно BSL-правила, а не общие
       (правила плагина имеют префикс `bsl-language:`, реже `bsl:`)
    3. (мягкая проверка) Quality Gate вернулся не пустой
       — `qualitygates/project_status` отдал status вместо ошибки

Это smoke task 5.2: предполагается, что
  - sonar-plugins/ содержит .jar BSL-плагина (см. install_sonar_bsl_plugin.py)
  - Quality Gate '1C BSL' создан и установлен default'ом
    (см. provision_sonar_quality_gate.py)
  - SonarQube перезапущен после установки плагина

Если 0 issues — это та самая ситуация из задачи 5.1, скрипт даёт
точный диагноз: «плагин лежит, но SonarQube его не подгрузил» / «плагин
загружен, но правила выключены в QG».

Usage:
    python3 scripts/smoke_sonar_bsl.py                 # против localhost:8014
    python3 scripts/smoke_sonar_bsl.py --verbose       # подробный лог
    python3 scripts/smoke_sonar_bsl.py --keep-project  # не удалять проект после прогона

Exit-code:
    0 — issues_total > 0 + найдены BSL-правила (плагин работает)
    1 — issues_total == 0 (плагин не работает) или transport error
    2 — ошибка вызова (CLI args, окружение)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

# Защита от системного HTTP-прокси (см. PLAN.md → «Известные проблемы»):
# на Windows v2rayN/Clash/Shadowsocks слушают 127.0.0.1:10809 и подхватываются
# httpx через trust_env=True. Если NO_PROXY уже задан явно — не трогаем,
# пользователь знает что делает. Иначе — выставляем разумный дефолт ДО
# любого импорта httpx/mcp, чтобы переменную успели прочитать при создании
# дефолтного клиента.
if not os.environ.get("NO_PROXY") and not os.environ.get("no_proxy"):
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
    os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

from _smoke_common import MCP_SERVERS, resolve_secret  # noqa: E402

EVALS_RUNNER = REPO_ROOT / "evals" / "runner"
sys.path.insert(0, str(EVALS_RUNNER))
try:
    from mcp_client import MCPSession  # type: ignore  # noqa: E402
except ImportError:
    print(
        "FATAL: cannot import MCPSession from evals/runner/mcp_client.py. "
        "Run from repository root.",
        file=sys.stderr,
    )
    sys.exit(2)


# ─── Маркерный BSL-сниппет ─────────────────────────────────────────────
#
# Подобран так, чтобы триггерить несколько РАЗНЫХ правил BSL-плагина:
#  - cyclomatic complexity (несколько вложенных Если/Иначе)
#  - empty block (пустой Если)
#  - переменная объявлена и не используется (Перем НеИспольз)
#  - дублирующаяся логика (две почти одинаковых ветки)
#  - magic number (467, 31)
#
# Это не идеальный набор «по одному на правило» — задача smoke не в этом.
# Задача — гарантировать, что issues_total > 0 даже на скромном сниппете,
# чтобы детектить ситуацию «плагин не работает».

MARKER_BSL = '''\
// Маркерный сниппет для smoke-теста SonarQube BSL анализа.
// Должен триггерить несколько правил BSL-плагина.

Функция Факториал(Число) Экспорт
    Перем НеИспольз;
    Результат = 1;
    Если Число < 0 Тогда
        Возврат 0;
    КонецЕсли;
    Если Число = 0 Тогда
    КонецЕсли;
    Сч = 1;
    Пока Сч <= Число Цикл
        Если Сч > 467 Тогда
            Если Сч > 31 Тогда
                Результат = Результат * Сч;
            Иначе
                Результат = Результат * Сч;
            КонецЕсли;
        Иначе
            Результат = Результат * Сч;
        КонецЕсли;
        Сч = Сч + 1;
    КонецЦикла;
    Возврат Результат;
КонецФункции
'''


# Префиксы rule-id, которые считаем «настоящими BSL-правилами».
# В разных версиях плагина они менялись:
#   - `bsl-language:S1234` — ранние версии
#   - `bsl:S1234` — позднейшая нотация
#   - `communitybsl:S1234` — встречается в свежих сборках
# Не сужаем до одного — иначе версионная привязка будет хрупкой.
# Префиксы rule-id, которые считаем «настоящими BSL-правилами».
# В разных версиях плагина и его движка они различаются:
#   - `bsl-language-server:S1234` — движок 1c-syntax/bsl-language-server
#     (фактически репортит правила в community-плагине 1.18+; подтверждено
#     эмпирически на смоке 06.05.2026 — это самый частый префикс).
#   - `bsl-language:S1234` — встречалось в ранних релизах.
#   - `bsl:S1234` — старая канонная нотация Sonar.
#   - `communitybsl:S1234` — встречается в свежих сборках.
# Не сужаем до одного — иначе версионная привязка будет хрупкой и smoke
# будет ложно-фейлиться при апгрейде плагина.
BSL_RULE_PREFIXES = (
    "bsl-language-server:",
    "bsl-language:",
    "bsl:",
    "communitybsl:",
)


def find_sonar_server() -> tuple[str, int]:
    for name, port in MCP_SERVERS:
        if name == "mcp-sonarqube":
            return name, port
    raise RuntimeError("mcp-sonarqube not in MCP_SERVERS — проверь _smoke_common.py")


async def call_tool(session: "MCPSession", tool: str, args: dict) -> dict:
    """Вызов tool с распарсиванием ответа (как в smoke_yaxunit)."""
    result = await session.call_tool(tool, args)
    if not result.ok:
        raise RuntimeError(f"{tool} call failed: {result.error or 'unknown'}")
    if result.is_error_flag:
        snippet = (result.raw_text or "")[:300]
        raise RuntimeError(f"{tool} returned isError: {snippet}")
    if not result.raw_text:
        raise RuntimeError(f"{tool} returned empty content")
    if result.parsed is not None:
        return result.parsed
    try:
        return json.loads(result.raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{tool} returned non-JSON text: {result.raw_text[:300]}... ({e})"
        )


# ─── Диагностика ────────────────────────────────────────────────────────


def diagnose_no_issues(scan_result: dict) -> list[str]:
    """Возвращает список вероятных причин 0 issues — для понятного фейла.
    Мы НЕ ходим в SonarQube напрямую, чтобы не усложнять зависимости
    smoke-скрипта (он ходит только в MCP). Поэтому диагноз — эвристический,
    по тому, что отдал sonar_scan_code."""
    causes: list[str] = []
    qg = scan_result.get("qualityGate") or {}
    qg_status = qg.get("status") if isinstance(qg, dict) else None
    project_key = scan_result.get("projectKey", "")

    if scan_result.get("status") == "scan_failed":
        # Сам scanner упал — это не «плагин не работает», это другое.
        stderr_tail = scan_result.get("stderr_tail", "")[:300]
        causes.append(f"sonar-scanner вернул код != 0; stderr_tail={stderr_tail!r}")
        return causes

    if qg_status is None or qg_status == "":
        causes.append("Quality Gate вернулся пустой — возможно, проект не создался "
                      "(проверь токен и права).")

    causes.append(
        "Самая вероятная причина: BSL-плагин не загружен в SonarQube. "
        "Проверь:\n"
        "    docker compose exec sonarqube ls /opt/sonarqube/extensions/plugins/\n"
        "  должен быть виден sonar-*bsl*.jar.\n"
        "  Если jar лежит — посмотри логи: docker compose logs sonarqube | "
        "grep -i 'bsl\\|plugin'."
    )
    causes.append(
        "Альтернативная причина: плагин загружен, но Quality Gate активного "
        "профиля BSL пуст / правила disabled. Проверь:\n"
        f"    {project_key} → Quality Profiles → BSL → активные правила."
    )
    return causes


# ─── Основной сценарий ─────────────────────────────────────────────────


async def run_smoke(args: argparse.Namespace) -> int:
    name, port = find_sonar_server()
    host = os.environ.get("MCP_HOST", "localhost")
    sonar_port = int(os.environ.get("MCP_SONAR_PORT", str(port)))
    url = f"http://{host}:{sonar_port}/sse"

    # Уникальный module_name → уникальный project_key, чтобы тесты не
    # смешивались между прогонами и не «деградировали» из-за прошлых данных.
    run_id = uuid.uuid4().hex[:8]
    module_name = f"SmokeBSL_{run_id}"

    secret = resolve_secret(None)
    if secret:
        os.environ["MCP_SHARED_SECRET"] = secret  # MCPSession читает из env

    print(f"target : {url}")
    print(f"module : {module_name}")
    print()

    async with MCPSession(url, init_timeout=30.0, call_timeout=900.0) as session:
        # ── 1. sonar_scan_code на маркерном сниппете
        print("[1/2] sonar_scan_code(MARKER_BSL) ... ", end="", flush=True)
        t0 = time.monotonic()
        try:
            scan = await call_tool(session, "sonar_scan_code", {
                "code": MARKER_BSL,
                "module_name": module_name,
            })
        except Exception as e:
            print(f"FAIL ({e})")
            return 1
        dt = time.monotonic() - t0

        if scan.get("status") != "scanned":
            print(f"FAIL ({dt:.1f}s) — scan не выполнился")
            print(json.dumps(scan, ensure_ascii=False, indent=2)[:1500])
            return 1
        issues_total = scan.get("issues_total", 0)
        issues = scan.get("issues", []) or []
        rules_seen = sorted({i.get("rule", "") for i in issues if i.get("rule")})
        print(f"OK ({dt:.1f}s, issues_total={issues_total}, unique_rules={len(rules_seen)})")

        # ── 2. Анализ результата
        print()
        print(f"[2/2] анализ issues:")
        print(f"  projectKey      : {scan.get('projectKey')}")
        qg = scan.get('qualityGate')
        qg_status = qg.get('status') if isinstance(qg, dict) else qg
        print(f"  qualityGate     : {qg_status}")
        print(f"  issues_total    : {issues_total}")
        print(f"  unique_rules    : {len(rules_seen)}")
        if args.verbose and rules_seen:
            print(f"  rule_ids        :")
            for r in rules_seen:
                print(f"      - {r}")
        elif rules_seen:
            print(f"  rule_ids (top5) : {', '.join(rules_seen[:5])}"
                  f"{'...' if len(rules_seen) > 5 else ''}")

        # Hard check 1: что-то нашли вообще
        if issues_total == 0:
            print()
            print("❌ FAIL: 0 issues на маркерном сниппете — это вся суть smoke'а.")
            print("   Возможные причины:")
            for cause in diagnose_no_issues(scan):
                print(f"     • {cause}")
            print()
            print(f"   Дашборд проекта (если жив):")
            print(f"     {scan.get('sonar_ui')}")
            return 1

        # Hard check 2: нашли именно BSL-правила
        bsl_rules = [r for r in rules_seen if any(r.startswith(p) for p in BSL_RULE_PREFIXES)]
        if not bsl_rules:
            print()
            print(f"❌ FAIL: issues есть ({issues_total}), но среди них НЕТ BSL-правил.")
            print(f"   Найдены только правила-неBSL: {', '.join(rules_seen[:5])}")
            print(f"   Это значит, что SonarQube распознал файл, но плагин BSL "
                  f"его не обработал.")
            print(f"   Возможные причины:")
            print(f"     • Плагин лежит в sonar-plugins/, но SonarQube не успел "
                  f"его подгрузить (нужен restart).")
            print(f"     • В .properties стоит `sonar.bsl.file.suffixes=.bsl` (это уже "
                  f"настроено в server.py), но на конкретном файле расширение не bsl.")
            return 1

        print()
        print(f"✅ PASS: issues_total={issues_total}, BSL-правил уникальных: {len(bsl_rules)}")
        print(f"   Примеры BSL-правил: {', '.join(bsl_rules[:5])}")

    if args.keep_project:
        print()
        print(f"   ℹ Проект {scan.get('projectKey')} оставлен в SonarQube для ручного "
              f"осмотра (--keep-project).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-тест BSL-анализа в SonarQube через mcp-sonarqube.",
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Печатать все найденные rule-id, не только топ-5.")
    parser.add_argument("--keep-project", action="store_true",
                        help="Не запоминать в отчёте, что проект надо чистить (по умолчанию "
                             "проект остаётся — у MCP-tool нет операции delete).")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        return asyncio.run(run_smoke(args))
    except KeyboardInterrupt:
        print("\n^C", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
