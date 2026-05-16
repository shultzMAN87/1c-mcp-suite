#!/usr/bin/env python3
"""
Установка плагина SonarQube для языка 1С BSL (задача 5.2).

Контекст: без BSL-плагина SonarQube анализирует .bsl-файлы как plain
text и выдаёт 0 issues на любом коде (засветилось в задаче 5.1 при
сканировании функции `Факториал`). Этот скрипт автоматизирует ручной
шаг из `sonar-plugins/README.md`.

Текущее имя артефакта в апстриме (подтверждено на 2026-04-XX):
    sonar-communitybsl-plugin-<version>.jar
Исторически — `sonar-bsl-plugin-community-*.jar` (тот же репозиторий
`1c-syntax/sonar-bsl-plugin-community`, ребрендинг внутри артефакта).
Скрипт распознаёт ОБА имени как «плагин уже на месте», поэтому
переход 1.x → 1.18+ не сломает существующие установки.

Логика:
  1. Если в `sonar-plugins/` уже лежит ЛЮБОЙ подходящий jar — ничего
     не качаем (любая версия). Скачать заново — `--force` или
     явный `--version X.Y.Z`.
  2. Если jar'а нет (или указан --force) — тащим последний релиз из
     GitHub Releases API.
  3. После любого изменения каталога — печатаем команду перезапуска
     SonarQube и proof-команду для проверки.

Скрипт намеренно stdlib-only (urllib + json) — чтобы запускался на
голой машине без `pip install httpx` и т.п. Совпадает по стилю с
`scripts/check_prereqs.py`.

Usage:
    # Самый частый случай — поставить latest, если ничего нет.
    python3 scripts/install_sonar_bsl_plugin.py

    # Принудительно перекачать (например, после смены плагина руками).
    python3 scripts/install_sonar_bsl_plugin.py --force

    # Жёстко закрепить версию (для CI / воспроизводимости).
    python3 scripts/install_sonar_bsl_plugin.py --version 1.17.0

    # Только проверить, что плагин на месте — без скачивания.
    # Полезно для прекоммит-хука или CI: exit 1, если нет.
    python3 scripts/install_sonar_bsl_plugin.py --check

Exit-code:
    0 — плагин на месте (был / установлен / совпал с --version)
    1 — плагин отсутствует (--check) или ошибка скачивания
    2 — ошибка вызова (неверные аргументы, недоступная сеть и т.п.)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "sonar-plugins"

# Имена обоих исторических артефактов. Любой match — считаем «плагин есть».
# `sonar-communitybsl-plugin-1.17.0.jar`            — текущее (с ~1.12)
# `sonar-bsl-plugin-community-1.11.0.jar`           — старое имя
JAR_PATTERNS = (
    re.compile(r"^sonar-communitybsl-plugin-.*\.jar$", re.IGNORECASE),
    re.compile(r"^sonar-bsl-plugin-community-.*\.jar$", re.IGNORECASE),
)

# GitHub API. Без токена лимит 60 req/h на IP — нам хватает с запасом.
GH_API_LATEST = "https://api.github.com/repos/1c-syntax/sonar-bsl-plugin-community/releases/latest"
GH_API_TAG    = "https://api.github.com/repos/1c-syntax/sonar-bsl-plugin-community/releases/tags/v{version}"

# User-Agent у GitHub API обязательный — без него 403.
UA = "1c-mcp-suite-installer/1.0 (+https://github.com/anthropics/claude)"


# ─── Локальная проверка ─────────────────────────────────────────────────

def find_existing_plugin() -> Path | None:
    """Возвращает путь к подходящему jar в `sonar-plugins/`, либо None."""
    if not PLUGIN_DIR.is_dir():
        return None
    for f in sorted(PLUGIN_DIR.iterdir()):
        if f.is_file() and any(p.match(f.name) for p in JAR_PATTERNS):
            return f
    return None


def remove_existing_plugins() -> list[str]:
    """Удаляет все совпадающие jar'ы (для --force/--version). Возвращает имена."""
    removed: list[str] = []
    if not PLUGIN_DIR.is_dir():
        return removed
    for f in sorted(PLUGIN_DIR.iterdir()):
        if f.is_file() and any(p.match(f.name) for p in JAR_PATTERNS):
            try:
                f.unlink()
                removed.append(f.name)
            except OSError as e:
                print(f"  предупреждение: не удалось удалить {f.name}: {e}",
                      file=sys.stderr)
    return removed


# ─── Сеть: GitHub API ───────────────────────────────────────────────────

