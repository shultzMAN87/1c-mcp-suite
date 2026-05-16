"""
MCP-сервер: SonarQube для 1С (BSL)
===================================
Обёртка над SonarQube Web API + sonar-scanner CLI.
Сервер SonarQube должен быть поднят отдельно (см. docker-compose.yml, сервис `sonarqube`)
с установленным плагином sonar-bsl-plugin-community (поддержка языка 1С BSL).

Инструменты:
  - sonar_scan_code:       отправить фрагмент кода на анализ (создаёт временный проект)
  - sonar_scan_directory:  запустить sonar-scanner по каталогу (выгрузка конфигурации 1С)
  - sonar_get_issues:      получить issues по projectKey из Web API
  - sonar_quality_gate:    статус Quality Gate проекта (pass/fail) — ключевой "валидатор"
  - sonar_list_projects:   список всех проектов на сервере
"""
import os
import json
import subprocess
import tempfile
import uuid
from pathlib import Path
import logging

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("1C SonarQube Validator")
logger = logging.getLogger(__name__)

SONAR_URL   = os.environ.get("SONAR_URL", "http://sonarqube:9000").rstrip("/")
SONAR_TOKEN = os.environ.get("SONAR_TOKEN", "")
SONAR_SCANNER = os.environ.get("SONAR_SCANNER", "sonar-scanner")
DEFAULT_PROJECT_PREFIX = os.environ.get("SONAR_PROJECT_PREFIX", "1c-agent")


def _auth():
    return (SONAR_TOKEN, "") if SONAR_TOKEN else None


def _slugify_module(name: str) -> str:
    """
    Превращает имя модуля 1С (в т.ч. на кириллице) в безопасный project_key.
    Сохраняет читаемость: 'ОбщегоНазначения.МойМодуль' -> 'obshchegonaznacheniya-moymodul'.
    SonarQube допускает в projectKey: буквы/цифры/'-'/'_'/'.'/':'.
    """
    import re, unicodedata
    # Транслитерация кириллицы в латиницу
    table = {
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh',
        'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
        'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts',
        'ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
    }
    out = []
    for ch in name.lower():
        out.append(table.get(ch, ch))
    s = "".join(out)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9._-]+", "-", s).strip("-._")
    return s or "module"


def _project_key_for_module(module_name: str) -> str:
    return f"{DEFAULT_PROJECT_PREFIX}-{_slugify_module(module_name)}"


def _api_get(path: str, params: dict | None = None) -> dict:
    url = f"{SONAR_URL}/api/{path.lstrip('/')}"
    try:
        r = httpx.get(url, params=params or {}, auth=_auth(), timeout=30.0)
        if r.status_code >= 400:
            return {"error": f"HTTP {r.status_code}", "body": r.text[-1000:]}
        return r.json()
    except Exception as e:
        return {"error": f"Сеть/SonarQube недоступен: {e}"}


def _escape_props(value: str) -> str:
    """
    Экранирует не-ASCII символы как \\uXXXX для java.util.Properties (.properties — ISO-8859-1).
    Без этого кириллица в sonar.projectName превращается в мохнатые 'Ð¾Ð±'.
    Также экранируем спецсимволы Java Properties: \\, =, :, #, !.
    """
    out = []
    for ch in value:
        code = ord(ch)
        if ch in ('\\', '=', ':', '#', '!'):
            out.append('\\' + ch)
        elif 0x20 <= code < 0x7F:
            out.append(ch)
        else:
            out.append(f'\\u{code:04x}')
    return ''.join(out)


