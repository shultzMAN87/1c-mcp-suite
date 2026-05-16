#!/usr/bin/env python3
"""
Provision Quality Profile для языка BSL в SonarQube.

Зачем:
  Quality Gate (provision_sonar_quality_gate.py) — это «что считать
  падением». Quality Profile — это «какие правила запускать вообще».
  Без активного Quality Profile для BSL у проекта SonarQube запускает
  плагин, но 0 правил активны → 0 issues → пустой Quality Gate (status
  NONE), независимо от того, насколько хороший QG настроен.

  Засветилось в smoke task 5.2: после установки плагина и QG smoke
  возвращал issues_total=0 + qualityGate=NONE именно из-за этого.

Что делает скрипт:
  1. Проверяет, что язык 'bsl' известен серверу (плагин загружен).
  2. Находит built-in профиль для bsl (плагин community поставляет
     'Sonar way'-профиль с набором правил).
  3. Делает его default для языка bsl — тогда новые проекты при
     первом анализе автоматически получают активные правила.
  4. (опционально, --report) — печатает count активных правил, чтобы
     убедиться, что профиль не пустой.

Идемпотентность:
  Повторный запуск ничего не ломает. Если default уже стоит — выводит
  «уже default» и завершается.

Usage:
    python3 scripts/provision_sonar_quality_profile.py --set-default

    python3 scripts/provision_sonar_quality_profile.py --report
        # покажет какие профили есть для bsl, сколько в каждом активных правил

    python3 scripts/provision_sonar_quality_profile.py --dry-run --set-default
        # покажет план

Exit-code:
    0 — успех
    1 — нет профиля для bsl (плагин не поставил его, или загружен не до конца)
    2 — ошибка вызова / нет токена
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

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _smoke_common import load_env_file  # noqa: E402

UA = "1c-mcp-suite-qp-provisioner/1.0"
BSL_LANGUAGE = "bsl"


# ─── Конфиг (повторяем минимум резолвера, чтобы не тащить зависимость) ──

def resolve_token_and_url() -> tuple[str, str]:
    env_file = load_env_file(REPO_ROOT / ".env")
    url = (
        os.environ.get("SONAR_URL")
        or env_file.get("SONAR_URL")
        or "http://localhost:9001"
    ).rstrip("/")
    token = (
        os.environ.get("SONAR_TOKEN")
        or env_file.get("SONAR_TOKEN")
        or ""
    ).strip()
    return token, url


# ─── HTTP ───────────────────────────────────────────────────────────────


class SonarApiError(RuntimeError):
    def __init__(self, msg: str, code: int | None = None, body: str = ""):
        super().__init__(msg)
        self.code = code
        self.body = body


class SonarClient:
    """Минимальная обёртка с обходом системного прокси для localhost
    (см. PLAN.md → Известные проблемы про httpx и v2rayN/Clash)."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        host = self.base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        if host in ("localhost", "127.0.0.1", "::1"):
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    def _headers(self) -> dict[str, str]:
        auth = base64.b64encode(f"{self.token}:".encode("ascii")).decode("ascii")
        return {
            "User-Agent": UA,
            "Accept": "application/json",
            "Authorization": f"Basic {auth}",
        }

    def _request(self, method: str, path: str,
                 params: dict[str, str] | None = None,
                 timeout: float = 15.0) -> dict:
        url = f"{self.base_url}/api/{path.lstrip('/')}"
        data = None
        headers = self._headers()
        if method == "POST":
            data = urllib.parse.urlencode(params or {}).encode("utf-8")
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
            raise SonarApiError(f"{method} {path} → сеть недоступна: {e.reason}")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}

    def get(self, path: str, params=None):
        return self._request("GET", path, params)

    def post(self, path: str, params=None):
        return self._request("POST", path, params)


# ─── Операции с профилями ──────────────────────────────────────────────


def list_bsl_profiles(c: SonarClient) -> list[dict]:
    """Все Quality Profiles для языка BSL. Может быть пусто, если плагин
    ещё не успел зарегистрировать профиль (бывает в первые секунды после
    подъёма SonarQube)."""
    data = c.get("qualityprofiles/search", {"language": BSL_LANGUAGE})
    return data.get("profiles") or []


def list_languages(c: SonarClient) -> list[dict]:
    """Все языки, которые SonarQube знает — для диагностики 'плагин не
    подгружен'."""
    data = c.get("languages/list")
    return data.get("languages") or []


def set_default_profile(c: SonarClient, language: str, profile_name: str) -> None:
    c.post("qualityprofiles/set_default", {
        "language":     language,
        "qualityProfile": profile_name,
    })