def _http_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_release(version: str | None) -> tuple[str, str, str]:
    """
    Возвращает (version, jar_name, download_url) для запрошенной версии.
    Если version=None — берёт latest.
    Бросает RuntimeError при сетевых/API проблемах.
    """
    url = GH_API_LATEST if version is None else GH_API_TAG.format(version=version)
    try:
        data = _http_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404 and version is not None:
            raise RuntimeError(f"Версия v{version} не найдена в релизах апстрима")
        raise RuntimeError(f"GitHub API ответил HTTP {e.code} на {url}: {e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Сетевая ошибка при обращении к {url}: {e.reason}")
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"Не разобрал ответ {url}: {e}")

    tag = (data.get("tag_name") or "").lstrip("v")
    if not tag:
        raise RuntimeError(f"В ответе нет tag_name: {data!r}")

    # Среди assets найдём первый jar, попадающий под наши паттерны.
    for asset in data.get("assets") or []:
        name = asset.get("name") or ""
        dl   = asset.get("browser_download_url") or ""
        if dl and any(p.match(name) for p in JAR_PATTERNS):
            return tag, name, dl

    raise RuntimeError(
        f"В релизе v{tag} нет jar-файла, подходящего под известные имена.\n"
        f"  Ожидали: sonar-communitybsl-plugin-*.jar или sonar-bsl-plugin-community-*.jar\n"
        f"  Доступные assets: {[a.get('name') for a in (data.get('assets') or [])]}"
    )


def download(url: str, dest: Path) -> int:
    """Скачивает url в dest. Возвращает размер в байтах. Качаем во временный
    файл и атомарно переименовываем — на случай прерывания."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out, length=64 * 1024)
        size = tmp.stat().st_size
        # Sanity: jar плагина < 1MB подозрителен (реальный — десятки MB).
        if size < 100_000:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"Скачанный файл подозрительно мал ({size} байт). "
                f"Возможно, сеть/прокси отдали HTML-заглушку. URL: {url}"
            )
        tmp.replace(dest)
        return size
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ─── CLI ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Установить плагин SonarQube для 1С BSL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Без аргументов: ничего не делать, если jar уже есть; "
            "иначе скачать latest.\n"
            "См. также: sonar-plugins/README.md."
        ),
    )
    ap.add_argument(
        "--version",
        help="Закрепить конкретную версию (например, 1.17.0). Перетаскивает "
             "jar даже если уже установлена другая версия.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Удалить существующий jar и скачать latest заново.",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Только проверить наличие jar. Exit 0 если есть, 1 если нет.",
    )
    args = ap.parse_args()

    if args.check and (args.force or args.version):
        print("ОШИБКА: --check несовместим с --force/--version", file=sys.stderr)
        return 2

    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    existing = find_existing_plugin()

    # ─── --check ────
    if args.check:
        if existing:
            print(f"OK: плагин найден — {existing.name}")
            return 0
        print("НЕТ: в sonar-plugins/ нет подходящего jar", file=sys.stderr)
        print("     запусти: python3 scripts/install_sonar_bsl_plugin.py", file=sys.stderr)
        return 1

    # ─── default / --force / --version ────
    need_download = args.force or (args.version is not None) or (existing is None)

    if not need_download:
        # existing != None и пользователь не просил перекачать — ничего не делаем.
        print(f"✓ Плагин уже установлен: {existing.name}")
        print(f"  Чтобы взять новую версию: python3 {Path(__file__).name} --force")
        return 0

    # Качаем.
    try:
        version, jar_name, dl_url = fetch_release(args.version)
    except RuntimeError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1

    print(f"→ Скачиваю sonar-bsl-plugin-community v{version} ({jar_name})")
    print(f"  {dl_url}")

    if existing:
        removed = remove_existing_plugins()
        for name in removed:
            print(f"  удалён старый: {name}")

    dest = PLUGIN_DIR / jar_name
    try:
        size = download(dl_url, dest)
    except RuntimeError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"ОШИБКА: сеть недоступна — {e.reason}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"ОШИБКА: запись на диск — {e}", file=sys.stderr)
        return 1

    print(f"✓ Установлен: {dest.name} ({size / 1024 / 1024:.1f} MB)")
    print()
    print("Дальше:")
    print("  1. Перезапустить SonarQube, чтобы он подхватил плагин:")
    print("       docker compose restart sonarqube")
    print("  2. Подождать 30-60 сек (Sonar поднимается долго) и проверить:")
    print("       docker compose logs sonarqube | grep -i 'bsl\\|community'")
    print("     должна быть строка вида: 'Plugin [BSL Community Plugin]'")
    print("  3. Завести Quality Gate с правилами BSL:")
    print("       python3 scripts/provision_sonar_quality_gate.py")
    print("  4. End-to-end проверка анализа:")
    print("       python3 scripts/smoke_sonar_bsl.py")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано", file=sys.stderr)
        sys.exit(2)
