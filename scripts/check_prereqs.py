#!/usr/bin/env python3
"""
Preflight-проверка окружения для 1C MCP Suite.

Запускает набор проверок и выдаёт таблицу "что готово / чего не хватает".
Используется перед первым `docker compose up`, чтобы новый пользователь сразу
увидел конкретный список шагов настройки, а не ловил загадочные ошибки при
старте контейнеров.

Зависимости: только stdlib Python 3.8+. Не требует pip install чего бы то ни
было — пользователь может ещё ничего не установить.

Использование:
    py scripts/check_prereqs.py            # Windows
    python3 scripts/check_prereqs.py       # Linux/macOS
    make check-prereqs                     # Linux/macOS (через Makefile)

Выход:
    0 — все критичные проверки прошли
    1 — есть хотя бы один FAIL (стек не поднимется)
    Предупреждения (WARN) не приводят к ненулевому exit code.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# ─── Цвета в консоли ─────────────────────────────────────────────────────────
# ANSI escape-коды. Автоматически отключаются, если:
#   - выход не в TTY (пайп, редирект в файл)
#   - переменная NO_COLOR установлена (см. https://no-color.org/)
#   - Windows и отсутствует Windows Terminal (старый cmd не умеет ANSI)

_ENABLE_COLOR = (
    sys.stdout.isatty()
    and not os.environ.get("NO_COLOR")
    and (os.name != "nt" or os.environ.get("WT_SESSION") or os.environ.get("TERM"))
)

def _c(code: str, text: str) -> str:
    """Красим текст, если цвета разрешены."""
    if not _ENABLE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(s: str) -> str:  return _c("32", s)
def red(s: str) -> str:    return _c("31", s)
def yellow(s: str) -> str: return _c("33", s)
def bold(s: str) -> str:   return _c("1",  s)
def dim(s: str) -> str:    return _c("2",  s)


# ─── Модель результата ──────────────────────────────────────────────────────

@dataclass
class CheckResult:
    status: str   # "ok" | "warn" | "fail"
    message: str
    hint: str = ""


def ok(msg: str) -> CheckResult:
    return CheckResult("ok", msg)

def warn(msg: str, hint: str = "") -> CheckResult:
    return CheckResult("warn", msg, hint)

def fail(msg: str, hint: str = "") -> CheckResult:
    return CheckResult("fail", msg, hint)


# ─── Корень проекта ─────────────────────────────────────────────────────────
# Скрипт лежит в scripts/, корень — на уровень выше.

ROOT = Path(__file__).resolve().parent.parent


# ─── Чтение .env ─────────────────────────────────────────────────────────────

def parse_env_file(path: Path) -> dict[str, str]:
    """
    Парсит .env как простой словарь. Не поддерживает экспорт, подстановку
    переменных и многострочные значения — нам такие и не нужны.
    Пустые строки и комментарии (#) пропускаются.
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Срезаем внешние кавычки, если есть
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def is_placeholder(value: str) -> bool:
    """
    Плейсхолдер — это значение, которое явно нужно заменить.
    Распознаём по типичным маркерам, которые встречаются в .env.example.
    """
    if not value:
        return True
    v = value.lower()
    markers = ("<токен>", "<token>", "change_me", "changeme", "<your", "xxx", "example.com")
    return any(m in v for m in markers)


# ─── Сами проверки ──────────────────────────────────────────────────────────

def check_docker() -> CheckResult:
    """Docker установлен и запущен."""
    if not shutil.which("docker"):
        return fail(
            "Docker не найден в PATH",
            "Установите Docker Desktop (https://www.docker.com/products/docker-desktop) "
            "или Docker Engine для Linux.",
        )
    try:
        out = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return fail(
                "Docker установлен, но не отвечает",
                "Запустите Docker Desktop или сервис dockerd.",
            )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return fail(f"Docker недоступен: {e}", "Проверьте, что Docker Desktop запущен.")
    return ok("Docker установлен и запущен")


def check_docker_compose() -> CheckResult:
    """docker compose (v2) доступен."""
    if not shutil.which("docker"):
        return fail("Docker не найден — compose проверить невозможно")
    try:
        out = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return fail(
                "docker compose (v2) недоступен",
                "Проверьте, что установлен Docker Desktop или docker-compose-plugin.",
            )
        # Пример вывода: "Docker Compose version v2.24.6"
        match = re.search(r"v?(\d+\.\d+\.\d+)", out.stdout)
        version = match.group(1) if match else "?"
        return ok(f"docker compose v{version}")
    except Exception as e:
        return fail(f"Ошибка вызова docker compose: {e}")


def check_env_file() -> CheckResult:
    """`.env` создан (не `.env.example`)."""
    env = ROOT / ".env"
    example = ROOT / ".env.example"
    if not env.exists():
        hint = "Скопируйте .env.example в .env и заполните ключи:\n" \
               "  Linux/macOS: cp .env.example .env\n" \
               "  Windows:     Copy-Item .env.example .env"
        if not example.exists():
            return fail(".env и .env.example отсутствуют", hint)
        return fail(".env не создан", hint)
    return ok(".env существует")


def check_llm_key(env_vars: dict[str, str]) -> CheckResult:
    """Хотя бы один LLM-ключ задан и не плейсхолдер."""
    openrouter = env_vars.get("OPENROUTER_API_KEY", "")
    anthropic  = env_vars.get("ANTHROPIC_API_KEY", "")
    openrouter_ok = openrouter and not is_placeholder(openrouter)
    anthropic_ok  = anthropic  and not is_placeholder(anthropic)
    if openrouter_ok and anthropic_ok:
        return ok("LLM-ключи заданы: OPENROUTER_API_KEY и ANTHROPIC_API_KEY")
    if openrouter_ok:
        return ok("LLM-ключ задан: OPENROUTER_API_KEY")
    if anthropic_ok:
        return ok("LLM-ключ задан: ANTHROPIC_API_KEY")
    return fail(
        "Ни один LLM-ключ не задан (или все — плейсхолдеры)",
        "Заполните в .env хотя бы одну переменную:\n"
        "  OPENROUTER_API_KEY=sk-or-v1-...\n"
        "  ANTHROPIC_API_KEY=sk-ant-...",
    )


def check_llm_models(env_vars: dict[str, str]) -> CheckResult:
    """LLM_MODEL_STRONG и LLM_MODEL_FAST заданы."""
    missing = [k for k in ("LLM_MODEL_STRONG", "LLM_MODEL_FAST") if not env_vars.get(k)]
    if missing:
        return warn(
            f"Не заданы: {', '.join(missing)}",
            "В .env пропишите модели оркестратора, например:\n"
            "  LLM_MODEL_STRONG=anthropic/claude-sonnet-4.5\n"
            "  LLM_MODEL_FAST=anthropic/claude-haiku-4.5\n"
            "Без них оркестратор упадёт на первом вызове субагента.",
        )
    return ok(f"Модели: STRONG={env_vars['LLM_MODEL_STRONG']}, "
              f"FAST={env_vars['LLM_MODEL_FAST']}")


def check_neo4j_password(env_vars: dict[str, str]) -> CheckResult:
    """
    Пароль Neo4j задан и не является дефолтом или плейсхолдером.

    В docker-compose.yml используется ${NEO4J_PASSWORD:?...} — compose упадёт
    с явной ошибкой, если переменная не задана. Но старый дефолт 'password1c'
    compose не поймает (переменная "задана"), поэтому ловим его здесь.
    """
    pw = env_vars.get("NEO4J_PASSWORD", "")

    # Единая подсказка для всех проблемных случаев
    hint = (
        "Сгенерируйте случайный пароль и пропишите в .env:\n"
        "  Linux/macOS: NEO4J_PASSWORD=$(openssl rand -base64 24)\n"
        "  Windows:     NEO4J_PASSWORD=<результат [Convert]::ToBase64String((1..18|%{[byte](Get-Random -Max 256)}))>"
    )

    if not pw:
        return fail("NEO4J_PASSWORD не задан — docker compose up не запустится", hint)
    if pw == "password1c":
        return fail("NEO4J_PASSWORD использует старый дефолт 'password1c'", hint)
    if is_placeholder(pw):
        return fail("NEO4J_PASSWORD — плейсхолдер, замените на реальный пароль", hint)
    if len(pw) < 12:
        return warn(
            f"NEO4J_PASSWORD короткий ({len(pw)} символов) — рекомендуется от 16",
            hint,
        )
    return ok("NEO4J_PASSWORD задан")


def check_onec_token(env_vars: dict[str, str]) -> CheckResult:
    """Токен 1С:Напарник (требует активной подписки ИТС)."""
    token = env_vars.get("ONEC_AI_TOKEN", "")
    if not token or is_placeholder(token):
        return warn(
            "ONEC_AI_TOKEN не задан — сервер mcp-1c-naparnik работать не будет",
            "Получите токен на code.1c.ai (требуется активная подписка ИТС), "
            "затем пропишите в .env:\n  ONEC_AI_TOKEN=...",
        )
    return ok("ONEC_AI_TOKEN задан")


def check_sonar_token(env_vars: dict[str, str]) -> CheckResult:
    """Токен SonarQube (нужен для авторизации mcp-sonarqube)."""
    token = env_vars.get("SONAR_TOKEN", "")
    if not token or is_placeholder(token):
        return warn(
            "SONAR_TOKEN не задан — mcp-sonarqube не сможет обратиться к SonarQube",
            "После запуска SonarQube (http://localhost:9001, admin/admin) создайте "
            "User Token (префикс squ_, НЕ Global Analysis Token): "
            "My Account → Security → Generate Tokens.\n"
            "Затем пропишите в .env:\n  SONAR_TOKEN=squ_...",
        )
    return ok("SONAR_TOKEN задан")


def check_compose_file_separator(env_vars: dict[str, str]) -> CheckResult:
    """
    COMPOSE_FILE использует правильный разделитель для текущей ОС.

    Это та самая Windows-ловушка: разделитель путей в переменной COMPOSE_FILE
    зависит от ОС — на Linux/macOS это ':', на Windows ';'. Если скопировать
    .env.example как есть на Windows, docker compose вывалится с ошибкой
    'CreateFile ...:... The filename syntax is incorrect'.
    """
    value = env_vars.get("COMPOSE_FILE", "")
    if not value:
        # Необязательная переменная (нужна только при мультифайловом compose)
        return ok("COMPOSE_FILE не задан (OK, используется docker-compose.yml по умолчанию)")

    is_windows = os.name == "nt"
    expected_sep = ";" if is_windows else ":"
    wrong_sep    = ":" if is_windows else ";"

    # Смотрим, какой разделитель в значении. Но двоеточие в пути на Windows
    # может быть и частью буквы диска ("D:\..."). Поэтому исключаем такие.
    # Практика: на Linux в COMPOSE_FILE не должно быть ';', на Windows — ':'
    # за пределами "C:\..." паттерна.

    if is_windows:
        # Нас интересует ':', который разделяет пути в COMPOSE_FILE
        # (а не ':' после буквы диска в абсолютном пути вроде "C:\...").
        # Убираем все абсолютные пути с буквой диска и смотрим, остался ли ':'.
        stripped = re.sub(r"\b[A-Za-z]:[\\/]", "", value)
        if ":" in stripped:
            fixed_value = value.replace(":", ";", 1)
            return fail(
                "COMPOSE_FILE использует ':' как разделитель — это для Linux/macOS.\n"
                f"    Текущее значение: {value}",
                "На Windows разделитель — ';'. Исправьте в .env:\n"
                f"  COMPOSE_FILE={fixed_value}",
            )
    else:
        if ";" in value:
            return fail(
                "COMPOSE_FILE использует ';' как разделитель — это для Windows.\n"
                f"    Текущее значение: {value}",
                "На Linux/macOS разделитель — ':'. Исправьте в .env:\n"
                f"  COMPOSE_FILE={value.replace(';', ':', 1)}",
            )
    return ok(f"COMPOSE_FILE разделитель корректен для {'Windows' if is_windows else 'Linux/macOS'}")


def _dir_has_files(path: Path, extensions: tuple[str, ...] | None = None) -> bool:
    """Есть ли в директории хотя бы один файл (опционально — с нужным расширением), не считая README/.gitignore."""
    if not path.is_dir():
        return False
    for entry in path.iterdir():
        if not entry.is_file():
            continue
        name = entry.name.lower()
        if name.startswith(".") or name.startswith("readme"):
            continue
        if extensions is None or name.endswith(extensions):
            return True
    return False


def check_config_xml() -> CheckResult:
    """XML-выгрузка конфигурации 1С."""
    path = ROOT / "1c-config-xml"
    if not path.is_dir():
        return fail(
            "Директория 1c-config-xml/ отсутствует",
            "Создайте её и выгрузите туда XML-представление вашей конфигурации 1С "
            "(через Конфигуратор → Конфигурация → Выгрузить в файлы XML).",
        )
    if not _dir_has_files(path, (".xml", ".txt")):
        return fail(
            "1c-config-xml/ пустая — граф метаданных не построится",
            "Выгрузите XML-представление конфигурации через Конфигуратор:\n"
            "  Конфигурация → Выгрузить конфигурацию в файлы XML",
        )
    return ok("1c-config-xml/ содержит выгрузку")


def check_its_articles() -> CheckResult:
    """PDF-статьи ИТС (опционально, но полезно)."""
    path = ROOT / "its-articles"
    if not _dir_has_files(path, (".pdf", ".txt", ".md")):
        return warn(
            "its-articles/ пустая — поиск по ИТС работать не будет",
            "Положите PDF-статьи с its.1c.ru (Ctrl+P → Сохранить как PDF). "
            "Требуется активная подписка ИТС.",
        )
    return ok("its-articles/ содержит статьи")


def check_platform_help() -> CheckResult:
    """`.hbk`-файлы справки платформы (опционально)."""
    path = ROOT / "platform-help-data"
    if not _dir_has_files(path, (".hbk",)):
        return warn(
            "platform-help-data/ не содержит .hbk — справка платформы будет пустой",
            "Скопируйте .hbk-файлы из установки 1С (обычно:\n"
            "  C:\\Program Files\\1cv8\\8.3.x.x\\bin\\conf\\) в platform-help-data/.",
        )
    return ok("platform-help-data/ содержит .hbk")


def check_sonar_plugin() -> CheckResult:
    """jar BSL-плагина для SonarQube в sonar-plugins/."""
    path = ROOT / "sonar-plugins"
    if not _dir_has_files(path, (".jar",)):
        return warn(
            "sonar-plugins/ не содержит .jar — SonarQube будет анализировать BSL "
            "как plain text (0 issues на любом коде)",
            "Простой путь — запустить установщик:\n"
            "  python3 scripts/install_sonar_bsl_plugin.py\n"
            "Ручной путь — скачать jar с\n"
            "  https://github.com/1c-syntax/sonar-bsl-plugin-community/releases\n"
            "и положить в sonar-plugins/.",
        )
    return ok("sonar-plugins/ содержит .jar плагин")


def check_sonar_plugin_loaded(env_vars: dict[str, str]) -> CheckResult:
    """
    Проверка, что живой SonarQube подгрузил BSL-плагин.
    Soft-skip: если сервер недоступен (compose не поднят, токен пустой и т.п.) —
    возвращает WARN, а не FAIL. Это preflight-проверка, не runtime.

    Закрывает кейс из задачи 5.1: jar лежит в sonar-plugins/, но SonarQube
    его не подхватил (например, после смены имени артефакта или ошибки
    совместимости), а пользователь видит только «0 issues».
    """
    token = env_vars.get("SONAR_TOKEN", "")
    if not token or is_placeholder(token):
        return warn(
            "Не могу проверить, виден ли BSL-плагин в SonarQube — нет SONAR_TOKEN",
            "Это не блокер: проверка опциональная. После задания SONAR_TOKEN "
            "в .env проверка прогонится и подтвердит, что плагин подхвачен.",
        )

    sonar_url = env_vars.get("SONAR_URL", "http://localhost:9001").rstrip("/")

    import base64
    import json as _json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"{sonar_url}/api/plugins/installed")
    req.add_header(
        "Authorization",
        "Basic " + base64.b64encode(f"{token}:".encode("ascii")).decode("ascii"),
    )
    # Тот же фикс, что в provision_sonar_quality_gate.py: системный прокси
    # на Windows (v2rayN/Clash) перехватывает localhost-запросы. Для local
    # endpoint'а используем opener с пустым ProxyHandler.
    host = sonar_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host in ("localhost", "127.0.0.1", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=5.0) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return warn(
            f"SonarQube ответил HTTP {e.code} на /api/plugins/installed — "
            f"проверка пропущена",
            f"Если 401 — токен невалидный или с урезанными правами "
            f"(нужен User Token, префикс 'squ_'). Если другой код — посмотри "
            f"docker compose logs sonarqube.",
        )
    except (urllib.error.URLError, OSError):
        return warn(
            f"SonarQube недоступен ({sonar_url}) — проверка плагина пропущена",
            "Это нормально, если compose ещё не поднят. После `docker compose up -d` "
            "и таймаута 30-60с проверка прогонится.",
        )

    plugins = data.get("plugins") or []
    bsl = [p for p in plugins
           if "bsl" in (p.get("key") or "").lower()
           or "bsl" in (p.get("name") or "").lower()]
    if not bsl:
        return fail(
            "В sonar-plugins/ есть jar, но SonarQube его НЕ подгрузил",
            "Это типичный симптом задачи 5.1 (0 issues на BSL). Проверь:\n"
            "  1. docker compose restart sonarqube  (после установки jar)\n"
            "  2. docker compose logs sonarqube | grep -i 'plugin\\|bsl'\n"
            "     — там должна быть строка вида 'Loaded plugin BSL Community'.\n"
            "  3. Версия плагина может быть несовместима с этой версией SonarQube — "
            "посмотри release notes плагина.",
        )

    p = bsl[0]
    return ok(f"BSL-плагин подгружен: {p.get('name', '?')} {p.get('version', '?')}")


