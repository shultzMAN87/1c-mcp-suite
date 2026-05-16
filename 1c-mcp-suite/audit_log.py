"""
Аудит-лог вызовов MCP-инструментов (задача 3.3).

Зачем
=====
`mcp-rest-proxy` — единственный MCP-сервер в стеке, который умеет
ходить в *живую* базу 1С (OData и самописные HTTP-сервисы). Метрики
(`mcp_metrics.py`) дают сводку "сколько вызовов, какие быстрые, какие
падают", но не годятся для расследования инцидентов — там нет URL,
не видно параметров, нет привязки к сессии. Аудит-лог закрывает эту
дыру: по каждому реальному походу в 1С пишется строка в JSONL со
всем, что нужно для ответа на вопрос "кто, когда, куда, с каким
результатом".

Формат
======
JSONL — по одному JSON-объекту на строку. Читается и grep'ом, и
jq'ом, и любым lines-based tailer'ом (Loki/Vector/Promtail).
Ротация через `logging.handlers.RotatingFileHandler` — стандартный
механизм, на который можно положиться.

Размер и параметры
==================
Конфиг через переменные окружения (все опциональные, дефолты
подобраны консервативно):

  REST_PROXY_AUDIT_ENABLED=true           — включить/выключить
  REST_PROXY_AUDIT_PATH=/data/audit/rest-proxy.jsonl
  REST_PROXY_AUDIT_MAX_BYTES=10485760     — 10 MB
  REST_PROXY_AUDIT_BACKUP_COUNT=5         — держим до 5 ротаций
  REST_PROXY_AUDIT_INCLUDE_BODY=false     — писать ли тело запроса/ответа

Состав записи
=============
Всегда:
  ts             ISO-8601 UTC с миллисекундами
  tool           имя MCP-инструмента (odata_get, http_service_call, ...)
  params         аргументы tool'а как dict {name: value}
                 (длинные строки сокращаются; тела вырезаются если
                 AUDIT_INCLUDE_BODY=false)
  read_only_mode bool — статус ONEC_READ_ONLY на момент вызова
  onec_url       итоговый URL в 1С (последний, если вызов внутри tool
                 сделал несколько запросов)
  http_method    метод этого URL
  http_code      HTTP-код ответа (None если сетевая ошибка)
  duration_ms    полное время выполнения tool'а
  status         "ok" | "error" | "blocked"
                 blocked — ReadOnlyViolation или SSRF-защита сработала
                 ДО отправки запроса
  response_size  размер тела ответа в байтах (0 для blocked)
  error          строка с ошибкой или None
  remote_ip      IP клиента (из MCP-сессии; может быть "")
  mcp_session_id ID сессии MCP (может быть "")

При включённом REST_PROXY_AUDIT_INCLUDE_BODY=true дополнительно:
  request_body   dict/str или None — body, отправленный в 1С
  response_body  str — первые 50k символов ответа от 1С

Почему не пишем тела по умолчанию
=================================
Данные 1С — это ФИО контрагентов, суммы, реквизиты, договоры.
Писать всё это на диск каждому пользователю стека по умолчанию —
подарок для случайной утечки. Флагом включается осознанно теми,
кому реально нужно дебажить content-related баги.

Поточность
==========
`logging.handlers.RotatingFileHandler` в CPython thread-safe
(использует `threading.RLock` внутри `logging.Handler`).
MCP-серверы на FastMCP обрабатывают SSE через uvicorn workers —
это async, но каждый tool в итоге вызывается sync-кодом внутри
`ToolManager._run_tool`. Concurrent-запись из нескольких корутин
в один файл безопасна.

Передача HTTP-контекста
=======================
Tool возвращает JSON-строку, но нам нужен URL/HTTP-код, которые
известны только внутри `_http_request`. Чтобы не городить парсинг
выходной строки, audit_log предоставляет contextvars-контейнер
`current_http_context`, в который `_http_request` складывает свои
результаты. Обёртка tool'а его забирает. contextvars корректно
работают и в sync-, и в async-коде, и изолируют состояние между
одновременными вызовами tools.
"""
from __future__ import annotations

