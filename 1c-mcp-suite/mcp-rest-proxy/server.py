"""
MCP-сервер: REST-прокси к живой базе 1С (v2.1)
================================================
Выполняет запросы против реальной базы 1С через стандартный OData-интерфейс
или самописные HTTP-сервисы. С ЖЁСТКИМ read-only режимом по умолчанию.

Зачем:
  - Агент может не только ПИСАТЬ код запросов, но и ВЫПОЛНЯТЬ их против
    живой (тестовой!) базы, видеть результаты, отлаживать гипотезы.
  - В режиме read-only (по умолчанию) ЛЮБЫЕ модифицирующие операции
    блокируются ДО отправки HTTP-запроса.

Инструменты:
  - odata_list_entities    — список сущностей (справочники, документы, регистры),
                             доступных через OData
  - odata_get              — получить записи сущности (с $filter, $top, $skip)
  - odata_get_by_key       — получить конкретную запись по GUID
  - odata_metadata         — $metadata сущности (поля, типы, ключи)
  - http_service_call      — вызов самописного HTTP-сервиса 1С (только GET)
  - connection_info        — показать текущее подключение и режим
  - test_connection        — проверить доступность базы

Безопасность:
  - ONEC_READ_ONLY=true (по умолчанию) — блокирует POST/PUT/PATCH/DELETE
  - Любая попытка вызвать модифицирующий метод → немедленная ошибка
  - Белый список разрешённых HTTP-методов на уровне кода (не на уровне конфига)
  - Логирование всех запросов через модуль метрик

Конфигурация (через переменные окружения):
  ONEC_BASE_URL       — URL базы 1С, например https://server/base/odata/standard.odata
  ONEC_USER           — имя пользователя 1С
  ONEC_PASSWORD       — пароль
  ONEC_READ_ONLY      — "true" (по умолчанию) или "false"
  ONEC_TIMEOUT        — таймаут в секундах (по умолчанию 30)
  ONEC_HTTP_SERVICES_URL — (опционально) URL самописных HTTP-сервисов
"""

import os
import json
import base64
import urllib.parse
import httpx
from dataclasses import dataclass
import logging

from mcp.server.fastmcp import FastMCP

# Аудит-лог вызовов (задача 3.3). Если модуль недоступен —
# подсовываем no-op, чтобы сервер мог стартовать и вне контейнера
# (например в CI/локальных тестах).
try:
    from audit_log import record_http_call as _audit_record_http_call
except Exception:  # pragma: no cover
    def _audit_record_http_call(*_a, **_kw):  # type: ignore[no-redef]
        return None

mcp = FastMCP("1C REST Proxy")
logger = logging.getLogger(__name__)

# ─── Конфигурация ────────────────────────────────────────────────────────

ONEC_BASE_URL = os.environ.get("ONEC_BASE_URL", "").rstrip("/")
ONEC_USER = os.environ.get("ONEC_USER", "")
ONEC_PASSWORD = os.environ.get("ONEC_PASSWORD", "")
ONEC_READ_ONLY = os.environ.get("ONEC_READ_ONLY", "true").lower() in ("true", "1", "yes")
ONEC_TIMEOUT = int(os.environ.get("ONEC_TIMEOUT", "30"))
ONEC_HTTP_SERVICES_URL = os.environ.get("ONEC_HTTP_SERVICES_URL", "").rstrip("/")
# База 1С обычно живёт во внутренней сети — разрешаем приватные IP, если явно указано
ONEC_ALLOW_PRIVATE_NETWORK = os.environ.get("ONEC_ALLOW_PRIVATE_NETWORK", "true").lower() in ("true", "1", "yes")

# ЖЁСТКИЙ белый список методов для read-only режима
READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}
ALL_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}  # в любом режиме

# Максимум записей в одном ответе — защита от переполнения контекста
MAX_RECORDS_PER_RESPONSE = 100
DEFAULT_TOP = 20


# ─── Проверка безопасности ──────────────────────────────────────────────

class ReadOnlyViolation(Exception):
    """Исключение при попытке модифицирующей операции в read-only режиме."""
    pass


