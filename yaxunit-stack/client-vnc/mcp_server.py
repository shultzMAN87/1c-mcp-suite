"""
mcp_server.py — MCP HTTP server for 1C YAxUnit test execution.

API:
  POST /run_tests              — run tests against uploaded zip archive (legacy)
  POST /run_tests_path         — run tests against payload on shared volume
  GET  /runs                   — list recent runs
  GET  /runs/{id}              — details + junit.xml + log
  GET  /runs/{id}/junit.xml    — raw JUnit XML
  GET  /health                 — readiness check
"""
import os
import shutil
import subprocess
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
import uvicorn

# ── Configuration ──
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "/tmp/runs"))
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "/reports"))
ALLURE_RESULTS_DIR = Path(os.environ.get("ALLURE_RESULTS", "/reports/allure-results"))
SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", "/app/scripts"))
PIPELINE_SCRIPT = SCRIPTS_DIR / "full_pipeline.sh"
PIPELINE_TIMEOUT = int(os.environ.get("PIPELINE_TIMEOUT", "600"))
DEFAULT_MODE = os.environ.get("PIPELINE_MODE", "server")

# Shared volume с mcp-testing. Все пути в POST /run_tests_path обязаны
# лежать строго под этим префиксом — это единственная защита от
# path-traversal на стороне раннера. Менять с осторожностью.
PAYLOADS_DIR = Path(os.environ.get("PAYLOADS_DIR", "/payloads")).resolve()

RUNS_REGISTRY: dict[str, dict] = {}
MAX_REGISTRY_SIZE = 100

app = FastAPI(title="1C YAxUnit MCP Server", version="1.0")


# ── Helpers ──

def _strip_ns(tag: str) -> str:
    """Remove XML namespace prefix from tag name."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def read_extension_name(config_xml_path: Path) -> str:
    """
    Read the extension name from Configuration.xml.

    Structure of Configuration.xml (1C XML dump):
        <MetaDataObject xmlns="...">
          <Configuration uuid="...">
            <InternalInfo>...</InternalInfo>
            <Properties>
              <Name>Tests</Name>          ← THIS is what we want
              <Synonym>...</Synonym>
              ...
            </Properties>
          </Configuration>
        </MetaDataObject>

    We find <Configuration> → <Properties> → <Name> explicitly,
    not the first <Name> in the tree (which could be inside InternalInfo).

    Forces UTF-8 parsing. Returns Unicode string as-is (Cyrillic OK).
    """
    if not config_xml_path.is_file():
        raise ValueError(f"Configuration.xml not found at {config_xml_path}")

    # Explicit UTF-8. Don't trust default encoding.
    parser = ET.XMLParser(encoding="utf-8")
    tree = ET.parse(str(config_xml_path), parser=parser)
    root = tree.getroot()

    # Find <Configuration> node (direct child of root <MetaDataObject>)
    configuration = None
    for child in root:
        if _strip_ns(child.tag) == "Configuration":
            configuration = child
            break
    if configuration is None:
        raise ValueError("No <Configuration> element in Configuration.xml")

    # Find <Properties> inside <Configuration>
    properties = None
    for child in configuration:
        if _strip_ns(child.tag) == "Properties":
            properties = child
            break
    if properties is None:
        raise ValueError("No <Properties> inside <Configuration>")

    # Find <Name> inside <Properties> (direct child only)
    for child in properties:
        if _strip_ns(child.tag) == "Name" and child.text:
            name = child.text.strip()
            if not name:
                raise ValueError("<Name> is empty")
            return name

    raise ValueError("No <Name> inside <Properties>")


def extract_zip_safely(zip_path: Path, dest: Path) -> None:
    """
    Extract zip with path traversal protection.
    Handles common encoding issues:
    - PowerShell Compress-Archive writes UTF-8 names but doesn't set the UTF-8 flag,
      so zipfile decodes them as cp437. We undo that by re-interpreting bytes as UTF-8.
    - If that fails, fall back to cp866.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            raw_name = info.filename
            if not (info.flag_bits & 0x800):
                # zipfile decoded as cp437 — get original bytes back
                try:
                    raw_bytes = raw_name.encode("cp437")
                    # Try UTF-8 first (PowerShell Compress-Archive case)
                    try:
                        raw_name = raw_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        raw_name = raw_bytes.decode("cp866", errors="replace")
                except UnicodeEncodeError:
                    pass

            target = (dest / raw_name).resolve()
            try:
                target.relative_to(dest.resolve())
            except ValueError:
                raise ValueError(f"Unsafe path in archive: {raw_name}")

            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def parse_junit(junit_path: Path) -> dict:
    """Sum tests/failures/errors across all <testsuite> elements. UTF-8 explicit."""
    if not junit_path.is_file():
        return {"tests": 0, "failures": 0, "errors": 0}
    try:
        parser = ET.XMLParser(encoding="utf-8")
        tree = ET.parse(str(junit_path), parser=parser)
    except ET.ParseError:
        return {"tests": 0, "failures": 0, "errors": 0}

    total = {"tests": 0, "failures": 0, "errors": 0}
    for elem in tree.getroot().iter():
        if _strip_ns(elem.tag) == "testsuite":
            for k in total:
                try:
                    total[k] += int(elem.get(k, "0"))
                except (TypeError, ValueError):
                    pass
    return total