import contextvars
import functools
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ─── Конфигурация ────────────────────────────────────────────────────────

AUDIT_ENABLED = os.environ.get("REST_PROXY_AUDIT_ENABLED", "true").lower() in (
    "true", "1", "yes",
)
AUDIT_PATH = os.environ.get(
    "REST_PROXY_AUDIT_PATH", "/data/audit/rest-proxy.jsonl",
)
AUDIT_MAX_BYTES = int(os.environ.get("REST_PROXY_AUDIT_MAX_BYTES", str(10 * 1024 * 1024)))
AUDIT_BACKUP_COUNT = int(os.environ.get("REST_PROXY_AUDIT_BACKUP_COUNT", "5"))
AUDIT_INCLUDE_BODY = os.environ.get("REST_PROXY_AUDIT_INCLUDE_BODY", "false").lower() in (
    "true", "1", "yes",
)

# Обрезка длинных строк в params (чтобы в лог не попал гигантский base64)
_MAX_PARAM_STR = 2000
# Размер response_body (если включён AUDIT_INCLUDE_BODY)
_MAX_BODY_CHARS = 50_000

# Параметры, которые могут нести тело: для http_service_call это `body`,
# для odata_* тел нет. При AUDIT_INCLUDE_BODY=false — вырезаем.
_BODY_PARAM_NAMES = {"body"}

# Stderr-логгер для собственных ошибок audit_log — чтобы проблемы
# логгера не блокировали работу tool'а.
_self_log = logging.getLogger("audit_log")


# ─── HTTP-контекст, который заполняет _http_request ──────────────────────
#
# Каждый tool может сделать 0, 1 или много _http_request (например,
# odata_metadata делает один, а odata_list_entities может сделать
# несколько). Собираем ВСЕ вызовы в list — в audit-запись уходит
# последний (чаще всего именно он определил итог tool'а), но при
# AUDIT_INCLUDE_BODY=true можно увидеть весь trail в поле http_trail.

class HttpCallInfo(dict):
    """Удобный typed-ish wrapper для dict. Ключи:
    url, method, http_code (или None), response_size, error (или None).
    """
    pass


_http_context: contextvars.ContextVar[list[HttpCallInfo] | None] = contextvars.ContextVar(
    "rest_proxy_http_context", default=None,
)


def begin_tool_scope() -> None:
    """Сбрасывает HTTP-контекст для нового вызова tool'а."""
    _http_context.set([])


def record_http_call(
    url: str,
    method: str,
    http_code: int | None,
    response_size: int,
    error: str | None = None,
) -> None:
    """
    Вызывается из _http_request в rest-proxy сразу после ответа
    (или ловли исключения / блокировки). Накапливает trail, чтобы
    обёртка tool'а могла потом его прочесть.
    """
    ctx = _http_context.get()
    if ctx is None:
        # tool не под @audit — ничего не делаем, это легально
        return
    ctx.append(HttpCallInfo(
        url=url,
        method=method.upper() if method else "",
        http_code=http_code,
        response_size=int(response_size) if response_size else 0,
        error=error,
    ))


def get_http_trail() -> list[HttpCallInfo]:
    """Текущий trail, накопленный в этом tool-вызове."""
    return list(_http_context.get() or [])


# ─── MCP-контекст (remote_ip, session_id) ────────────────────────────────
#
# FastMCP в принципе умеет прокидывать информацию о запросе в tool,
# но API это делает только когда tool принимает параметр `context` —
# а rest-proxy tool'ы его не принимают и менять их ради этого
# противоречит задаче ("патч минимальный"). Поэтому ставим простой
# middleware в start.py, который кладёт remote_ip/session_id в
# contextvars перед вызовом tool'а.

_session_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "rest_proxy_session", default={},
)