def _check_method_allowed(method: str) -> None:
    """
    Блокирует запрещённые HTTP-методы.
    В read-only режиме разрешены только GET/HEAD/OPTIONS.
    Raises ReadOnlyViolation если метод запрещён.
    """
    method_upper = method.upper()
    if ONEC_READ_ONLY:
        if method_upper not in READ_ONLY_METHODS:
            raise ReadOnlyViolation(
                f"Метод {method_upper} заблокирован: MCP работает в режиме READ-ONLY. "
                f"Разрешены только: {', '.join(sorted(READ_ONLY_METHODS))}. "
                f"Для модификации данных установите ONEC_READ_ONLY=false "
                f"(НЕ рекомендуется для production-баз)."
            )
    else:
        # Даже в read-write режиме блокируем совсем опасные методы
        # чтобы случайно ничего не грохнуть — модификация только через POST/PATCH/PUT
        # а DELETE и TRACE — никогда
        if method_upper in ("DELETE", "TRACE", "CONNECT"):
            raise ReadOnlyViolation(
                f"Метод {method_upper} всегда заблокирован в MCP-прокси "
                f"по соображениям безопасности."
            )


def _check_url_safe(url: str) -> None:
    """
    Проверяет URL на отсутствие подозрительных паттернов.
    Блокирует не-HTTP схемы, localhost и приватные IP-диапазоны (RFC1918, link-local).
    """
    import ipaddress
    import socket

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ReadOnlyViolation(f"Запрещённая схема URL: {parsed.scheme}")

    host = parsed.hostname or ""
    if not host:
        raise ReadOnlyViolation("URL без хоста")

    # Резолвим в IP и проверяем все адреса
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ReadOnlyViolation(f"Не удалось разрешить хост {host}: {e}")

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise ReadOnlyViolation(
                f"URL ведёт на запрещённый адрес ({ip_str}): "
                f"loopback/link-local/multicast заблокированы."
            )
        if ip.is_private and not ONEC_ALLOW_PRIVATE_NETWORK:
            raise ReadOnlyViolation(
                f"URL ведёт на приватный адрес ({ip_str}). "
                f"Установите ONEC_ALLOW_PRIVATE_NETWORK=true если это нужно."
            )


# ─── HTTP клиент ─────────────────────────────────────────────────────────

def _build_auth_header() -> dict:
    """Basic Auth заголовок."""
    if not ONEC_USER:
        return {}
    token = base64.b64encode(f"{ONEC_USER}:{ONEC_PASSWORD}".encode("utf-8")).decode()
    return {"Authorization": f"Basic {token}"}


def _http_request(
    url: str,
    method: str = "GET",
    params: dict | None = None,
    body: dict | None = None,
    extra_headers: dict | None = None,
) -> dict:
    """
    Универсальный HTTP-клиент с проверкой безопасности (httpx).
    Возвращает {'ok': bool, 'status': int, 'data': ..., 'error': str}

    Вызывает `audit_log.record_http_call` на всех выходах — это даёт
    задаче 3.3 URL/метод/код для записи в JSONL-аудит. При блокировке
    ДО отправки (ReadOnlyViolation) тоже пишется запись, чтобы
    видеть попытки модифицирующих операций в read-only режиме.
    """
    try:
        _check_method_allowed(method)
        _check_url_safe(url)
    except ReadOnlyViolation as e:
        _audit_record_http_call(url, method, http_code=None, response_size=0,
                                error=f"blocked_by=read_only_policy: {e}")
        return {"ok": False, "status": 403, "error": str(e), "blocked_by": "read_only_policy"}

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    headers.update(_build_auth_header())
    if extra_headers:
        headers.update(extra_headers)

    try:
        with httpx.Client(timeout=ONEC_TIMEOUT, follow_redirects=False) as client:
            resp = client.request(
                method.upper(),
                url,
                params=params or None,
                json=body if body is not None else None,
                headers=headers,
            )
    except httpx.TimeoutException as e:
        _audit_record_http_call(url, method, http_code=None, response_size=0,
                                error=f"timeout: {e}")
        return {"ok": False, "status": 0, "error": f"Таймаут запроса: {e}"}
    except httpx.RequestError as e:
        _audit_record_http_call(url, method, http_code=None, response_size=0,
                                error=f"network: {e}")
        return {"ok": False, "status": 0, "error": f"Сетевая ошибка: {e}"}
    except Exception as e:
        _audit_record_http_call(url, method, http_code=None, response_size=0,
                                error=f"unexpected: {e}")
        return {"ok": False, "status": 0, "error": f"Неожиданная ошибка: {e}"}

    if 200 <= resp.status_code < 300:
        try:
            parsed = resp.json()
        except Exception:
            parsed = {"raw": resp.text}
        _audit_record_http_call(str(resp.url), method, http_code=resp.status_code,
                                response_size=len(resp.content or b""), error=None)
        return {"ok": True, "status": resp.status_code, "data": parsed}

    _audit_record_http_call(str(resp.url), method, http_code=resp.status_code,
                            response_size=len(resp.content or b""),
                            error=f"HTTP {resp.status_code}")
    return {
        "ok": False,
        "status": resp.status_code,
        "error": f"HTTP {resp.status_code}: {resp.reason_phrase}",
        "response_body": resp.text[:1000],
    }