def _run_scanner(src_dir: str, project_key: str, project_name: str) -> dict:
    """Запускает sonar-scanner по каталогу. Язык — bsl (требуется sonar-bsl-plugin)."""
    props = Path(src_dir) / "sonar-project.properties"
    props_text = (
        f"sonar.projectKey={_escape_props(project_key)}\n"
        f"sonar.projectName={_escape_props(project_name)}\n"
        f"sonar.sources=.\n"
        f"sonar.sourceEncoding=UTF-8\n"
        f"sonar.host.url={_escape_props(SONAR_URL)}\n"
        f"sonar.token={_escape_props(SONAR_TOKEN)}\n"
        # расширения файлов 1С, которые плагин bsl должен подхватить
        f"sonar.bsl.file.suffixes=.bsl,.os\n"
    )
    # .properties по спецификации Java — ISO-8859-1; после _escape_props там
    # уже только ASCII, так что любая кодировка корректна, но для надёжности — latin-1.
    props.write_text(props_text, encoding="latin-1")

    # Заставляем JVM scanner-а работать в UTF-8 (важно для имён файлов).
    env = os.environ.copy()
    env["SONAR_SCANNER_OPTS"] = (
        env.get("SONAR_SCANNER_OPTS", "") + " -Dfile.encoding=UTF-8"
    ).strip()

    try:
        res = subprocess.run(
            [SONAR_SCANNER, "-X"],
            cwd=src_dir,
            capture_output=True, text=True, timeout=600, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"error": "Таймаут sonar-scanner (600 сек)"}
    except FileNotFoundError:
        return {"error": f"sonar-scanner не найден (SONAR_SCANNER={SONAR_SCANNER})"}

    # Sonar scanner после успешного analysis пишет report-task.txt в
    # .scannerwork/ с ceTaskId — идентификатором фоновой задачи на сервере,
    # которая обрабатывает отчёт АСИНХРОННО. Без ожидания этой задачи
    # мы получим пусто из issues/search, даже если анализ нашёл проблемы:
    # "ANALYSIS SUCCESSFUL" в логе scanner'а означает только «отчёт залит»,
    # но не «issues уже в БД». Эта подножка стоила нам пары часов в 5.2.
    ce_task_id = ""
    report_task = Path(src_dir) / ".scannerwork" / "report-task.txt"
    if report_task.exists():
        try:
            for line in report_task.read_text(encoding="utf-8").splitlines():
                if line.startswith("ceTaskId="):
                    ce_task_id = line.split("=", 1)[1].strip()
                    break
        except OSError:
            pass

    return {
        "returncode": res.returncode,
        "ok": res.returncode == 0,
        "stdout_tail": res.stdout[-2000:] if res.stdout else "",
        "stderr_tail": res.stderr[-1500:] if res.stderr else "",
        "ce_task_id": ce_task_id,
    }


def _wait_for_ce_task(task_id: str, timeout_sec: float = 60.0,
                      poll_interval_sec: float = 0.7) -> dict:
    """Ждёт завершения Compute Engine task — той самой фоновой обработки
    отчёта, которая на сервере наполняет issues/measures после прихода
    отчёта от scanner'а.

    Возвращает {"status": "SUCCESS"|"FAILED"|"CANCELED"|"PENDING"|"IN_PROGRESS",
                "duration_ms": int, "error_message": str}.

    Если за timeout_sec не дождались — отдаём последний известный статус
    с пометкой '_timeout': True (caller сам решает, что вернуть пользователю).
    """
    import time
    deadline = time.monotonic() + timeout_sec
    last: dict = {}
    while time.monotonic() < deadline:
        data = _api_get("ce/task", {"id": task_id})
        task = data.get("task") if isinstance(data, dict) else None
        if not task:
            # API недоступен или {"error": ...}; не зацикливаемся
            return {"status": "UNKNOWN", "_api_response": data}
        status = task.get("status", "")
        last = {
            "status": status,
            "duration_ms": task.get("executionTimeMs"),
            "error_message": task.get("errorMessage", ""),
        }
        if status in ("SUCCESS", "FAILED", "CANCELED"):
            return last
        time.sleep(poll_interval_sec)
    last["_timeout"] = True
    return last


