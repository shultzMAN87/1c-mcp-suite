#!/usr/bin/env python3
"""
Просмотр и фильтрация аудит-лога mcp-rest-proxy (задача 3.3).

Читает JSONL-лог, пишуемый контейнером mcp-rest-proxy в
/data/audit/rest-proxy.jsonl (volume `audit-logs`), с хоста —
через `docker cp` во временный файл, либо из уже смонтированного
пути (если вы смонтировали volume в файловую систему хоста).

Использование:
    # 20 последних записей
    python3 scripts/audit_tail.py --tail 20

    # Все ошибки за последний час в pretty-режиме
    python3 scripts/audit_tail.py --since 1h --status error --pretty

    # Только вызовы конкретного tool'а
    python3 scripts/audit_tail.py --tool http_service_call

    # Читать из конкретного файла (например, архив)
    python3 scripts/audit_tail.py --file ./rest-proxy.jsonl.1

Фильтры комбинируются по AND.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


CONTAINER_NAME = "mcp-rest-proxy"
CONTAINER_PATH = "/data/audit/rest-proxy.jsonl"


def _parse_duration(s: str) -> timedelta:
    """'10m' / '2h' / '3d' / '45s' → timedelta."""
    m = re.fullmatch(r"(\d+)([smhd])", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(
            f"--since принимает формат N[s|m|h|d], например 10m или 2h; "
            f"получено: {s!r}"
        )
    n, unit = int(m.group(1)), m.group(2)
    return {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }[unit]


def _fetch_from_docker() -> Path:
    """Копирует файл из контейнера mcp-rest-proxy во временный файл."""
    if not shutil.which("docker"):
        sys.stderr.write(
            "docker не найден в PATH. Используй --file <path> или запусти "
            "утилиту с хоста, где установлен docker.\n"
        )
        sys.exit(2)
    tmp = Path(tempfile.mkstemp(suffix=".jsonl", prefix="audit-tail-")[1])
    try:
        subprocess.run(
            ["docker", "cp", f"{CONTAINER_NAME}:{CONTAINER_PATH}", str(tmp)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace").strip()
        sys.stderr.write(
            f"docker cp не удался: {stderr or e}\n"
            f"Проверь что контейнер '{CONTAINER_NAME}' поднят "
            f"(docker ps) и что в нём существует файл {CONTAINER_PATH}.\n"
        )
        sys.exit(2)
    return tmp


def _iter_records(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                sys.stderr.write(f"строка {i}: невалидный JSON ({e})\n")


def _parse_ts(ts: str) -> datetime | None:
    try:
        # Наш формат: 2026-04-19T14:30:15.234Z
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None


def _matches(rec: dict, *, tool: str | None, status: str | None,
             since: datetime | None, http_code: int | None) -> bool:
    if tool and rec.get("tool") != tool:
        return False
    if status and rec.get("status") != status:
        return False
    if http_code is not None and rec.get("http_code") != http_code:
        return False
    if since is not None:
        ts = _parse_ts(rec.get("ts", ""))
        if ts is None or ts < since:
            return False
    return True


def _format_line(rec: dict, pretty: bool) -> str:
    if pretty:
        return json.dumps(rec, ensure_ascii=False, indent=2)
    # компактная одно-строковая сводка для терминала
    status = rec.get("status", "?")
    icon = {"ok": "✓", "error": "✗", "blocked": "⛔"}.get(status, "?")
    code = rec.get("http_code")
    code_s = str(code) if code is not None else "---"
    tool = rec.get("tool", "?")
    dur = rec.get("duration_ms", 0)
    url = rec.get("onec_url") or "(no url)"
    ts = rec.get("ts", "")
    err = rec.get("error") or ""
    err_s = f" — {err}" if err else ""
    return f"{ts}  {icon} {status:7s}  {code_s:>3}  {dur:>5}ms  {tool:22s}  {url}{err_s}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Просмотр JSONL-аудита mcp-rest-proxy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument(
        "--file", type=Path,
        help="путь к файлу JSONL-аудита (по умолчанию — копия из docker-контейнера)",
    )
    src.add_argument(
        "--stdin", action="store_true",
        help="читать JSONL со stdin (для pipe из docker exec/logs)",
    )

    ap.add_argument("--tail", type=int, default=None,
                    help="показать только последние N записей")
    ap.add_argument("--since", type=_parse_duration, default=None,
                    help="только записи не старше N[s|m|h|d], например 10m, 2h")
    ap.add_argument("--tool", type=str, default=None,
                    help="фильтр по имени tool'а (точное совпадение)")
    ap.add_argument("--status", choices=("ok", "error", "blocked"),
                    help="фильтр по статусу записи")
    ap.add_argument("--http-code", type=int, default=None,
                    help="фильтр по HTTP-коду (точное совпадение)")
    ap.add_argument("--pretty", action="store_true",
                    help="печатать каждую запись как pretty-JSON вместо краткой строки")
    ap.add_argument("--count", action="store_true",
                    help="только число совпавших записей, ничего не печатать")
    args = ap.parse_args()

    since_dt: datetime | None = None
    if args.since is not None:
        since_dt = datetime.now(timezone.utc) - args.since

    tmp_to_cleanup: Path | None = None

    if args.stdin:
        source = Path("/dev/stdin")
    elif args.file:
        source = args.file
        if not source.exists():
            sys.stderr.write(f"файл не найден: {source}\n")
            return 2
    else:
        tmp_to_cleanup = _fetch_from_docker()
        source = tmp_to_cleanup

    try:
        records = [
            r for r in _iter_records(source)
            if _matches(r, tool=args.tool, status=args.status,
                        since=since_dt, http_code=args.http_code)
        ]
        if args.tail is not None:
            records = records[-args.tail:]
        if args.count:
            print(len(records))
            return 0
        for r in records:
            print(_format_line(r, pretty=args.pretty))
    finally:
        if tmp_to_cleanup and tmp_to_cleanup.exists():
            try:
                tmp_to_cleanup.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