def set_session(remote_ip: str = "", session_id: str = "") -> None:
    _session_ctx.set({"remote_ip": remote_ip or "", "session_id": session_id or ""})


def get_session() -> dict:
    return _session_ctx.get() or {}


# ─── Логгер и ротация ────────────────────────────────────────────────────

class _JsonlFormatter(logging.Formatter):
    """Форматирует запись как одну JSON-строку."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload = getattr(record, "audit_payload", None)
        if payload is None:
            # fallback — на случай если кто-то пошлёт обычное сообщение
            payload = {"ts": _iso_now(), "msg": record.getMessage()}
        try:
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return json.dumps({"ts": _iso_now(), "msg": "audit_format_error"})


_logger_lock = threading.Lock()
_audit_logger: logging.Logger | None = None


def _build_logger() -> logging.Logger | None:
    """Создаёт logger с RotatingFileHandler. Возвращает None если не получилось."""
    try:
        Path(AUDIT_PATH).parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _self_log.error("audit: не удалось создать каталог %s: %s", AUDIT_PATH, e)
        return None

    lg = logging.getLogger("rest_proxy_audit")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    # Если уже настроен (повторный import) — не дублируем handler
    if lg.handlers:
        return lg
    try:
        handler = logging.handlers.RotatingFileHandler(
            AUDIT_PATH,
            maxBytes=AUDIT_MAX_BYTES,
            backupCount=AUDIT_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as e:
        _self_log.error("audit: не удалось открыть файл %s: %s", AUDIT_PATH, e)
        return None

    handler.setFormatter(_JsonlFormatter())
    lg.addHandler(handler)
    return lg


def _get_logger() -> logging.Logger | None:
    global _audit_logger
    if not AUDIT_ENABLED:
        return None
    if _audit_logger is not None:
        return _audit_logger
    with _logger_lock:
        if _audit_logger is None:
            _audit_logger = _build_logger()
    return _audit_logger


# ─── Санитайзер параметров ───────────────────────────────────────────────

def _sanitize_params(
    args: tuple,
    kwargs: dict,
    param_names: list[str] | None,
) -> dict:
    """
    Из (args, kwargs) собирает {name: value}, используя param_names
    (порядок параметров tool'а из сигнатуры). Если имена недоступны —
    складываем позиционные как args0, args1, ...

    Обрезает длинные строки, вырезает body при AUDIT_INCLUDE_BODY=false.
    """
    merged: dict[str, Any] = {}
    # позиционные
    if param_names:
        for i, a in enumerate(args):
            if i < len(param_names):
                merged[param_names[i]] = a
            else:
                merged[f"args{i}"] = a
    else:
        for i, a in enumerate(args):
            merged[f"args{i}"] = a
    # keyword
    merged.update(kwargs)

    safe: dict[str, Any] = {}
    for name, val in merged.items():
        if name in _BODY_PARAM_NAMES and not AUDIT_INCLUDE_BODY:
            if val:
                safe[name] = f"<hidden, {len(str(val))} chars — enable REST_PROXY_AUDIT_INCLUDE_BODY>"
            else:
                safe[name] = ""
            continue
        safe[name] = _shorten(val)
    return safe


def _shorten(val: Any) -> Any:
    """Урезает длинные строки и вложенные структуры до _MAX_PARAM_STR."""
    if isinstance(val, str):
        if len(val) > _MAX_PARAM_STR:
            return val[:_MAX_PARAM_STR] + f"…(+{len(val) - _MAX_PARAM_STR}ch)"
        return val
    if isinstance(val, (int, float, bool)) or val is None:
        return val
    # dict/list — сериализуем и, если длинное, обрезаем как строку
    try:
        s = json.dumps(val, ensure_ascii=False, default=str)
    except Exception:
        s = str(val)
    if len(s) > _MAX_PARAM_STR:
        return s[:_MAX_PARAM_STR] + f"…(+{len(s) - _MAX_PARAM_STR}ch)"
    try:
        return json.loads(s)
    except Exception:
        return s


def _iso_now() -> str:
    # Один вызов now() с UTC tz — миллисекунды идут в ISO-формате
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") \
        + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


# ─── Декоратор: обернуть MCP tool ────────────────────────────────────────

def _classify_status(
    trail: list[HttpCallInfo],
    raised: BaseException | None,
) -> tuple[str, str | None]:
    """
    Определяет status/error для аудит-записи.

    Порядок:
      1) tool упал исключением     → ("error", str(exc))
      2) последний HTTP вернул 2xx → ("ok", None)
      3) последний HTTP заблокирован ДО отправки (нет http_code и
         есть флаг blocked_by в error)           → ("blocked", error)
      4) есть last.error (сетевая/timeout)       → ("error", error)
      5) last.http_code >= 400                   → ("error", f"HTTP <code>")
      6) trail пустой (tool не ходил в 1С)       → ("ok", None)
    """
    if raised is not None:
        return "error", f"{type(raised).__name__}: {raised}"
    if not trail:
        return "ok", None
    last = trail[-1]
    if last.get("http_code") is None:
        err = last.get("error") or ""
        if "blocked_by" in err or "заблокирован" in err or "Запрещённ" in err:
            return "blocked", err or "blocked"
        return "error", err or "network_error"
    code = last["http_code"]
    if 200 <= code < 300:
        return "ok", None
    return "error", f"HTTP {code}"


def audit_tool(tool_name: str, param_names: list[str] | None = None) -> Callable:
    """
    Декоратор-фабрика. Оборачивает callable (обычно MCP tool) так,
    чтобы каждый его вызов породил строку в JSONL-аудите.

    Нужна сигнатура в виде списка имён параметров, чтобы красиво
    разложить *args обратно в {name: value}. `inspect.signature`
    подтягивается автоматически, если param_names не передан.

    Поддерживает и sync-, и async-функции (FastMCP умеет оба вида).
    """
    def _decorate(func: Callable) -> Callable:
        # Автоопределение имён параметров
        names = param_names
        if names is None:
            try:
                import inspect
                sig = inspect.signature(func)
                names = [
                    p.name
                    for p in sig.parameters.values()
                    if p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY, p.KEYWORD_ONLY)
                    and p.name != "self"
                ]
            except (TypeError, ValueError):
                names = None

        # Не включаем аудит вовсе — возвращаем функцию как есть
        if not AUDIT_ENABLED:
            return func

        # Ленивая инициализация на первый вызов (чтобы ошибка открытия
        # файла не ломала старт сервера — уйдём в fallback).
        is_async = False
        try:
            import inspect
            is_async = inspect.iscoroutinefunction(func)
        except Exception:
            pass

        if is_async:
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                begin_tool_scope()
                start = time.time()
                raised: BaseException | None = None
                result = None
                try:
                    result = await func(*args, **kwargs)
                    return result
                except BaseException as e:
                    raised = e
                    raise
                finally:
                    try:
                        _emit_record(
                            tool=tool_name,
                            start=start,
                            args=args,
                            kwargs=kwargs,
                            param_names=names,
                            result=result,
                            raised=raised,
                        )
                    except Exception as log_err:  # pragma: no cover
                        _self_log.error("audit emit failed for %s: %s", tool_name, log_err)
            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            begin_tool_scope()
            start = time.time()
            raised: BaseException | None = None
            result = None
            try:
                result = func(*args, **kwargs)
                return result
            except BaseException as e:
                raised = e
                raise
            finally:
                try:
                    _emit_record(
                        tool=tool_name,
                        start=start,
                        args=args,
                        kwargs=kwargs,
                        param_names=names,
                        result=result,
                        raised=raised,
                    )
                except Exception as log_err:  # pragma: no cover
                    _self_log.error("audit emit failed for %s: %s", tool_name, log_err)
        return wrapper
    return _decorate


def _emit_record(
    tool: str,
    start: float,
    args: tuple,
    kwargs: dict,
    param_names: list[str] | None,
    result: Any,
    raised: BaseException | None,
) -> None:
    """Собирает JSON-payload и пишет в JSONL. Ловит всё — не должен падать."""
    logger = _get_logger()
    if logger is None:
        return

    duration_ms = int((time.time() - start) * 1000)
    trail = get_http_trail()
    status, error = _classify_status(trail, raised)

    # Собираем "главный" HTTP (последний) и при желании — полный trail
    onec_url = ""
    http_method = ""
    http_code: int | None = None
    response_size = 0
    if trail:
        last = trail[-1]
        onec_url = last.get("url", "") or ""
        http_method = last.get("method", "") or ""
        http_code = last.get("http_code", None)
        response_size = int(last.get("response_size") or 0)

    # read_only_mode и опциональные поля берём из env/caller-контекста
    read_only = os.environ.get("ONEC_READ_ONLY", "true").lower() in ("true", "1", "yes")
    sess = get_session()

    payload: dict[str, Any] = {
        "ts": _iso_now(),
        "tool": tool,
        "params": _sanitize_params(args, kwargs, param_names),
        "read_only_mode": read_only,
        "onec_url": onec_url,
        "http_method": http_method,
        "http_code": http_code,
        "duration_ms": duration_ms,
        "status": status,
        "response_size": response_size,
        "error": error,
        "remote_ip": sess.get("remote_ip", ""),
        "mcp_session_id": sess.get("session_id", ""),
    }

    if AUDIT_INCLUDE_BODY:
        # request body берём из kwargs/args по имени параметра "body"
        merged = dict(kwargs)
        if param_names:
            for i, a in enumerate(args):
                if i < len(param_names):
                    merged[param_names[i]] = a
        payload["request_body"] = merged.get("body", None)
        # response body — это то, что tool отдал в качестве строки
        if isinstance(result, (str, bytes)):
            rb = result.decode("utf-8", errors="replace") if isinstance(result, bytes) else result
            payload["response_body"] = rb[:_MAX_BODY_CHARS]
        else:
            payload["response_body"] = None
        if len(trail) > 1:
            # несколько HTTP-запросов внутри одного tool — сохраняем хвост
            payload["http_trail"] = list(trail)

    # Отправляем в logger
    rec = logging.LogRecord(
        name="rest_proxy_audit", level=logging.INFO, pathname="", lineno=0,
        msg="", args=None, exc_info=None,
    )
    rec.audit_payload = payload
    logger.handle(rec)


# ─── Программная обёртка всех tools MCP-объекта ──────────────────────────

def wrap_mcp_tools(mcp_obj) -> int:
    """
    Проходит по всем зарегистрированным tools FastMCP и оборачивает
    их в `audit_tool(tool_name)`. Возвращает количество обёрнутых.

    Идемпотентна: повторный вызов ничего не ломает благодаря атрибуту
    `__wrapped_by_audit__`, который ставим на функцию.
    """
    if not AUDIT_ENABLED:
        return 0
    # Принудительно инициализируем логгер, чтобы ошибка открытия файла
    # всплыла при старте, а не на первом запросе.
    lg = _get_logger()
    if lg is None:
        sys.stderr.write(
            "[audit] не удалось открыть аудит-файл — tools не обёрнуты\n"
        )
        return 0

    try:
        tools = getattr(mcp_obj._tool_manager, "_tools", {})
    except AttributeError:
        sys.stderr.write("[audit] _tool_manager._tools недоступен\n")
        return 0

    wrapped = 0
    for t in tools.values():
        fn = getattr(t, "fn", None)
        if fn is None:
            continue
        if getattr(fn, "__wrapped_by_audit__", False):
            continue
        new_fn = audit_tool(t.name)(fn)
        try:
            new_fn.__wrapped_by_audit__ = True  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass
        t.fn = new_fn
        wrapped += 1
    return wrapped