def check_yaxunit_license(env_vars: dict[str, str]) -> CheckResult:
    """
    Лицензия для yaxunit-stack (опциональный тестовый стенд).
    Проверяется, только если COMPOSE_FILE включает yaxunit-stack.
    """
    compose_file = env_vars.get("COMPOSE_FILE", "")
    if "yaxunit-stack" not in compose_file:
        return ok("yaxunit-stack не включён (OK, опциональный стенд)")

    path = ROOT / "yaxunit-stack" / "licenses"
    if not _dir_has_files(path, (".lic",)):
        return fail(
            "yaxunit-stack включён, но yaxunit-stack/licenses/ без .lic-файла",
            "Положите файл лицензии 1С (.lic) в yaxunit-stack/licenses/. "
            "Без лицензии сервер 1С в тестовом контейнере не стартует.",
        )
    return ok("yaxunit-stack/licenses/ содержит .lic")


# ─── Runner ──────────────────────────────────────────────────────────────────

def run_all_checks() -> list[tuple[str, CheckResult]]:
    """
    Выполняет все проверки, возвращает список (label, result).
    Проверки, зависящие от .env, пропускаются если .env не существует.
    """
    checks: list[tuple[str, CheckResult]] = []

    # Независимые от .env проверки
    checks.append(("Docker",           check_docker()))
    checks.append(("docker compose",   check_docker_compose()))

    env_result = check_env_file()
    checks.append((".env",             env_result))

    # Если .env нет — дальнейшие проверки бесполезны
    if env_result.status == "fail":
        return checks

    env_vars = parse_env_file(ROOT / ".env")

    checks.append(("LLM ключ",         check_llm_key(env_vars)))
    checks.append(("LLM модели",       check_llm_models(env_vars)))
    checks.append(("COMPOSE_FILE",     check_compose_file_separator(env_vars)))
    checks.append(("Neo4j пароль",     check_neo4j_password(env_vars)))
    checks.append(("ONEC_AI_TOKEN",    check_onec_token(env_vars)))
    checks.append(("SONAR_TOKEN",      check_sonar_token(env_vars)))
    checks.append(("1c-config-xml/",   check_config_xml()))
    checks.append(("its-articles/",    check_its_articles()))
    checks.append(("platform-help-data/", check_platform_help()))
    checks.append(("sonar-plugins/",   check_sonar_plugin()))
    checks.append(("Sonar BSL plugin loaded", check_sonar_plugin_loaded(env_vars)))
    checks.append(("yaxunit-stack",    check_yaxunit_license(env_vars)))

    return checks