@mcp.tool()
def sonar_scan_code(code: str, module_name: str = "", project_key: str = "") -> str:
    """
    Отправить фрагмент BSL-кода в SonarQube на анализ.

    ВАЖНО: всегда передавайте `module_name` — имя модуля 1С, который вы редактируете
    (например, "ОбщегоНазначения" или "Документ.ЗаказПокупателя.МодульОбъекта").
    Это нужно, чтобы один и тот же модуль всегда попадал в ОДИН и тот же проект
    SonarQube — иначе на каждый сниппет создаётся новый проект, и история issues
    размазывается.

    Логика выбора project_key:
      1. если передан явный `project_key` — используется он;
      2. иначе если передан `module_name` — ключ = "<prefix>-<slug(module_name)>";
      3. иначе генерируется случайный (FALLBACK, нежелательно).

    Возвращает Quality Gate + список issues + ссылку на дашборд.
    """
    if not project_key:
        if module_name:
            project_key = _project_key_for_module(module_name)
        else:
            project_key = f"{DEFAULT_PROJECT_PREFIX}-snippet-{uuid.uuid4().hex[:8]}"

    # Имя файла внутри tmp-каталога — тоже из module_name, чтобы issues
    # ссылались на читаемое имя, а не на безликий Module.bsl.
    file_stem = _slugify_module(module_name) if module_name else "Module"
    file_name = f"{file_stem}.bsl"

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / file_name).write_text(code, encoding="utf-8-sig")
        project_name = module_name or f"Agent snippet {project_key}"
        scan = _run_scanner(tmp, project_key, project_name)

    if not scan.get("ok"):
        return json.dumps({"status": "scan_failed", "projectKey": project_key, **scan},
                          ensure_ascii=False, indent=2)

    # Дождаться, пока SonarQube доварит отчёт. Без этого issues/search
    # вернёт пусто на свежесозданном проекте (CE-task обычно ~5-15 сек).
    ce_task_id = scan.get("ce_task_id", "")
    ce_status: dict = {}
    if ce_task_id:
        ce_status = _wait_for_ce_task(ce_task_id, timeout_sec=60.0)
        # Если CE-task провалился — это не «нет issues», это другая ошибка.
        # Сразу отдаём её пользователю, не маскируя под successful scan.
        if ce_status.get("status") == "FAILED":
            return json.dumps({
                "status": "ce_task_failed",
                "projectKey": project_key,
                "ce_task_id": ce_task_id,
                "error_message": ce_status.get("error_message", ""),
                "sonar_ui": f"{SONAR_URL}/dashboard?id={project_key}",
            }, ensure_ascii=False, indent=2)

    qg = _api_get("qualitygates/project_status", {"projectKey": project_key})
    issues = _api_get("issues/search", {"componentKeys": project_key, "ps": 100})

    return json.dumps({
        "status": "scanned",
        "projectKey": project_key,
        "module": module_name or None,
        "qualityGate": qg.get("projectStatus", qg),
        "issues_total": issues.get("total", 0),
        "issues": [
            {
                "rule": i.get("rule"),
                "severity": i.get("severity"),
                "message": i.get("message"),
                "line": i.get("line"),
                "component": i.get("component"),
            }
            for i in issues.get("issues", [])[:50]
        ],
        "ce_task": {
            "id":          ce_task_id,
            "status":      ce_status.get("status"),
            "duration_ms": ce_status.get("duration_ms"),
            "timed_out":   ce_status.get("_timeout", False),
        } if ce_task_id else None,
        "sonar_ui": f"{SONAR_URL}/dashboard?id={project_key}",
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def sonar_scan_directory(dir_path: str, project_key: str, project_name: str = "") -> str:
    """
    Запустить sonar-scanner по каталогу (например, выгрузка конфигурации 1С в XML+BSL).
    Возвращает результат сканирования и ссылку на дашборд.
    """
    p = Path(dir_path)
    if not p.is_dir():
        return json.dumps({"error": f"Каталог не найден: {dir_path}"}, ensure_ascii=False)
    scan = _run_scanner(str(p), project_key, project_name or project_key)
    # На больших проектах CE-task может занять минуты — не блокируем тут
    # надолго, но базовый wait делаем (180с — компромисс между «scan_directory
    # обычно для больших проектов с долгим CE» и «не висеть бесконечно»).
    ce_task_id = scan.get("ce_task_id", "")
    if ce_task_id and scan.get("ok"):
        scan["ce_task"] = _wait_for_ce_task(ce_task_id, timeout_sec=180.0)
    scan["sonar_ui"] = f"{SONAR_URL}/dashboard?id={project_key}"
    return json.dumps(scan, ensure_ascii=False, indent=2)


@mcp.tool()
def sonar_quality_gate(project_key: str) -> str:
    """
    Получить статус Quality Gate проекта: OK / ERROR / WARN.
    Это ключевой "валидатор", который агент должен проверять после генерации кода.
    """
    data = _api_get("qualitygates/project_status", {"projectKey": project_key})
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def sonar_get_issues(project_key: str, severities: str = "", limit: int = 100) -> str:
    """
    Получить список issues проекта из SonarQube.
    severities — через запятую: BLOCKER,CRITICAL,MAJOR,MINOR,INFO (пусто = все).
    """
    params = {"componentKeys": project_key, "ps": max(1, min(limit, 500))}
    if severities:
        params["severities"] = severities
    data = _api_get("issues/search", params)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def sonar_list_projects() -> str:
    """Список проектов на SonarQube."""
    data = _api_get("projects/search", {"ps": 100})
    return json.dumps(data, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import uvicorn
    print(f"→ SonarQube URL: {SONAR_URL}")
    print(f"→ Token set:     {'yes' if SONAR_TOKEN else 'NO (анонимный режим)'}")
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    app = mcp.sse_app()
    # Задача 3.2: shared-secret-middleware. См. /app/mcp_auth.py.
    try:
        from mcp_auth import wrap_sse_app
        app = wrap_sse_app(app, server_name="sonarqube")
    except Exception as e:
        import sys as _sys
        _sys.stderr.write(f"[mcp-auth] wrap_sse_app failed: {e}\n")
    uvicorn.run(app, host="0.0.0.0", port=8014)
