"""
Общий код для smoke-скриптов (`smoke_auth.py`, `smoke_mcp.py`).

Держит в одном месте:
  1. Канонический список MCP-серверов (имя + порт) — он же задан в
     `1c-mcp-suite/mcp-config.json`. Если там изменится — менять надо и
     здесь. В теории можно парсить JSON оттуда, но в dev-окружении на
     Windows путь к нему относительный/ненадёжный, поэтому проще хранить
     дубль + короткий тест совместимости (см. ниже `verify_against_json`).
  2. Загрузку MCP_SHARED_SECRET: явный CLI-arg > env > .env.

Ни один вызов наружу, ни одного зависимого пакета — чистая stdlib.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Канонический список. Должен совпадать с `1c-mcp-suite/mcp-config.json`.
# Порядок — как в плане и в .env.example, чтобы отчёт читался привычно.
MCP_SERVERS: list[tuple[str, int]] = [
    ("mcp-metadata-graph", 8001),
    ("mcp-bsl-checker",    8002),
    ("mcp-platform-help",  8003),
    ("mcp-1c-naparnik",    8007),
    ("mcp-code-templates", 8008),
    ("mcp-query-builder",  8009),
    ("mcp-testing",        8010),
    ("mcp-code-rag",       8011),
    ("mcp-rest-proxy",     8013),
    ("mcp-sonarqube",      8014),
]


def load_env_file(path: Path) -> dict[str, str]:
    """Минимальный парсер .env — без зависимостей. Поддерживает
    KEY=VALUE, комментарии `#`, пустые строки. Снимает окружающие кавычки.
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        result[k.strip()] = v
    return result


def resolve_secret(cli_value: str | None, env_file: str = ".env") -> str | None:
    """Иерархия источников: --secret (CLI) → env-var → .env → None."""
    secret = (cli_value or "").strip() or os.environ.get("MCP_SHARED_SECRET", "").strip()
    if not secret:
        dotenv = load_env_file(Path(env_file))
        secret = dotenv.get("MCP_SHARED_SECRET", "").strip()
    return secret or None


def verify_against_json(config_path: Path) -> list[str]:
    """Небольшой санити-чек для CI: список серверов/портов здесь не
    разошёлся с `mcp-config.json`. Возвращает список несоответствий
    (пустой — всё ок). Файл может отсутствовать (например, в dev-
    окружении без компоновки) — в этом случае возвращается пустой список.
    """
    if not config_path.exists():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"cannot parse {config_path}: {e}"]

    problems: list[str] = []
    mcp = (data or {}).get("mcp") or {}
    # Соберём (hostname, port) из URL'ов вида http://mcp-xxx:NNNN/sse
    json_entries: list[tuple[str, int]] = []
    for entry in mcp.values():
        url = (entry or {}).get("url", "")
        # Парсим вручную — urllib дёргать не хочется: нужен только host+port
        if "://" not in url:
            continue
        rest = url.split("://", 1)[1]
        host = rest.split("/", 1)[0]
        if ":" not in host:
            continue
        name, port_str = host.rsplit(":", 1)
        try:
            json_entries.append((name, int(port_str)))
        except ValueError:
            problems.append(f"bad port in {url}")

    our_set = set(MCP_SERVERS)
    json_set = set(json_entries)

    for missing in our_set - json_set:
        problems.append(f"in MCP_SERVERS but not in mcp-config.json: {missing}")
    for extra in json_set - our_set:
        problems.append(f"in mcp-config.json but not in MCP_SERVERS: {extra}")

    return problems
