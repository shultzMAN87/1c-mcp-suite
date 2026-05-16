"""
MCP-сервер: Проверка синтаксиса BSL
====================================
Использует BSL Language Server для статического анализа кода 1С.
Поддерживает:
  - проверку синтаксиса фрагмента кода
  - анализ файла .bsl
  - список доступных диагностик
"""

import os
import json
import subprocess
import tempfile
from pathlib import Path
import logging

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("1C BSL Syntax Checker")
logger = logging.getLogger(__name__)

BSL_LS_JAR = os.environ.get("BSL_LS_JAR", "/opt/bsl-language-server/bsl-ls.jar")
BSL_LS_CONFIG = os.environ.get("BSL_LS_CONFIG", "")
JAVA_OPTS = os.environ.get("JAVA_OPTS", "-Xmx512m")


def _run_analysis(src_path: str, config_path: str = "") -> dict:
    """Запускает BSL Language Server в режиме анализа."""
    cmd = [
        "java", *JAVA_OPTS.split(),
        "-jar", BSL_LS_JAR,
        "--analyze",
        "--src", src_path,
        "--reporter", "json",
    ]
    if config_path:
        cmd.extend(["--configuration", config_path])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired:
        return {"error": "Таймаут анализа (120 сек)"}
    except FileNotFoundError:
        return {"error": f"BSL Language Server не найден: {BSL_LS_JAR}"}

    # BSL LS выводит JSON-отчёт в файл bsl-ls_report.json рядом с src
    report_path = Path(src_path) / "bsl-ls_report.json"
    if not report_path.exists():
        # Может быть рядом с jar
        alt = Path(BSL_LS_JAR).parent / "bsl-ls_report.json"
        if alt.exists():
            report_path = alt
        else:
            # Пробуем получить из stdout
            output = result.stdout.strip()
            if output:
                try:
                    return json.loads(output)
                except json.JSONDecodeError:
                    pass
            return {
                "diagnostics": [],
                "stdout": result.stdout[-2000:] if result.stdout else "",
                "stderr": result.stderr[-2000:] if result.stderr else "",
                "returncode": result.returncode,
            }

    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        # Чистим за собой
        report_path.unlink(missing_ok=True)
        return report
    except Exception as e:
        return {"error": f"Ошибка чтения отчёта: {e}"}


@mcp.tool()
def bsl_check_code(code: str) -> str:
    """
    Проверить фрагмент кода 1С (BSL) на синтаксические ошибки и соответствие стандартам.

    Параметр code — текст кода на языке 1С.
    Возвращает список диагностик с указанием строки, кода ошибки и описания.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        bsl_file = Path(tmpdir) / "Module.bsl"
        bsl_file.write_text(code, encoding="utf-8-sig")
        report = _run_analysis(tmpdir)

    if "error" in report:
        return json.dumps(report, ensure_ascii=False)

    # Извлекаем диагностики
    diagnostics = []
    if isinstance(report, dict) and "fileinfos" in report:
        for fi in report.get("fileinfos", []):
            for d in fi.get("diagnostics", []):
                diagnostics.append({
                    "line": d.get("range", {}).get("start", {}).get("line", 0) + 1,
                    "code": d.get("code", ""),
                    "message": d.get("message", ""),
                    "severity": d.get("severity", ""),
                    "source": d.get("source", ""),
                })
    elif isinstance(report, list):
        for fi in report:
            for d in fi.get("diagnostics", []):
                diagnostics.append({
                    "line": d.get("range", {}).get("start", {}).get("line", 0) + 1,
                    "code": d.get("code", ""),
                    "message": d.get("message", ""),
                    "severity": d.get("severity", ""),
                })

    if not diagnostics:
        return json.dumps({"status": "ok", "message": "Ошибок не найдено"}, ensure_ascii=False)

    return json.dumps({
        "status": "issues_found",
        "count": len(diagnostics),
        "diagnostics": diagnostics,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def bsl_check_file(file_path: str) -> str:
    """
    Проверить файл .bsl на диагностики.

    Параметр file_path — путь к файлу .bsl (внутри контейнера / смонтированного тома).
    """
    p = Path(file_path)
    if not p.exists():
        return json.dumps({"error": f"Файл не найден: {file_path}"}, ensure_ascii=False)

    report = _run_analysis(str(p.parent))

    diagnostics = []
    target_name = p.name.lower()
    entries = report.get("fileinfos", report if isinstance(report, list) else [])
    for fi in entries:
        fname = fi.get("path", fi.get("fileInfo", {}).get("path", ""))
        if target_name in fname.lower():
            for d in fi.get("diagnostics", []):
                diagnostics.append({
                    "line": d.get("range", {}).get("start", {}).get("line", 0) + 1,
                    "code": d.get("code", ""),
                    "message": d.get("message", ""),
                    "severity": d.get("severity", ""),
                })

    if not diagnostics:
        return json.dumps({"status": "ok", "message": f"В файле {p.name} ошибок не найдено"}, ensure_ascii=False)

    return json.dumps({
        "status": "issues_found",
        "file": str(p),
        "count": len(diagnostics),
        "diagnostics": diagnostics,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def bsl_check_directory(dir_path: str, limit: int = 50, offset: int = 0) -> str:
    """
    Проверить все .bsl файлы в каталоге.

    Параметр dir_path — путь к каталогу (например, каталог выгрузки конфигурации).
    limit/offset — пагинация по списку файлов с проблемами (по умолчанию первые 50).
    """
    p = Path(dir_path)
    if not p.is_dir():
        return json.dumps({"error": f"Каталог не найден: {dir_path}"}, ensure_ascii=False)

    report = _run_analysis(str(p))
    if "error" in report:
        return json.dumps(report, ensure_ascii=False)

    total = 0
    files_with_issues = 0
    summary = []
    entries = report.get("fileinfos", report if isinstance(report, list) else [])
    for fi in entries:
        diags = fi.get("diagnostics", [])
        if diags:
            files_with_issues += 1
            total += len(diags)
            fname = fi.get("path", fi.get("fileInfo", {}).get("path", "?"))
            summary.append({
                "file": fname,
                "issues": len(diags),
                "first_issue": diags[0].get("message", ""),
            })

    # Применяем пагинацию ко всему списку, а не вырезаем первые 50 молча
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    page = summary[offset:offset + limit]
    has_more = offset + limit < len(summary)

    return json.dumps({
        "total_issues": total,
        "files_with_issues": files_with_issues,
        "shown": len(page),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
        "summary": page,
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import uvicorn

    if not Path(BSL_LS_JAR).exists():
        print(f"⚠ BSL Language Server не найден: {BSL_LS_JAR}")
        print("  Скачайте с https://github.com/1c-syntax/bsl-language-server/releases")
    else:
        print(f"✓ BSL Language Server: {BSL_LS_JAR}")

    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    app = mcp.sse_app()
    # Задача 3.2: shared-secret-middleware. См. /app/mcp_auth.py.
    try:
        from mcp_auth import wrap_sse_app
        app = wrap_sse_app(app, server_name="bsl-checker")
    except Exception as e:
        import sys as _sys
        _sys.stderr.write(f"[mcp-auth] wrap_sse_app failed: {e}\n")
    uvicorn.run(app, host="0.0.0.0", port=8002)