def _check_config() -> dict | None:
    """Проверяет что базовая конфигурация задана. Возвращает error-ответ если нет."""
    if not ONEC_BASE_URL:
        return {
            "error": "ONEC_BASE_URL не задан в переменных окружения. "
                     "Пример: ONEC_BASE_URL=https://server/base/odata/standard.odata",
        }
    return None


# ─── OData-специфичная обёртка ──────────────────────────────────────────

# Маппинг русских kind на английские OData-префиксы
KIND_TO_ODATA_PREFIX = {
    "Справочник": "Catalog",
    "Документ": "Document",
    "РегистрСведений": "InformationRegister",
    "РегистрНакопления": "AccumulationRegister",
    "РегистрБухгалтерии": "AccountingRegister",
    "РегистрРасчета": "CalculationRegister",
    "ПланСчетов": "ChartOfAccounts",
    "ПланВидовХарактеристик": "ChartOfCharacteristicTypes",
    "ПланВидовРасчета": "ChartOfCalculationTypes",
    "ПланОбмена": "ExchangePlan",
    "БизнесПроцесс": "BusinessProcess",
    "Задача": "Task",
    "Перечисление": "Enum",
    "Константа": "Constant",
    "Отчет": "Report",
    "Обработка": "DataProcessor",
}


def _resolve_entity_name(entity_name: str) -> str:
    """
    Преобразует 'Справочник.Контрагенты' → 'Catalog_Контрагенты'
    или возвращает как есть если уже в OData-формате.
    """
    if "_" in entity_name and "." not in entity_name:
        return entity_name  # уже OData-формат

    if "." in entity_name:
        parts = entity_name.split(".", 1)
        kind_ru = parts[0]
        name = parts[1]
        kind_en = KIND_TO_ODATA_PREFIX.get(kind_ru, kind_ru)
        return f"{kind_en}_{name}"

    return entity_name


# ─── MCP инструменты ─────────────────────────────────────────────────────

@mcp.tool()
def connection_info() -> str:
    """
    Информация о текущем подключении к базе 1С и режиме работы.
    ВАЖНО: показывает read-only статус — обязательно проверяйте перед работой.
    """
    info = {
        "base_url": ONEC_BASE_URL or "(не задан)",
        "user": ONEC_USER or "(не задан)",
        "password_set": bool(ONEC_PASSWORD),
        "read_only_mode": ONEC_READ_ONLY,
        "timeout_seconds": ONEC_TIMEOUT,
        "http_services_url": ONEC_HTTP_SERVICES_URL or "(не задан)",
        "allowed_methods": sorted(READ_ONLY_METHODS) if ONEC_READ_ONLY else ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH"],
        "max_records_per_response": MAX_RECORDS_PER_RESPONSE,
    }

    warnings = []
    if not ONEC_BASE_URL:
        warnings.append("⚠ ONEC_BASE_URL не задан — инструменты не будут работать")
    if not ONEC_READ_ONLY:
        warnings.append(
            "⚠⚠ READ-WRITE РЕЖИМ АКТИВЕН — возможны модифицирующие операции! "
            "Убедитесь что это ТЕСТОВАЯ база."
        )
    if ONEC_READ_ONLY:
        info["mode"] = "READ-ONLY (безопасный)"
    else:
        info["mode"] = "READ-WRITE (небезопасный — только для тестовых баз!)"

    if warnings:
        info["warnings"] = warnings

    return json.dumps(info, ensure_ascii=False, indent=2)


