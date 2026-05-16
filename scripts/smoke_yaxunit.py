#!/usr/bin/env python3
"""
Smoke-тест интеграции mcp-testing ↔ yaxunit-stack.

В отличие от scripts/smoke_mcp.py — этот скрипт ТРЕБУЕТ поднятого
yaxunit-stack (compose-профиль "testing") с готовой лицензией.
Запускается отдельно: либо вручную после поднятия обоих стеков,
либо как часть CI с уже разогретыми образами 1С.

Что проверяет:
  1. test_runner_health() — раннер достижим, лицензии на месте,
     payloads_volume_mounted=true с обеих сторон.
  2. test_run_path() — сквозной прогон демо-тестов из
     yaxunit-stack/sandbox/demo-{config,tests} через shared volume:
       - копирует demo-* во временный каталог под workspace;
       - дёргает test_run_path по этим путям;
       - проверяет, что вернулся status="passed" и tests > 0;
       - чистит временную копию.

Использование:
    # Дефолт — обращается к mcp-testing на localhost:8010
    python3 scripts/smoke_yaxunit.py

    # Другой хост / порт
    MCP_HOST=192.168.1.10 MCP_TESTING_PORT=8010 \
        python3 scripts/smoke_yaxunit.py

    # Подробный лог при провале
    python3 scripts/smoke_yaxunit.py --verbose

    # Файловый режим (быстрее, без серверной лицензии)
    python3 scripts/smoke_yaxunit.py --mode file

Exit-code: 0 если оба чека OK, иначе 1.

Этот скрипт намеренно НЕ включён в основной smoke_mcp.py: тот должен
оставаться быстрым и не зависеть от поднятой 1С. integration-smoke —
отдельный, дороже, и его OK даёт качественно другую гарантию.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

# Переиспользуем общую инфраструктуру MCP-клиента и загрузку секрета
from _smoke_common import MCP_SERVERS, resolve_secret  # noqa: E402

# MCPSession живёт в evals/runner/mcp_client.py (как и в smoke_mcp.py)
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


SANDBOX_DEMO = REPO_ROOT / "yaxunit-stack" / "sandbox"
WORKSPACE_DIR = REPO_ROOT / "workspace"


def find_testing_server() -> tuple[str, int]:
    """MCP_SERVERS — это list[tuple[name, port]] из _smoke_common.py."""
    for name, port in MCP_SERVERS:
        if name == "mcp-testing":
            return name, port
    raise RuntimeError("mcp-testing not in MCP_SERVERS list — proverь _smoke_common.py")


async def call_tool(session: MCPSession, tool: str, args: dict) -> dict:
    """Вызвать tool, проверить успешность, вернуть распарсенный JSON-dict.

    MCPSession возвращает ToolCallResult c полями ok/is_error_flag/
    raw_text/parsed/error — обращаемся через них, не через SDK-объекты.
    """
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
    # raw_text есть, но в JSON не распарсился
    try:
        return json.loads(result.raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{tool} returned non-JSON text: {result.raw_text[:300]}... ({e})"
        )


def check_demo_payload_present() -> tuple[Path, Path]:
    """Убедиться, что demo-config и demo-tests существуют на хосте."""
    demo_config = SANDBOX_DEMO / "demo-config"
    demo_tests = SANDBOX_DEMO / "demo-tests"
    for p in (demo_config, demo_tests):
        if not (p / "Configuration.xml").is_file():
            raise FileNotFoundError(
                f"{p}/Configuration.xml не найден — sandbox-данные на месте?"
            )
    return demo_config, demo_tests


def stage_into_workspace(demo_config: Path, demo_tests: Path) -> tuple[Path, Path, str]:
    """
    Скопировать demo-* во временный каталог внутри workspace.

    Возвращает (config_path_on_host, tests_path_on_host, rel_path).
    rel_path — путь относительно workspace, его передаём в test_run_path.
    """
    if not WORKSPACE_DIR.is_dir():
        raise FileNotFoundError(
            f"workspace не найден: {WORKSPACE_DIR}. "
            "Запускайте smoke из корня репозитория после `docker compose up`."
        )
    rel = f".smoke-yaxunit/{uuid.uuid4().hex[:8]}"
    dst_root = WORKSPACE_DIR / rel
    dst_root.mkdir(parents=True, exist_ok=False)
    shutil.copytree(demo_config, dst_root / "config")
    shutil.copytree(demo_tests, dst_root / "tests")
    return dst_root / "config", dst_root / "tests", rel


async def run_smoke(args) -> int:
    print(f"== smoke_yaxunit ==")
    _name, default_port = find_testing_server()
    # resolve_secret сам прочитает .env и вернёт секрет либо None
    secret = resolve_secret(args.secret, env_file=str(REPO_ROOT / ".env"))
    # MCPSession берёт секрет из env-переменной MCP_SHARED_SECRET — пробрасываем
    if secret:
        os.environ["MCP_SHARED_SECRET"] = secret
        print(f"  auth  : Bearer (secret length={len(secret)})")
        # Diagnostic: отпечаток секрета (первые/последние 4 символа) + repr —
        # ловит trailing \r, кавычки, пробелы и пр. невидимый мусор.
        # Полное значение НЕ печатаем (это секрет).
        print(f"  secret repr: {secret[:4]!r}...{secret[-4:]!r}  bytes={len(secret.encode())}")
        if any(c in secret for c in "\r\n\"' \t"):
            print(f"  WARN: секрет содержит whitespace/кавычки — это причина 401")
    else:
        # Не падаем — может быть, на сервере auth выключена. Но предупреждаем,
        # потому что чаще это значит «секрет не нашли», и в логах будет 401.
        print("  auth  : NONE (если в логах сервера 'auth ENABLED' — будет 401)")

    # default 127.0.0.1, не localhost — на Windows + Docker Desktop иногда
    # на одном порту параллельно слушают два процесса (wslrelay на ::1 и
    # com.docker.backend на ::), и Python через httpx по умолчанию идёт
    # на IPv6 → попадает на зависший relay и получает 503. Явный IPv4
    # обходит проблему. На Linux/Mac на работу не влияет.
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_TESTING_PORT", default_port))
    url = f"http://{host}:{port}/sse"

    print(f"  target: {url}")
    print(f"  mode  : {args.mode}")
    print()

    # Прогон тестов может идти долго — увеличиваем call_timeout
    # под YAXUNIT_TIMEOUT (по умолчанию 900с), плюс небольшой запас
    # на сетевые накладные.
    yax_timeout = int(os.environ.get("YAXUNIT_TIMEOUT", "900"))
    async with MCPSession(
        url,
        init_timeout=30.0,
        call_timeout=float(yax_timeout + 60),
    ) as session:
        # ── 1. Health
        print("[1/2] test_runner_health() ...", end=" ", flush=True)
        t0 = time.monotonic()
        try:
            health = await call_tool(session, "test_runner_health", {})
        except Exception as e:
            print(f"FAIL ({e})")
            return 1
        dt = time.monotonic() - t0

        runner_status = health.get("status")
        checks = health.get("checks", {})
        if runner_status != "ok":
            print(f"FAIL ({dt:.1f}s)")
            print(f"  status={runner_status}")
            print(f"  checks={json.dumps(checks, ensure_ascii=False, indent=2)}")
            print(f"  raw={json.dumps(health, ensure_ascii=False)[:500]}")
            return 1
        # Проверка наличия shared volume с обеих сторон
        for required in (
            "payloads_volume_mounted",
            "payloads_volume_mounted_caller_side",
            "workspace_dir_mounted",
        ):
            if not checks.get(required):
                print(f"FAIL ({dt:.1f}s) — {required}=False")
                print(f"  checks={json.dumps(checks, ensure_ascii=False, indent=2)}")
                return 1
        print(f"OK ({dt:.1f}s)")

        # ── 2. Sandbox payload — копия demo-* в workspace
        try:
            demo_config, demo_tests = check_demo_payload_present()
            host_cfg, host_tests, rel = stage_into_workspace(demo_config, demo_tests)
        except Exception as e:
            print(f"FATAL: cannot stage demo payload: {e}")
            return 1

        config_arg = f"{rel}/config"
        tests_arg = f"{rel}/tests"

        # ── 3. test_run_path
        print(
            f"[2/2] test_run_path(config_path={config_arg!r}, "
            f"tests_path={tests_arg!r}, mode={args.mode!r}) ...",
            flush=True,
        )
        t0 = time.monotonic()
        try:
            result = await call_tool(
                session,
                "test_run_path",
                {"config_path": config_arg, "tests_path": tests_arg, "mode": args.mode},
            )
        except Exception as e:
            print(f"  FAIL ({e})")
            if not args.keep_payload:
                shutil.rmtree(WORKSPACE_DIR / rel, ignore_errors=True)
            return 1
        dt = time.monotonic() - t0

        status = result.get("status")
        n_tests = result.get("tests", 0)
        n_failures = result.get("failures", 0)
        n_errors = result.get("errors", 0)
        print(
            f"  status={status} tests={n_tests} failures={n_failures} "
            f"errors={n_errors} duration={dt:.1f}s"
        )

        ok = status == "passed" and n_tests > 0
        if args.verbose or not ok:
            log_tail = (result.get("log") or "")[-2000:]
            print("  --- pipeline.log (tail) ---")
            print(log_tail)
            print("  ---")

        # Cleanup
        if ok or not args.keep_payload:
            shutil.rmtree(WORKSPACE_DIR / rel, ignore_errors=True)
        else:
            print(f"  payload оставлен для отладки: workspace/{rel}")

        return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Integration smoke for mcp-testing ↔ yaxunit-stack")
    ap.add_argument("--mode", choices=["server", "file"], default="server",
                    help="Режим прогона (server требует серверной лицензии). "
                         "Для быстрого smoke можно --mode file.")
    ap.add_argument("--secret", default=None,
                    help="MCP_SHARED_SECRET. Без флага читается из env-переменной "
                         "или из .env в корне репозитория. Если на сервере включена "
                         "auth и секрет не найден — будет 401.")
    ap.add_argument("--verbose", action="store_true",
                    help="Печатать хвост pipeline.log даже при passed.")
    ap.add_argument("--keep-payload", action="store_true",
                    help="Не удалять staged-копию demo-* из workspace после прогона.")
    args = ap.parse_args()
    return asyncio.run(run_smoke(args))


if __name__ == "__main__":
    sys.exit(main())
