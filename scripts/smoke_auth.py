#!/usr/bin/env python3
"""
Smoke-тест shared-secret аутентификации MCP SSE-серверов (задача 3.2).

Что делает:
  1. Читает MCP_SHARED_SECRET из .env (или env, или --secret).
  2. Ходит по всем 11 MCP SSE-портам на localhost.
  3. Для каждого проверяет:
     - без заголовка → ожидаем 401
     - с неправильным Bearer → ожидаем 401
     - с правильным Bearer → ожидаем стрим (200 + text/event-stream)
  4. Печатает сводку и возвращает exit code: 0 если всё OK, иначе 1.

Почему stream=True:
  SSE-эндпоинт FastMCP стримит первое событие (endpoint-redirect)
  мгновенно, после чего держит соединение открытым. Обычный `.get()`
  без stream иногда гонится с keep-alive и даёт ReadTimeout ДО того,
  как httpx успеет прочитать status+headers. В первой версии скрипта
  из-за этого probe 1 (ждали 401) интерпретировался как "SSE стрим
  пошёл" и валился в false-FAIL.
  `client.stream("GET", url)` отдаёт `Response` СРАЗУ после получения
  заголовков — до чтения body. Status и Content-Type уже доступны,
  тело мы явно закрываем.

Использование:
    python3 scripts/smoke_auth.py
    MCP_SHARED_SECRET=... python3 scripts/smoke_auth.py
    MCP_HOST=192.168.1.10 python3 scripts/smoke_auth.py
    python3 scripts/smoke_auth.py --only mcp-platform-help
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ОШИБКА: требуется пакет httpx. Установите: pip install httpx", file=sys.stderr)
    sys.exit(2)

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from _smoke_common import MCP_SERVERS, resolve_secret  # noqa: E402


# ─── Одна проба ──────────────────────────────────────────────────────────

def _probe(client: httpx.Client, url: str, headers: dict | None) -> tuple[int, str, str | None]:
    """Один GET в стриминг-режиме. Возвращает (status_code, content_type, error).

    Используем `client.stream("GET", ...)`: он возвращает Response как
    контекст-менеджер СРАЗУ после прихода заголовков, не блокируясь на
    чтении тела. Это отсекает гонку с keep-alive.
    """
    try:
        timeout = httpx.Timeout(5.0, connect=5.0, read=5.0)
        with client.stream("GET", url, headers=headers or {}, timeout=timeout) as r:
            ct = r.headers.get("content-type", "")
            status = r.status_code
            # Тело не читаем: на 401 ответ маленький и закроется сам,
            # на SSE мы не хотим тянуть стрим до таймаута.
            return status, ct, None
    except httpx.ConnectError as e:
        return 0, "", f"ConnectError: {e}"
    except httpx.ReadTimeout as e:
        # Редкость: заголовки должны прийти за миллисекунды. Но если
        # произошло — трактуем как отказ.
        return 0, "", f"ReadTimeout: {e}"
    except httpx.HTTPError as e:
        return 0, "", f"{type(e).__name__}: {e}"


def check_server(
    client: httpx.Client,
    host: str,
    name: str,
    port: int,
    secret: str | None,
) -> dict:
    """Возвращает dict с результатами трёх проб для одного сервера.

    Статусы в результате: "pass" / "fail" / "skip".
    """
    url = f"http://{host}:{port}/sse"
    probes: dict[str, dict] = {}

    # Probe 1: без заголовка
    status, ct, err = _probe(client, url, headers=None)
    if err and status == 0:
        probes["no_header"] = {"status": "fail", "reason": err}
        return {"server": name, "port": port, "reachable": False, "probes": probes}

    if secret:
        # Middleware активна — ожидаем 401.
        probes["no_header"] = {
            "status": "pass" if status == 401 else "fail",
            "http_code": status,
            "expected": 401,
        }
    else:
        # Middleware выключена — tunnel открыт, должен быть 200 SSE-стрим.
        ok = status == 200
        probes["no_header"] = {
            "status": "pass" if ok else "fail",
            "http_code": status,
            "content_type": ct,
            "expected": "200 + event-stream (auth disabled)",
        }

    # Probe 2: неправильный Bearer — только если secret задан
    if secret:
        status, ct, err = _probe(
            client, url, headers={"Authorization": "Bearer WRONG_WRONG_WRONG"}
        )
        if err and status == 0:
            probes["wrong_bearer"] = {"status": "fail", "reason": err}
        else:
            probes["wrong_bearer"] = {
                "status": "pass" if status == 401 else "fail",
                "http_code": status,
                "expected": 401,
            }
    else:
        probes["wrong_bearer"] = {"status": "skip", "reason": "secret not set"}

    # Probe 3: правильный Bearer — только если secret задан
    if secret:
        status, ct, err = _probe(
            client, url, headers={"Authorization": f"Bearer {secret}"}
        )
        if err and status == 0:
            probes["correct_bearer"] = {"status": "fail", "reason": err}
        else:
            # Ожидаем 200 + event-stream. Просто 200 — тоже pass
            # (на случай если FastMCP изменит content-type).
            stream_ok = status == 200
            probes["correct_bearer"] = {
                "status": "pass" if stream_ok else "fail",
                "http_code": status,
                "content_type": ct,
                "expected": "200 + event-stream",
            }
    else:
        probes["correct_bearer"] = {"status": "skip", "reason": "secret not set"}

    return {"server": name, "port": port, "reachable": True, "probes": probes}


# ─── Main ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-тест shared-secret auth для MCP SSE")
    ap.add_argument("--host", default=os.environ.get("MCP_HOST", "localhost"),
                    help="Хост MCP-серверов (по умолчанию localhost)")
    ap.add_argument("--env-file", default=".env",
                    help="Путь к .env (по умолчанию ./.env)")
    ap.add_argument("--secret", default=None,
                    help="Явный секрет; перекрывает и env, и .env файл")
    ap.add_argument("--only", default=None,
                    help="Запустить только указанные сервера через запятую")
    args = ap.parse_args()

    secret = resolve_secret(args.secret, env_file=args.env_file)

    servers = MCP_SERVERS
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        servers = [(n, p) for n, p in MCP_SERVERS if n in wanted]
        if not servers:
            print(f"ОШИБКА: --only={args.only!r} не нашёл ни одного сервера.", file=sys.stderr)
            return 2

    print(f"Host: {args.host}")
    if secret:
        print(f"Secret: задан (length={len(secret)})")
        print("Ожидание: все серверы должны возвращать 401 без заголовка и 200 с правильным.")
    else:
        print("Secret: НЕ ЗАДАН")
        print("Ожидание: все серверы открыты (middleware выключена), пробы с Bearer пропускаются.")
    print("─" * 72)

    total_probes = 0
    failed_probes = 0
    unreachable_servers = 0

    with httpx.Client() as client:
        for name, port in servers:
            result = check_server(client, args.host, name, port, secret)

            if not result["reachable"]:
                unreachable_servers += 1
                print(f"✗ {name}:{port} — НЕДОСТУПЕН")
                for probe_name, probe in result["probes"].items():
                    print(f"    {probe_name}: {probe.get('reason')}")
                continue

            server_ok = True
            probe_details = []
            for probe_name, probe in result["probes"].items():
                if probe["status"] == "skip":
                    probe_details.append(f"{probe_name}=skip")
                    continue
                total_probes += 1
                if probe["status"] == "pass":
                    probe_details.append(f"{probe_name}=✓")
                else:
                    failed_probes += 1
                    server_ok = False
                    probe_details.append(
                        f"{probe_name}=✗({probe.get('http_code', '?')} "
                        f"want {probe.get('expected')})"
                    )

            marker = "✓" if server_ok else "✗"
            print(f"{marker} {name}:{port}  [{', '.join(probe_details)}]")

    print("─" * 72)
    print(
        f"Серверов недоступно: {unreachable_servers}  |  "
        f"Проверок проведено: {total_probes}  |  Провалено: {failed_probes}"
    )

    if unreachable_servers and not secret:
        print("\n⚠  Недоступные серверы при выключенной аутентификации — "
              "docker compose точно запущен? (docker compose ps)")

    if failed_probes == 0 and unreachable_servers == 0:
        print("\nИТОГ: PASS")
        return 0

    print("\nИТОГ: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