@mcp.tool()
def test_connection() -> str:
    """
    Проверить доступность базы 1С.
    Делает запрос к корню OData-сервиса.
    """
    err = _check_config()
    if err:
        return json.dumps(err, ensure_ascii=False)

    result = _http_request(f"{ONEC_BASE_URL}/", method="GET")

    if result["ok"]:
        return json.dumps({
            "status": "ok",
            "http_status": result["status"],
            "read_only_mode": ONEC_READ_ONLY,
            "base_url": ONEC_BASE_URL,
            "message": "Подключение к базе 1С работает",
        }, ensure_ascii=False, indent=2)
    else:
        return json.dumps({
            "status": "error",
            "http_status": result.get("status", 0),
            "error": result.get("error", "Unknown"),
            "hint": "Проверьте ONEC_BASE_URL, учётные данные, сетевую доступность "
                    "и что OData-интерфейс опубликован в базе 1С "
                    "(Администрирование → Публикация на веб-сервере → Standard OData)",
        }, ensure_ascii=False, indent=2)


@mcp.tool()
def odata_list_entities(filter_kind: str = "", limit: int = 50, offset: int = 0) -> str:
    """
    Список сущностей, доступных через OData.
    Парсит $metadata и возвращает список EntitySet'ов.

    Параметры:
      filter_kind — фильтр по типу: "Catalog", "Document", "InformationRegister" и т.д.
      limit       — макс. результатов (1-200, по умолчанию 50)
      offset      — смещение для пагинации
    """
    err = _check_config()
    if err:
        return json.dumps(err, ensure_ascii=False)

    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    # Запрашиваем корневой документ OData — там список EntitySet'ов в JSON
    result = _http_request(f"{ONEC_BASE_URL}/", method="GET")

    if not result["ok"]:
        return json.dumps({
            "error": result.get("error", "Unknown"),
            "hint": "Не удалось получить список сущностей",
        }, ensure_ascii=False)

    data = result.get("data", {})
    # OData v3/v4 формат: { "value": [ {"name": "Catalog_Контрагенты", "url": "..."} ] }
    entities = data.get("value", [])
    if not entities:
        return json.dumps({
            "warning": "OData вернул пустой список сущностей",
            "raw_response_keys": list(data.keys()) if isinstance(data, dict) else [],
        }, ensure_ascii=False)

    # Извлекаем имена
    all_names = []
    for e in entities:
        if isinstance(e, dict):
            name = e.get("name") or e.get("url", "")
            all_names.append(name)
        elif isinstance(e, str):
            all_names.append(e)

    # Фильтр по типу (префиксу)
    if filter_kind:
        # Принимаем и русский и английский
        prefix_en = KIND_TO_ODATA_PREFIX.get(filter_kind, filter_kind)
        filtered = [n for n in all_names if n.startswith(f"{prefix_en}_")]
    else:
        filtered = all_names

    total = len(filtered)
    end = offset + limit
    page = filtered[offset:end]

    return json.dumps({
        "total": total,
        "returned": len(page),
        "offset": offset,
        "limit": limit,
        "has_more": end < total,
        "next_offset": end if end < total else None,
        "filter_kind": filter_kind,
        "items": page,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def odata_metadata(entity_name: str) -> str:
    """
    Получить метаданные конкретной сущности: поля, типы, ключи, навигационные свойства.
    Использует OData запрос с $top=1 для получения одной записи и анализирует её структуру.

    Параметры:
      entity_name — имя сущности ("Справочник.Контрагенты" или "Catalog_Контрагенты")
    """
    err = _check_config()
    if err:
        return json.dumps(err, ensure_ascii=False)

    odata_name = _resolve_entity_name(entity_name)
    url = f"{ONEC_BASE_URL}/{odata_name}"

    result = _http_request(url, method="GET", params={"$top": 1, "$format": "json"})

    if not result["ok"]:
        return json.dumps({
            "error": result.get("error", "Unknown"),
            "entity_requested": entity_name,
            "entity_resolved": odata_name,
            "hint": f"Проверьте что сущность '{odata_name}' существует и пользователь имеет доступ",
        }, ensure_ascii=False)

    data = result.get("data", {})
    records = data.get("value", [])

    if not records:
        return json.dumps({
            "entity": odata_name,
            "warning": "Сущность существует, но записей нет — метаданные неполные",
        }, ensure_ascii=False, indent=2)

    sample = records[0]

    # Анализируем поля
    fields = {}
    nav_props = []

    for key, value in sample.items():
        if key.startswith("@") or key == "odata.metadata":
            continue

        # Навигационные свойства содержат вложенные объекты или ссылки
        if isinstance(value, dict):
            nav_props.append(key)
            continue

        if isinstance(value, list):
            # Табличная часть
            nav_props.append(f"{key} (табличная часть)")
            continue

        # Определяем тип по значению
        if value is None:
            type_guess = "null/unknown"
        elif isinstance(value, bool):
            type_guess = "Boolean"
        elif isinstance(value, int):
            type_guess = "Number (Integer)"
        elif isinstance(value, float):
            type_guess = "Number (Float)"
        elif isinstance(value, str):
            # Эвристики
            if len(value) == 36 and value.count("-") == 4:
                type_guess = "GUID (Ref)"
            elif "T" in value and ":" in value and len(value) > 15:
                type_guess = "DateTime"
            else:
                type_guess = "String"
        else:
            type_guess = f"Unknown ({type(value).__name__})"

        fields[key] = type_guess

    # Стандартные ключи OData 1С
    standard_keys = [k for k in fields.keys() if k in ("Ref_Key", "Code", "Description")]

    return json.dumps({
        "entity": odata_name,
        "entity_requested": entity_name,
        "sample_record_count": len(records),
        "fields_count": len(fields),
        "fields": fields,
        "standard_keys": standard_keys,
        "navigation_properties": nav_props,
        "hint": f"Пример запроса: odata_get('{entity_name}', filter=\"Description eq 'Иванов'\", top=10)",
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def odata_get(
    entity_name: str,
    filter: str = "",
    select: str = "",
    order_by: str = "",
    expand: str = "",
    top: int = DEFAULT_TOP,
    skip: int = 0,
) -> str:
    """
    Получить записи сущности через OData с фильтрацией и пагинацией.

    Параметры:
      entity_name — имя сущности ("Справочник.Контрагенты" или "Catalog_Контрагенты")
      filter      — OData $filter, например "Description eq 'Иванов'" или "Number gt 100"
      select      — OData $select: поля через запятую, например "Description,Code"
      order_by    — OData $orderby, например "Description asc"
      expand      — OData $expand для навигационных свойств, например "Владелец"
      top         — макс. записей (1-100, по умолчанию 20)
      skip        — пропустить N записей (пагинация)

    Примеры OData $filter:
      - "Description eq 'Иванов'"
      - "Number ge 100 and Number le 200"
      - "substringof('ООО', Description)"
      - "DeletionMark eq false"
      - "Ref_Key eq guid'12345678-1234-1234-1234-123456789abc'"
    """
    err = _check_config()
    if err:
        return json.dumps(err, ensure_ascii=False)

    top = max(1, min(top, MAX_RECORDS_PER_RESPONSE))
    skip = max(0, skip)

    odata_name = _resolve_entity_name(entity_name)
    url = f"{ONEC_BASE_URL}/{odata_name}"

    params = {
        "$format": "json",
        "$top": top,
        "$skip": skip,
    }
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if order_by:
        params["$orderby"] = order_by
    if expand:
        params["$expand"] = expand

    # Также запрашиваем $inlinecount для получения total (OData v3)
    # или $count=true для OData v4 — 1С обычно v3
    params["$inlinecount"] = "allpages"

    result = _http_request(url, method="GET", params=params)

    if not result["ok"]:
        return json.dumps({
            "error": result.get("error", "Unknown"),
            "response_body": result.get("response_body", "")[:500],
            "entity": odata_name,
            "filter": filter,
            "hint": ("Проверьте синтаксис $filter. Распространённые ошибки: "
                     "строки в одинарных кавычках, даты в формате datetime'YYYY-MM-DDTHH:MM:SS', "
                     "GUID в формате guid'XXX-XXX-XXX'"),
        }, ensure_ascii=False, indent=2)

    data = result.get("data", {})
    records = data.get("value", [])

    # Очищаем служебные поля из записей для экономии контекста
    cleaned_records = []
    for rec in records:
        if isinstance(rec, dict):
            cleaned = {k: v for k, v in rec.items() if not k.startswith("@") and not k.endswith("@navigationLinkUrl")}
            cleaned_records.append(cleaned)
        else:
            cleaned_records.append(rec)

    # Пробуем достать total count из разных полей OData
    total = data.get("odata.count") or data.get("@odata.count") or data.get("__count")
    if total is not None:
        try:
            total = int(total)
        except (ValueError, TypeError):
            total = None

    response = {
        "entity": odata_name,
        "returned": len(cleaned_records),
        "top": top,
        "skip": skip,
        "filter": filter or None,
    }
    if total is not None:
        response["total"] = total
        end = skip + len(cleaned_records)
        response["has_more"] = end < total
        if end < total:
            response["next_skip"] = end

    response["items"] = cleaned_records

    if len(cleaned_records) == top and total is None:
        response["hint"] = (
            f"Вернулось ровно {top} записей, возможно есть ещё. "
            f"Увеличьте top или используйте skip для следующей страницы."
        )

    return json.dumps(response, ensure_ascii=False, indent=2)


@mcp.tool()
def odata_get_by_key(entity_name: str, ref_key: str, expand: str = "") -> str:
    """
    Получить конкретную запись по её уникальному ключу (GUID/Ref_Key).

    Параметры:
      entity_name — имя сущности
      ref_key     — GUID записи (например "12345678-1234-1234-1234-123456789abc")
      expand      — навигационные свойства для разворачивания
    """
    err = _check_config()
    if err:
        return json.dumps(err, ensure_ascii=False)

    odata_name = _resolve_entity_name(entity_name)

    # Валидация GUID (мягкая)
    if len(ref_key) != 36 or ref_key.count("-") != 4:
        return json.dumps({
            "error": f"ref_key не похож на GUID: '{ref_key}'. "
                     f"Ожидается формат XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
        }, ensure_ascii=False)

    url = f"{ONEC_BASE_URL}/{odata_name}(guid'{ref_key}')"
    params = {"$format": "json"}
    if expand:
        params["$expand"] = expand

    result = _http_request(url, method="GET", params=params)

    if not result["ok"]:
        return json.dumps({
            "error": result.get("error", "Unknown"),
            "status": result.get("status", 0),
            "entity": odata_name,
            "ref_key": ref_key,
        }, ensure_ascii=False)

    data = result.get("data", {})
    # Очищаем служебные поля
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if not k.startswith("@") and k != "odata.metadata"}

    return json.dumps({
        "entity": odata_name,
        "ref_key": ref_key,
        "record": data,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def http_service_call(
    service_path: str,
    method: str = "GET",
    query_params: str = "",
    body: str = "",
) -> str:
    """
    Вызов самописного HTTP-сервиса 1С.
    В read-only режиме разрешены только GET/HEAD.

    Параметры:
      service_path — путь сервиса относительно ONEC_HTTP_SERVICES_URL,
                     например "/api/report/sales" или "my_service/version"
      method       — HTTP метод (GET в read-only режиме)
      query_params — query string, например "date_from=2025-01-01&client=Ivanov"
      body         — JSON-тело запроса (только для POST/PUT в read-write режиме)
    """
    if not ONEC_HTTP_SERVICES_URL:
        return json.dumps({
            "error": "ONEC_HTTP_SERVICES_URL не задан. Этот инструмент работает "
                     "с самописными HTTP-сервисами 1С, отдельно от OData.",
        }, ensure_ascii=False)

    # Жёсткая проверка метода ДО формирования запроса
    try:
        _check_method_allowed(method)
    except ReadOnlyViolation as e:
        _audit_record_http_call(
            url=f"{ONEC_HTTP_SERVICES_URL}/{service_path.lstrip('/')}",
            method=method, http_code=None, response_size=0,
            error=f"blocked_by=read_only_policy: {e}",
        )
        return json.dumps({
            "error": str(e),
            "blocked_by": "read_only_policy",
            "requested_method": method.upper(),
        }, ensure_ascii=False)

    # Собираем URL
    path = service_path.lstrip("/")
    url = f"{ONEC_HTTP_SERVICES_URL}/{path}"

    # Симметрично с _http_request — валидируем URL (защита от SSRF на динамическом пути)
    try:
        _check_url_safe(url)
    except ReadOnlyViolation as e:
        _audit_record_http_call(
            url=url, method=method, http_code=None, response_size=0,
            error=f"blocked_by=url_safety_policy: {e}",
        )
        return json.dumps({
            "error": str(e),
            "blocked_by": "url_safety_policy",
        }, ensure_ascii=False)

    params = {}
    if query_params:
        try:
            params = dict(urllib.parse.parse_qsl(query_params))
        except Exception as e:
            return json.dumps({"error": f"Неверный формат query_params: {e}"}, ensure_ascii=False)

    body_obj = None
    if body:
        try:
            body_obj = json.loads(body)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"body должен быть валидным JSON: {e}"}, ensure_ascii=False)

    result = _http_request(url, method=method, params=params, body=body_obj)

    # Обрезаем большие ответы
    data = result.get("data")
    if isinstance(data, (dict, list)):
        data_str = json.dumps(data, ensure_ascii=False)
        if len(data_str) > 50000:
            data = {
                "_truncated": True,
                "_original_size": len(data_str),
                "_preview": data_str[:10000] + "...",
            }

    return json.dumps({
        "url": url,
        "method": method.upper(),
        "ok": result.get("ok", False),
        "status": result.get("status", 0),
        "data": data,
        "error": result.get("error") if not result.get("ok") else None,
    }, ensure_ascii=False, indent=2)


# ─── Запуск ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    mcp.settings.transport_security.enable_dns_rebinding_protection = False

    # Проверяем конфиг при старте и выводим статус
    print("=" * 60)
    print("1C REST Proxy — MCP Server")
    print("=" * 60)
    print(f"Base URL: {ONEC_BASE_URL or '(НЕ ЗАДАН)'}")
    print(f"User: {ONEC_USER or '(не задан)'}")
    print(f"Read-only mode: {'ENABLED ✓' if ONEC_READ_ONLY else '!!! DISABLED !!!'}")
    if not ONEC_READ_ONLY:
        print("!!! ВНИМАНИЕ: Read-write режим активен !!!")
        print("!!! Используйте ТОЛЬКО для тестовых баз !!!")
    print("=" * 60)

    app = mcp.sse_app()
    # Задача 3.2: shared-secret-middleware (для legacy-пути; обычный старт
    # идёт через start.py, который тоже применяет middleware).
    try:
        from mcp_auth import wrap_sse_app
        app = wrap_sse_app(app, server_name="rest-proxy")
    except Exception as e:
        logging.getLogger(__name__).error("wrap_sse_app failed: %s", e)

    # Задача 3.3: обёртка tools аудит-логом (для legacy-пути).
    try:
        from audit_log import wrap_mcp_tools
        n = wrap_mcp_tools(mcp)
        print(f"[audit] rest-proxy: обёрнуто инструментов: {n}")
    except Exception as e:
        logging.getLogger(__name__).error("audit wrap failed: %s", e)

    port = int(os.environ.get("MCP_PORT", 8013))
    uvicorn.run(app, host="0.0.0.0", port=port)
