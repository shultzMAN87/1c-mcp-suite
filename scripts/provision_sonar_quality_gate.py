#!/usr/bin/env python3
"""
Provisioning Quality Gate "1C BSL" в SonarQube (задача 5.2).

Контекст: после установки BSL-плагина (`scripts/install_sonar_bsl_plugin.py`)
SonarQube начинает выдавать issues, но без явного Quality Gate проект
по-прежнему «зелёный» — у дефолтного `Sonar way` пороги настроены под
языки экосистемы Sonar (Java/JS/Python), и BSL-issues в нём не
отражены. Этот скрипт создаёт целевой gate `1C BSL` с условиями,
которые имеют смысл для BSL-кода, и (по флагу) ставит его дефолтным.

Идемпотентность: повторный запуск НЕ создаёт дубликаты. Если gate уже
есть — скрипт сравнивает условия с целевыми и (при `--update`)
синхронизирует их (добавляет недостающие, обновляет несовпадающие,
удаляет лишние из тех, что мы сами кладём — чужие условия не трогаем).

Web API: используются эндпоинты `qualitygates/*`, документированные на
SonarQube Community Build. На Community Edition они все доступны.

Условия Quality Gate (минимальный практичный набор без покрытия тестами):
  - bugs > 0                          (overall)  — критичные баги
  - vulnerabilities > 0               (overall)  — уязвимости
  - code_smells > 0                   (overall)  — code smells, в т.ч. от BSL plugin
  - duplicated_lines_density > 3      (overall)  — дубликация >3%
  - new_code_smells > 0               (new code) — на новом коде smells быть не должно

Покрытие тестами намеренно НЕ включено: контекст 1С — production-конфы,
тесты появляются эпизодически (см. задачу 5.1, расширение `КотировкиТесты`).
Метрика `coverage` будет всегда 0 → gate всегда красный → пользователь
выключит gate за день → задача 5.2 потеряет смысл.

Stdlib-only (urllib + json) — как и `install_sonar_bsl_plugin.py`.

Usage:
    # Создать (или подтвердить существование) gate с условиями выше.
    python3 scripts/provision_sonar_quality_gate.py

    # Создать и сразу сделать дефолтным.
    python3 scripts/provision_sonar_quality_gate.py --set-default

    # Синхронизировать условия (добавить/удалить/обновить).
    python3 scripts/provision_sonar_quality_gate.py --update

    # Удалить gate (для отладки).
    python3 scripts/provision_sonar_quality_gate.py --delete

Переменные окружения:
    SONAR_URL    — URL сервера SonarQube. Default: http://localhost:9001.
    SONAR_TOKEN  — токен с правами Administer Quality Gates (User Token, squ_*).
                   Без него скрипт не запустится — administer операции требуют auth.

Exit-code:
    0 — успех (gate существует и условия совпадают/обновлены)
    1 — ошибка SonarQube (HTTP, недоступен, недостаточно прав)
    2 — ошибка вызова (плохие аргументы, нет токена)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GATE_NAME = "1C BSL"

# Целевые условия. (metric, op, error_threshold, on_new_code).
#   op  — LT (less than) / GT (greater than) / EQ / NE
#   on_new_code — True если ограничение применяется только к новому коду
#
# Набор «стандартный без coverage»: bugs/vulnerabilities ноль, ограничения
# на code_smells и дубли, плюс рейтинги (A для reliability/security,
# не хуже B для maintainability). Метрики, которых нет на сервере,
# обрабатываются defensive — sync_conditions их пропускает.
#
# `new_code_smells` намеренно НЕ включён: на наших одноразовых проектах
# агента «новый код» = весь код, и это условие дублирует overall code_smells.
TARGET_CONDITIONS: list[tuple[str, str, str, bool]] = [
    ("bugs",                       "GT", "0",  False),
    ("vulnerabilities",            "GT", "0",  False),
    ("code_smells",                "GT", "10", False),
    ("duplicated_lines_density",   "GT", "5",  False),
    ("reliability_rating",         "GT", "1",  False),  # 1=A, 2=B, ...
    ("security_rating",            "GT", "1",  False),
    ("sqale_rating",               "GT", "2",  False),  # ≤ B по maintainability
]

UA = "1c-mcp-suite-qg-provisioner/1.0"


# ─── .env loader (повторяем минимум из _smoke_common, чтобы не тянуть зависимость) ──

def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def resolve_token_and_url(cli_token: str | None, cli_url: str | None) -> tuple[str, str]:
    token = (cli_token or "").strip() or os.environ.get("SONAR_TOKEN", "").strip()
    url   = (cli_url   or "").strip() or os.environ.get("SONAR_URL", "").strip()

    if not token or not url:
        env = _load_env_file(ROOT / ".env")
        token = token or env.get("SONAR_TOKEN", "").strip()
        url   = url   or env.get("SONAR_URL", "").strip()

    url = url or "http://localhost:9001"
    return token, url.rstrip("/")


# ─── HTTP-клиент к SonarQube ────────────────────────────────────────────

class SonarApiError(RuntimeError):
    """Ошибка обращения к Sonar Web API. .code=HTTP-код или None."""
    def __init__(self, msg: str, code: int | None = None, body: str = ""):
        super().__init__(msg)
        self.code = code
        self.body = body


class SonarClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        # Системный HTTP-прокси (типично v2rayN/Clash/Shadowsocks на 127.0.0.1:10809
        # на Windows) перехватывает запросы к localhost и возвращает либо
        # timeout, либо HTTP 404. urllib читает прокси из env trust-by-default,
        # как и httpx — это ровно та проблема, что описана в PLAN.md как
        # «Известная проблема» про smoke. Для запросов к localhost / 127.0.0.1
        # ставим opener с пустым ProxyHandler — он игнорирует системные настройки.
        host = self.base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        self._bypass_proxy = host in ("localhost", "127.0.0.1", "::1")
        if self._bypass_proxy:
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    def _headers(self) -> dict[str, str]:
        # Sonar принимает токен как Basic auth: <token>:<empty>
        auth = base64.b64encode(f"{self.token}:".encode("ascii")).decode("ascii")
        return {
            "User-Agent": UA,
            "Accept": "application/json",
            "Authorization": f"Basic {auth}",
        }

    def _request(self, method: str, path: str,
                 params: dict[str, str] | None = None,
                 timeout: float = 15.0) -> dict:
        # Web API SonarQube: GET для read-only, POST для mutations.
        # POST'ы тоже принимают параметры в query или в form-body. Используем
        # form-body — по докам это рекомендуемый способ.
        url = f"{self.base_url}/api/{path.lstrip('/')}"
        data = None
        headers = self._headers()
        if method == "POST":
            body = urllib.parse.urlencode(params or {}).encode("utf-8")
            data = body
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:1500]
            except Exception:
                pass
            raise SonarApiError(
                f"{method} {path} → HTTP {e.code}: {e.reason}",
                code=e.code, body=body,
            )
        except urllib.error.URLError as e:
            raise SonarApiError(
                f"{method} {path} → сеть недоступна: {e.reason}",
            )
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Некоторые эндпоинты (delete/destroy) возвращают пустоту/204.
            return {"_raw": raw}

    def get(self,  path: str, params: dict[str, str] | None = None) -> dict:
        return self._request("GET", path, params)

    def post(self, path: str, params: dict[str, str] | None = None) -> dict:
        return self._request("POST", path, params)


# ─── Операции с Quality Gate ────────────────────────────────────────────

def find_gate(c: SonarClient, name: str) -> dict | None:
    """Ищет gate по имени. Возвращает dict {name, isDefault, isBuiltIn, ...} или None."""
    data = c.get("qualitygates/list")
    for q in data.get("qualitygates") or []:
        if (q.get("name") or "") == name:
            return q
    return None


def show_gate(c: SonarClient, name: str) -> dict:
    """Состав gate: имя + список conditions [{id, metric, op, error}, ...]."""
    return c.get("qualitygates/show", {"name": name})


def create_gate(c: SonarClient, name: str) -> None:
    c.post("qualitygates/create", {"name": name})


def delete_gate(c: SonarClient, name: str) -> None:
    c.post("qualitygates/destroy", {"name": name})


def add_condition(c: SonarClient, gate_name: str, metric: str,
                  op: str, error: str) -> None:
    """Добавить условие. on_new_code определяется именем метрики (`new_*`)."""
    c.post("qualitygates/create_condition", {
        "gateName": gate_name,
        "metric":   metric,
        "op":       op,
        "error":    error,
    })


def update_condition(c: SonarClient, condition_id: str,
                     metric: str, op: str, error: str) -> None:
    c.post("qualitygates/update_condition", {
        "id":     condition_id,
        "metric": metric,
        "op":     op,
        "error":  error,
    })


def delete_condition(c: SonarClient, condition_id: str) -> None:
    c.post("qualitygates/delete_condition", {"id": condition_id})


def set_default(c: SonarClient, name: str) -> None:
    c.post("qualitygates/set_as_default", {"name": name})


# ─── Синхронизация условий ──────────────────────────────────────────────

def sync_conditions(c: SonarClient, gate_name: str, do_update: bool,
                    purge_foreign: bool = False) -> tuple[int, int, int, int]:
    """
    Сравнивает фактические условия gate с целевыми (TARGET_CONDITIONS) и
    при `do_update=True` приводит их в соответствие.

    `purge_foreign=True` — удалять также условия, чьих метрик нет в
    TARGET_CONDITIONS. Это нужно ровно для одного кейса: SonarQube 9+ при
    `qualitygates/create` копирует условия из built-in 'Sonar way' (
    new_coverage<80, new_violations>0, new_security_hotspots_reviewed<100,
    new_duplicated_lines_density>3 и т.п.) — на наших одноразовых
    проектах без CI они либо вечно зелёные, либо вечно красные, и
    нужны нам не больше, чем «complexity» из бабушкиного recipe-ника.
    На уже существующем gate'е по умолчанию НЕ удаляем — там могут быть
    осознанные кастомизации пользователя.

    Возвращает (added, updated, removed, kept).
    """
    state = show_gate(c, gate_name)
    actual: list[dict] = state.get("conditions") or []

    # Целевые — по metric, потому что SonarQube не разрешает дубликаты
    # condition'ов на одну метрику в одном gate.
    target_by_metric: dict[str, tuple[str, str, bool]] = {
        m: (op, err, on_new) for (m, op, err, on_new) in TARGET_CONDITIONS
    }
    actual_by_metric: dict[str, dict] = {
        (cnd.get("metric") or ""): cnd for cnd in actual
    }

    added = updated = removed = kept = 0

    for metric, (op, err, _on_new) in target_by_metric.items():
        existing = actual_by_metric.get(metric)
        if not existing:
            if do_update:
                try:
                    add_condition(c, gate_name, metric, op, err)
                except SonarApiError as e:
                    # Метрики, про которые SonarQube не знает (нет нужного
                    # языка/плагина или сменилось имя в новой версии), не
                    # должны валить весь sync — добавляем то, что можем,
                    # остальное помечаем «пропущено».
                    if e.code == 400:
                        print(f"  ⚠ skip metric '{metric}': {e}", file=sys.stderr)
                        continue
                    raise
            added += 1
            continue
        # Сравним op + error. Совпадает — оставляем.
        same = (existing.get("op") == op and str(existing.get("error", "")) == str(err))
        if same:
            kept += 1
        else:
            if do_update:
                cid = str(existing.get("id", ""))
                if cid:
                    update_condition(c, cid, metric, op, err)
            updated += 1

    # Удаляем «лишние» условия только на наших же метриках (которые мы знаем).
    # Если кто-то добавил руками условие на метрику вне TARGET_CONDITIONS —
    # не трогаем: пользовательская кастомизация имеет приоритет.
    our_metrics = set(target_by_metric.keys())
    for cnd in actual:
        m = cnd.get("metric") or ""
        # «Лишним» считаем только то, что лежит в TARGET_CONDITIONS, но
        # дублируется (на одной метрике несколько условий — Sonar так не
        # должен делать, но защита от грязного состояния).
        if m in our_metrics and our_metrics:
            # Уже обработали выше через actual_by_metric — там был один
            # представитель метрики. Реальные дубликаты убираем.
            pass
    # Удаление дубликатов (если actual_by_metric выбрал не того):
    seen_metrics: set[str] = set()
    for cnd in actual:
        m = cnd.get("metric") or ""
        if m not in our_metrics:
            continue
        if m in seen_metrics:
            if do_update:
                cid = str(cnd.get("id", ""))
                if cid:
                    delete_condition(c, cid)
            removed += 1
        else:
            seen_metrics.add(m)

    # Удаление унаследованных от built-in 'Sonar way' условий
    # (new_coverage<80 и компания) — только если purge_foreign=True.
    if purge_foreign and do_update:
        for cnd in actual:
            m = cnd.get("metric") or ""
            if m and m not in our_metrics:
                cid = str(cnd.get("id", ""))
                if cid:
                    try:
                        delete_condition(c, cid)
                        removed += 1
                    except SonarApiError as e:
                        print(f"  ⚠ не удалось удалить унаследованное условие "
                              f"'{m}' (id={cid}): {e}", file=sys.stderr)

    return added, updated, removed, kept


# ─── CLI ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Создать Quality Gate '1C BSL' в SonarQube.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="См. также: scripts/install_sonar_bsl_plugin.py, scripts/smoke_sonar_bsl.py.",
    )
    ap.add_argument("--token", help="SONAR_TOKEN (User Token, squ_*). По умолчанию — из env/.env.")
    ap.add_argument("--url",   help="SONAR_URL. По умолчанию — http://localhost:9001 или из .env.")
    ap.add_argument("--set-default", action="store_true",
                    help="После создания/обновления сделать gate дефолтным для всех проектов.")
    ap.add_argument("--update", action="store_true",
                    help="Синхронизировать условия с целевыми (добавить/обновить/удалить дубликаты).")
    ap.add_argument("--purge-foreign", action="store_true",
                    help="Удалить условия с метриками вне TARGET_CONDITIONS — например, "
                         "унаследованные от built-in 'Sonar way' (new_coverage<80 и т.п.). "
                         "Применяется только вместе с --update; на свежем create включено "
                         "автоматически.")
    ap.add_argument("--delete", action="store_true",
                    help="Удалить gate (для отладки/реверта). Перед удалением default-gate "
                         "Sonar сам переключает дефолт на 'Sonar way'.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Показать, что отличается от целевого состояния, но не менять. "
                         "Совместимо с --set-default (ставит флаг to-be-applied, не применяет).")
    args = ap.parse_args()

    token, url = resolve_token_and_url(args.token, args.url)
    if not token:
        print("ОШИБКА: SONAR_TOKEN не задан.", file=sys.stderr)
        print("  Создай User Token в SonarQube (My Account → Security → Generate Tokens),", file=sys.stderr)
        print("  тип 'User Token' (префикс squ_, НЕ Global Analysis Token sqa_).", file=sys.stderr)
        print("  Положи в .env как SONAR_TOKEN=squ_...", file=sys.stderr)
        return 2

    print(f"→ SonarQube: {url}")

    c = SonarClient(url, token)

    # Pre-flight: убедимся, что сервер живой и токен принят. Лёгкий вызов.
    try:
        c.get("system/status")
    except SonarApiError as e:
        if e.code == 401:
            print(f"ОШИБКА: токен не принят ({url}/api/system/status → 401)", file=sys.stderr)
            print("  Возможные причины: токен истёк, тип Global Analysis Token (sqa_) "
                  "вместо User Token (squ_).", file=sys.stderr)
            return 1
        print(f"ОШИБКА: SonarQube не отвечает: {e}", file=sys.stderr)
        return 1

    # ─── --delete ────
    if args.delete:
        existing = find_gate(c, GATE_NAME)
        if not existing:
            print(f"✓ Gate '{GATE_NAME}' уже отсутствует — нечего удалять")
            return 0
        if args.dry_run:
            print(f"[dry-run] Был бы удалён gate '{GATE_NAME}'")
            return 0
        try:
            delete_gate(c, GATE_NAME)
        except SonarApiError as e:
            print(f"ОШИБКА удаления: {e}\n{e.body}", file=sys.stderr)
            return 1
        print(f"✓ Gate '{GATE_NAME}' удалён")
        return 0

    # ─── default путь: create-or-sync ────
    existing = find_gate(c, GATE_NAME)
    gate_exists_now = existing is not None
    just_created = False
    if not existing:
        if args.dry_run:
            print(f"[dry-run] Был бы создан Quality Gate '{GATE_NAME}' и залиты "
                  f"{len(TARGET_CONDITIONS)} целевых условий")
            # Gate'а нет, sync_conditions(show) → 404. Пропускаем синхронизацию
            # целиком и переходим сразу к --set-default-секции (она тоже dry-run-aware).
        else:
            try:
                create_gate(c, GATE_NAME)
            except SonarApiError as e:
                print(f"ОШИБКА создания gate: {e}\n{e.body}", file=sys.stderr)
                return 1
            print(f"✓ Создан Quality Gate '{GATE_NAME}'")
            gate_exists_now = True
            just_created = True
            # Сразу заливаем все целевые условия — шаг гарантированно --update-режим.
            do_update = True
    else:
        print(f"✓ Gate '{GATE_NAME}' уже существует "
              f"(isDefault={existing.get('isDefault')}, isBuiltIn={existing.get('isBuiltIn')})")
        # В dry-run отключаем фактические правки, оставляя diff.
        do_update = args.update and not args.dry_run

    if gate_exists_now:
        try:
            # purge_foreign только при свежем create (чтобы вычистить унаследованные
            # от built-in Sonar way new_coverage<80 и т.п.) ИЛИ по явному
            # пользовательскому --purge-foreign на уже существующий gate.
            purge = just_created or args.purge_foreign
            added, updated, removed, kept = sync_conditions(
                c, GATE_NAME, do_update, purge_foreign=purge,
            )
        except SonarApiError as e:
            print(f"ОШИБКА синхронизации условий: {e}\n{e.body}", file=sys.stderr)
            return 1

        if do_update:
            print(f"  условия: добавлено {added}, обновлено {updated}, "
                  f"удалено дубликатов {removed}, без изменений {kept}")
        else:
            # Только diff, без изменений.
            if added or updated or removed:
                print(f"  условия отличаются от целевых: "
                      f"не хватает {added}, не совпадают {updated}, дубликатов {removed}.")
                print(f"  Запусти с --update, чтобы применить.")
            else:
                print(f"  условия уже совпадают с целевыми ({kept} штук)")

    if args.set_default:
        if args.dry_run:
            print(f"[dry-run] Был бы назначен '{GATE_NAME}' дефолтным")
        else:
            try:
                set_default(c, GATE_NAME)
            except SonarApiError as e:
                print(f"ОШИБКА установки дефолтного gate: {e}\n{e.body}", file=sys.stderr)
                return 1
            print(f"✓ Gate '{GATE_NAME}' назначен дефолтным")

    # Финальный показ состава — только если gate реально существует
    # (в dry-run на новый gate его нет, и `show` выдаст 404).
    if gate_exists_now:
        final = show_gate(c, GATE_NAME)
        print()
        print(f"Текущие условия gate '{GATE_NAME}':")
        for cnd in final.get("conditions") or []:
            print(f"  - {cnd.get('metric'):<30} {cnd.get('op')} {cnd.get('error')}")

    print()
    print("Дальше:")
    if not args.set_default and not (existing and existing.get("isDefault")):
        print(f"  - Если хочешь, чтобы '{GATE_NAME}' применялся ко всем 1c-agent-* проектам")
        print(f"    автоматически: python3 {Path(__file__).name} --set-default")
        print(f"    (либо привяжи руками: Project → Quality Gate → Use specific)")
    print(f"  - End-to-end проверка: python3 scripts/smoke_sonar_bsl.py")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано", file=sys.stderr)
        sys.exit(2)