def register_run(run_id: str, info: dict) -> None:
    RUNS_REGISTRY[run_id] = info
    if len(RUNS_REGISTRY) > MAX_REGISTRY_SIZE:
        oldest = sorted(RUNS_REGISTRY.items(), key=lambda x: x[1]["started_at"])[:10]
        for k, _ in oldest:
            RUNS_REGISTRY.pop(k, None)


# ── Endpoints ──

@app.get("/health")
def health():
    """Check pipeline script presence, YAxUnit, platform binary, license dir."""
    checks = {
        "pipeline_script": PIPELINE_SCRIPT.is_file(),
        "yaxunit_cfe": Path("/opt/yaxunit/yaxunit.cfe").is_file(),
        "platform": any(Path("/opt/1cv8").rglob("1cv8")),
        "license_dir_exists": Path("/var/1C/licenses").is_dir(),
        "license_files_present": any(
            Path("/var/1C/licenses").glob("*.lic")
        ) if Path("/var/1C/licenses").is_dir() else False,
        # Shared volume для path-based payload. Если False — /run_tests_path
        # будет ловить 400 на каждом вызове. Не делаем required для ok=ok,
        # потому что legacy /run_tests (через base64) от volume не зависит.
        "payloads_volume_mounted": PAYLOADS_DIR.is_dir(),
    }
    # Required-набор: всё, кроме payloads_volume_mounted (он опционален —
    # старый /run_tests без него работает).
    required = {k: v for k, v in checks.items() if k != "payloads_volume_mounted"}
    ok = all(required.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "degraded", "checks": checks},
    )


def _validate_mode(raw: Optional[str]) -> str:
    selected = (raw or DEFAULT_MODE).strip().lower()
    if selected not in ("file", "server"):
        raise HTTPException(400, f"Invalid mode '{selected}', expected 'file' or 'server'")
    return selected