def render(results: list[tuple[str, CheckResult]]) -> tuple[int, int, int]:
    """Печатает таблицу и возвращает (n_ok, n_warn, n_fail)."""
    n_ok = n_warn = n_fail = 0

    label_width = max(len(label) for label, _ in results)

    print()
    print(bold("Preflight 1C MCP Suite"))
    print(dim("─" * 70))
    print()

    for label, res in results:
        if res.status == "ok":
            icon = green("✓ OK  ")
            n_ok += 1
        elif res.status == "warn":
            icon = yellow("⚠ WARN")
            n_warn += 1
        else:
            icon = red("✗ FAIL")
            n_fail += 1
        print(f"  {icon}  {label.ljust(label_width)}  {res.message}")

    print()
    print(dim("─" * 70))

    # Подсказки по проблемам
    problems = [(label, res) for label, res in results if res.status in ("warn", "fail")]
    if problems:
        print()
        print(bold("Что исправить:"))
        print()
        for label, res in problems:
            colored = red(label) if res.status == "fail" else yellow(label)
            print(f"  {colored}: {res.message}")
            if res.hint:
                for line in res.hint.splitlines():
                    print(f"      {dim(line)}")
            print()

    # Итог
    summary = f"{green(f'{n_ok} OK')}  {yellow(f'{n_warn} WARN')}  {red(f'{n_fail} FAIL')}"
    print(f"Итого: {summary}")
    print()

    return n_ok, n_warn, n_fail


def main() -> int:
    results = run_all_checks()
    _, _, n_fail = render(results)
    return 1 if n_fail > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