def count_active_rules(c: SonarClient, profile_key: str) -> int:
    """Сколько правил активно в профиле. ps=1 — мы только paging.total смотрим."""
    data = c.get("rules/search", {
        "qprofile": profile_key,
        "activation": "true",
        "ps": 1,
    })
    return int(data.get("total") or 0)


# ─── CLI ────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Provision Quality Profile для BSL в SonarQube.",
        epilog="См. также: scripts/provision_sonar_quality_gate.py.",
    )
    ap.add_argument("--set-default", action="store_true",
                    help="Сделать built-in BSL-профиль (Sonar way) дефолтным для языка bsl. "
                         "После этого новые проекты при первом анализе подхватят активные "
                         "правила автоматически.")
    ap.add_argument("--report", action="store_true",
                    help="Только отчёт: какие профили для bsl есть, сколько в каждом "
                         "активных правил, какой default.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Показать план без изменений.")
    args = ap.parse_args()

    token, url = resolve_token_and_url()
    if not token:
        print("ОШИБКА: SONAR_TOKEN не задан.", file=sys.stderr)
        return 2

    if not (args.set_default or args.report):
        # Дефолтное поведение — отчёт, чтобы --help-новичка не оставлять без сигнала.
        args.report = True

    print(f"→ SonarQube: {url}")
    c = SonarClient(url, token)

    # ── язык bsl вообще зарегистрирован?
    try:
        langs = list_languages(c)
    except SonarApiError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1
    bsl_lang = next((l for l in langs if l.get("key") == BSL_LANGUAGE), None)
    if not bsl_lang:
        known = sorted(l.get("key") for l in langs)
        print(f"ОШИБКА: язык 'bsl' не зарегистрирован в SonarQube.", file=sys.stderr)
        print(f"  Известные: {known}", file=sys.stderr)
        print(f"  Это значит, что плагин BSL не загружен. Проверь:", file=sys.stderr)
        print(f"    docker compose logs sonarqube | grep -i 'bsl\\|community'", file=sys.stderr)
        return 1
    print(f"✓ Язык '{BSL_LANGUAGE}' зарегистрирован: {bsl_lang.get('name')}")

    # ── профили для bsl
    try:
        profiles = list_bsl_profiles(c)
    except SonarApiError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1
    if not profiles:
        print(f"ОШИБКА: для языка '{BSL_LANGUAGE}' нет ни одного Quality Profile.",
              file=sys.stderr)
        print(f"  Это аномалия — community-плагин обычно поставляет хотя бы один.",
              file=sys.stderr)
        print(f"  Возможно, сервер не успел доинициализироваться. Подожди 30 сек "
              f"и повтори.", file=sys.stderr)
        return 1

    # ── отчёт (всегда, перед действиями)
    print()
    print(f"Quality Profiles для '{BSL_LANGUAGE}':")
    for p in profiles:
        marker = " ← DEFAULT" if p.get("isDefault") else ""
        try:
            n = count_active_rules(c, p.get("key", ""))
        except SonarApiError:
            n = -1
        n_str = str(n) if n >= 0 else "?"
        print(f"  - {p.get('name', '?'):<40} key={p.get('key', '?'):<24} "
              f"активных_правил={n_str:>6}{marker}")

    if args.report and not args.set_default:
        return 0

    # ── set-default
    if args.set_default:
        # Берём built-in профиль (плагин его всегда отмечает isBuiltIn=true).
        # Если их несколько (1c-syntax community-плагин поставляет один),
        # выбираем тот, что уже default, либо первый built-in.
        default_now = next((p for p in profiles if p.get("isDefault")), None)
        target = next((p for p in profiles if p.get("isBuiltIn")), None) or profiles[0]

        if default_now and default_now.get("name") == target.get("name"):
            print()
            print(f"✓ '{target.get('name')}' уже default — ничего делать не надо.")
            return 0

        print()
        print(f"→ Установка default profile: '{target.get('name')}'")
        if args.dry_run:
            print(f"  [dry-run] был бы вызван qualityprofiles/set_default")
            return 0
        try:
            set_default_profile(c, BSL_LANGUAGE, target.get("name") or "")
        except SonarApiError as e:
            print(f"ОШИБКА: {e}\n{e.body}", file=sys.stderr)
            return 1
        print(f"✓ Default profile для '{BSL_LANGUAGE}' = '{target.get('name')}'")
        print()
        print(f"Дальше:")
        print(f"  - Проверь повторно: python3 scripts/smoke_sonar_bsl.py")
        print(f"  - ВАЖНО: смена default НЕ перепривяжет уже существующие проекты.")
        print(f"    Старые проекты (например, неудачные smoke-прогоны) останутся на")
        print(f"    'No profile' и будут давать 0 issues. Их проще удалить и пересоздать")
        print(f"    либо привязать руками: Project → Quality Profiles.")
        return 0

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано", file=sys.stderr)
        sys.exit(2)