def _execute_pipeline(
    config_dir: Path,
    tests_dir: Path,
    selected_mode: str,
    *,
    run_id: str,
    work_dir: Path,
    cleanup_work_dir_on_success: bool,
) -> dict:
    """
    Запустить full_pipeline.sh для подготовленных каталогов config/ и tests/.

    Общее ядро для /run_tests (zip + base64) и /run_tests_path (shared volume).
    Вызывающий обязан гарантировать, что config_dir и tests_dir существуют
    и доступны контейнеру 1С по тем же путям.

    Параметры:
      config_dir, tests_dir       — каталоги XML-выгрузок (читаются /full_pipeline.sh)
      selected_mode               — "file" | "server" (уже провалидировано)
      run_id                      — идентификатор прогона (12 hex)
      work_dir                    — каталог для pipeline.log; для path-режима
                                    можно указать любой scratch-каталог
      cleanup_work_dir_on_success — снести work_dir после passed-прогона.
                                    Для /run_tests=True (там лежит распакованный
                                    zip), для /run_tests_path=False (полезный
                                    payload пользователь чистит сам — см.
                                    ниже про path-traversal-safe удаление).

    Возвращает info_full (dict): run_id, status, stats, junit_xml, log, ...
    """
    started_at = time.time()

    # 1. Read extension name from tests/Configuration.xml — UTF-8 explicit
    try:
        ext_name = read_extension_name(tests_dir / "Configuration.xml")
    except Exception as e:
        raise HTTPException(
            400,
            f"Cannot read extension name from tests/Configuration.xml: {e}",
        )

    # 2. Build pipeline environment
    # CRITICAL: Cyrillic ext_name passed via env (UTF-8 native), not shell args.
    report_dir = REPORTS_DIR / run_id
    report_dir.mkdir(parents=True, exist_ok=True)
    junit_path = report_dir / "junit.xml"
    allure_run_dir = ALLURE_RESULTS_DIR / run_id

    env = {
        **os.environ,
        "PIPELINE_MODE": selected_mode,
        "SRC": str(config_dir),
        "TESTS": str(tests_dir),
        "EXT_NAME": ext_name,
        "RUN_ID": run_id,
        "REPORT_DIR": str(report_dir),
        "JUNIT_PATH": str(junit_path),
        "ALLURE_RESULTS": str(allure_run_dir),
        "REF": f"mcp_{run_id}",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }

    # 3. Run pipeline (no shell, list-form args, UTF-8 env)
    work_dir.mkdir(parents=True, exist_ok=True)
    log_path = work_dir / "pipeline.log"
    rc: int
    with open(log_path, "wb") as logf:
        try:
            proc = subprocess.run(
                ["bash", str(PIPELINE_SCRIPT), "--mode", selected_mode,
                 "--timeout", str(PIPELINE_TIMEOUT)],
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                timeout=PIPELINE_TIMEOUT + 60,
                check=False,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = 124

    duration = round(time.time() - started_at, 1)
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    stats = parse_junit(junit_path)

    if rc == 0 and stats["tests"] > 0 and stats["failures"] == 0 and stats["errors"] == 0:
        status = "passed"
    elif stats["tests"] > 0 and (stats["failures"] > 0 or stats["errors"] > 0):
        status = "failed"
    else:
        status = "error"

    junit_content = (
        junit_path.read_text(encoding="utf-8", errors="replace")
        if junit_path.is_file()
        else ""
    )

    info_summary = {
        "run_id": run_id,
        "status": status,
        "tests": stats["tests"],
        "failures": stats["failures"],
        "errors": stats["errors"],
        "duration_sec": duration,
        "extension": ext_name,
        "mode": selected_mode,
        "exit_code": rc,
        "started_at": started_at,
    }
    info_full = {**info_summary, "junit_xml": junit_content, "log": log_text}
    register_run(run_id, info_full)

    if cleanup_work_dir_on_success and status == "passed":
        shutil.rmtree(work_dir, ignore_errors=True)

    return info_full


@app.post("/run_tests")
async def run_tests(
    archive: UploadFile = File(...),
    mode: Optional[str] = Form(None),
):
    """
    Run YAxUnit tests against uploaded zip archive (legacy path).

    DEPRECATED for LLM-driven callers: для конфигураций > 5 МБ передача
    base64 раздувает токены. Новые интеграции должны использовать
    POST /run_tests_path с shared volume.

    Эндпоинт оставлен для обратной совместимости с prepare_and_run.sh,
    smoke-скриптами и ручными curl-вызовами.

    Archive structure (zip):
      config/                 — main configuration XML dump
        Configuration.xml
        ...
      tests/                  — test extension XML dump
        Configuration.xml     — <Properties><Name> = extension name (auto-detected)
        ...
      yaxunit.json            — optional, YAxUnit runtime config

    Form fields:
      archive (required)      — the zip file
      mode (optional)         — "file" or "server", default from PIPELINE_MODE env

    Returns JSON with status, stats, junit_xml, and log.
    """
    run_id = uuid.uuid4().hex[:12]
    selected_mode = _validate_mode(mode)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. Save and extract archive
    zip_path = run_dir / "input.zip"
    try:
        with open(zip_path, "wb") as f:
            shutil.copyfileobj(archive.file, f)
    finally:
        archive.file.close()

    try:
        extract_zip_safely(zip_path, run_dir)
    except Exception as e:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(400, f"Bad archive: {e}")

    config_dir = run_dir / "config"
    tests_dir = run_dir / "tests"
    if not config_dir.is_dir():
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(400, "Archive must contain 'config/' directory")
    if not tests_dir.is_dir():
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(400, "Archive must contain 'tests/' directory")

    info_full = _execute_pipeline(
        config_dir=config_dir,
        tests_dir=tests_dir,
        selected_mode=selected_mode,
        run_id=run_id,
        work_dir=run_dir,
        cleanup_work_dir_on_success=True,
    )
    return JSONResponse(content=info_full)


# ── Path-based payload (shared volume с mcp-testing) ─────────────────

class RunTestsPathRequest(BaseModel):
    payload_path: str = Field(
        ...,
        description="Абсолютный путь внутри контейнера раннера. Должен лежать "
                    "строго под PAYLOADS_DIR (по умолчанию /payloads). "
                    "Каталог обязан содержать config/ и tests/ (XML-выгрузки 1С).",
    )
    mode: Optional[str] = Field(
        None,
        description="'file' или 'server'. По умолчанию — значение PIPELINE_MODE.",
    )


def _resolve_payload(raw_path: str) -> Path:
    """
    Резолвит payload_path и убеждается, что он строго под PAYLOADS_DIR.

    Защита от path-traversal: симлинки, '..', абсолютные пути типа /etc —
    всё это после resolve() либо упрётся в проверку префикса, либо в
    отсутствие config/. Без этой проверки кто угодно с доступом к раннеру
    мог бы скормить ему /etc как config_dir.
    """
    candidate = Path(raw_path).resolve()
    try:
        candidate.relative_to(PAYLOADS_DIR)
    except ValueError:
        raise HTTPException(
            400,
            f"payload_path must be under {PAYLOADS_DIR}/, got '{raw_path}'",
        )
    if not candidate.is_dir():
        raise HTTPException(400, f"payload_path is not a directory: {raw_path}")
    return candidate


@app.post("/run_tests_path")
async def run_tests_path(req: RunTestsPathRequest):
    """
    Run YAxUnit tests against a payload already present on the shared volume.

    Структура payload (как у /run_tests, но не в zip, а в готовом каталоге):
      <payload_path>/
        config/
          Configuration.xml
          ...
        tests/
          Configuration.xml      ← имя расширения читается отсюда
          ...
        yaxunit.json             ← опционально

    Безопасность: payload_path обязан лежать под PAYLOADS_DIR. Симлинки
    наружу резолвятся и блокируются на этапе валидации.

    Caller-side контракт (mcp-testing):
      1. Сгенерировать payload_id и создать /payloads/<id>/{config,tests}.
      2. Скопировать туда XML-выгрузки из workspace.
      3. POST {"payload_path": "/payloads/<id>", "mode": "server"}.
      4. Ответ: тот же JSON, что у /run_tests (run_id, status, stats,
         junit_xml, log, extension, mode, exit_code, started_at).
      5. Caller сам решает, удалять payload или нет — раннер payload не
         трогает (полезен для повторных прогонов и отладки).

    Returns JSON with status, stats, junit_xml, and log.
    """
    selected_mode = _validate_mode(req.mode)
    payload = _resolve_payload(req.payload_path)
    config_dir = payload / "config"
    tests_dir = payload / "tests"
    if not config_dir.is_dir():
        raise HTTPException(400, f"Missing 'config/' under {req.payload_path}")
    if not tests_dir.is_dir():
        raise HTTPException(400, f"Missing 'tests/' under {req.payload_path}")

    run_id = uuid.uuid4().hex[:12]
    # Логи прогона лежат отдельно от payload (RUNS_DIR), чтобы caller мог
    # очищать payload не трогая историю прогонов.
    info_full = _execute_pipeline(
        config_dir=config_dir,
        tests_dir=tests_dir,
        selected_mode=selected_mode,
        run_id=run_id,
        work_dir=RUNS_DIR / run_id,
        cleanup_work_dir_on_success=True,
    )
    return JSONResponse(content=info_full)


@app.get("/runs")
def list_runs():
    """Last N runs, summary only (no junit_xml/log payloads)."""
    runs = []
    for rid, info in sorted(RUNS_REGISTRY.items(), key=lambda x: -x[1]["started_at"])[:50]:
        runs.append({k: v for k, v in info.items() if k not in ("junit_xml", "log")})
    return {"runs": runs}


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    info = RUNS_REGISTRY.get(run_id)
    if not info:
        raise HTTPException(404, "Run not found")
    return info


@app.get("/runs/{run_id}/junit.xml")
def get_junit(run_id: str):
    info = RUNS_REGISTRY.get(run_id)
    if not info:
        raise HTTPException(404, "Run not found")
    return PlainTextResponse(content=info.get("junit_xml", ""), media_type="application/xml")


if __name__ == "__main__":
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ALLURE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=8019)
