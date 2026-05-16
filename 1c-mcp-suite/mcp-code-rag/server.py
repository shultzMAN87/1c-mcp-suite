"""
MCP-сервер: RAG по кодовой базе проекта 1С
=============================================
Семантический поиск по BSL-модулям вашей конфигурации.
Индексирует .bsl файлы из workspace в Qdrant и позволяет
AI-агенту находить «как у нас в проекте обычно делают».

Инструменты:
  - code_search         — семантический поиск по коду проекта
  - code_similar        — найти похожие процедуры/фрагменты
  - code_patterns       — извлечь паттерны использования объекта
  - code_reindex        — переиндексировать кодовую базу

Зависимости:
  - Qdrant (векторная БД)
  - sentence-transformers (эмбеддинги)
"""

import os
import json
import re
import hashlib
import urllib.request
import urllib.error
from pathlib import Path
import logging

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("1C Code RAG")
logger = logging.getLogger(__name__)

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION_NAME = os.environ.get("CODE_COLLECTION", "project_code")
WORKSPACE_DIR = os.environ.get("WORKSPACE_DIR", "/workspace")
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")

# ─── Embedding модель ────────────────────────────────────────────────────

_model = None
_model_loaded = False


def _get_model():
    global _model, _model_loaded
    if not _model_loaded:
        try:
            from sentence_transformers import SentenceTransformer
            print(f"Загрузка модели {EMBEDDING_MODEL_NAME}...")
            _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
            print(f"  ✓ Модель загружена (dim={_model.get_sentence_embedding_dimension()})")
            _model_loaded = True
        except Exception as e:
            print(f"  ⚠ Не удалось загрузить модель: {e}")
            _model_loaded = True
    return _model


def _embed(text: str) -> list | None:
    model = _get_model()
    if model is None:
        return None
    is_e5 = "e5" in EMBEDDING_MODEL_NAME.lower()
    text_to_encode = f"query: {text}" if is_e5 else text
    return model.encode(text_to_encode).tolist()


def _embed_passage(text: str) -> list | None:
    model = _get_model()
    if model is None:
        return None
    is_e5 = "e5" in EMBEDDING_MODEL_NAME.lower()
    text_to_encode = f"passage: {text}" if is_e5 else text
    return model.encode(text_to_encode).tolist()


# ─── Qdrant клиент ───────────────────────────────────────────────────────

def _qdrant_request(method: str, path: str, data=None):
    url = f"{QDRANT_URL}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": str(e), "detail": body}
    except Exception as e:
        return {"error": str(e)}


def _qdrant_available() -> bool:
    try:
        result = _qdrant_request("GET", f"/collections/{COLLECTION_NAME}")
        return result.get("status") == "ok"
    except Exception:
        return False


def _ensure_collection():
    """Создаёт коллекцию если не существует."""
    model = _get_model()
    if not model:
        return False
    dim = model.get_sentence_embedding_dimension()
    result = _qdrant_request("PUT", f"/collections/{COLLECTION_NAME}", {
        "vectors": {"size": dim, "distance": "Cosine"},
    })
    return "error" not in result or "already exists" in str(result.get("detail", ""))


# ─── Парсер BSL-файлов ──────────────────────────────────────────────────

def _parse_bsl_file(filepath: str) -> list[dict]:
    """Парсит .bsl файл и извлекает процедуры/функции с контекстом."""
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            content = f.read()
    except Exception:
        try:
            with open(filepath, "r", encoding="windows-1251") as f:
                content = f.read()
        except Exception:
            return []

    chunks = []
    # Определяем тип модуля по пути
    path = Path(filepath)
    module_type = _detect_module_type(str(path))
    object_name = _detect_object_name(str(path))

    # Разбиваем на процедуры/функции
    proc_pattern = re.compile(
        r'(Процедура|Функция|Procedure|Function)\s+'
        r'([А-Яа-яёЁA-Za-z0-9_]+)\s*\(([^)]*)\)[^\n]*\n'
        r'(.*?)'
        r'Конец(?:Процедуры|Функции|Procedure|Function)',
        re.UNICODE | re.IGNORECASE | re.DOTALL
    )

    for match in proc_pattern.finditer(content):
        kind = match.group(1)
        name = match.group(2)
        params = match.group(3).strip()
        body = match.group(4)

        # Собираем комментарий перед процедурой
        start = match.start()
        pre_context = content[max(0, start - 300):start]
        comment_lines = []
        for line in reversed(pre_context.split("\n")):
            stripped = line.strip()
            if stripped.startswith("//"):
                comment_lines.insert(0, stripped[2:].strip())
            elif stripped == "":
                continue
            else:
                break

        # Формируем текст для индексации
        description = " ".join(comment_lines) if comment_lines else ""
        full_text = f"{object_name} / {name}({params})\n{description}\n{body[:500]}"

        chunk_id = hashlib.md5(f"{filepath}:{name}".encode()).hexdigest()

        chunks.append({
            "id": chunk_id,
            "text": full_text,
            "metadata": {
                "file": str(path.relative_to(WORKSPACE_DIR)) if str(path).startswith(WORKSPACE_DIR) else str(path),
                "object_name": object_name,
                "module_type": module_type,
                "procedure_name": name,
                "procedure_kind": "Функция" if "ункци" in kind.lower() or "unction" in kind.lower() else "Процедура",
                "params": params,
                "description": description,
                "lines_count": body.count("\n") + 1,
            },
        })

    # Если нет процедур — индексируем весь файл как один чанк
    if not chunks and content.strip():
        chunk_id = hashlib.md5(filepath.encode()).hexdigest()
        chunks.append({
            "id": chunk_id,
            "text": f"{object_name} / {module_type}\n{content[:1000]}",
            "metadata": {
                "file": str(path.relative_to(WORKSPACE_DIR)) if str(path).startswith(WORKSPACE_DIR) else str(path),
                "object_name": object_name,
                "module_type": module_type,
                "procedure_name": "(весь модуль)",
                "lines_count": content.count("\n") + 1,
            },
        })

    return chunks


def _detect_module_type(path: str) -> str:
    """Определяет тип модуля по пути файла."""
    if "ObjectModule" in path:
        return "МодульОбъекта"
    elif "ManagerModule" in path:
        return "МодульМенеджера"
    elif "Form" in path and "Module" in path:
        return "МодульФормы"
    elif "RecordSetModule" in path:
        return "МодульНабораЗаписей"
    elif "SessionModule" in path:
        return "МодульСеанса"
    elif "ManagedApplicationModule" in path:
        return "МодульПриложения"
    elif "ExternalConnectionModule" in path:
        return "МодульВнешнегоСоединения"
    elif "CommandModule" in path:
        return "МодульКоманды"
    elif "CommonModule" in path or "CommonModules" in path:
        return "ОбщийМодуль"
    else:
        return "Модуль"


def _detect_object_name(path: str) -> str:
    """Извлекает имя объекта метаданных из пути."""
    parts = Path(path).parts
    # Ищем каталог объекта (Catalogs/ИмяСправочника, Documents/ИмяДокумента и т.д.)
    kind_map = {
        "Catalogs": "Справочник",
        "Documents": "Документ",
        "DataProcessors": "Обработка",
        "Reports": "Отчёт",
        "InformationRegisters": "РегистрСведений",
        "AccumulationRegisters": "РегистрНакопления",
        "CommonModules": "ОбщийМодуль",
        "ChartsOfCharacteristicTypes": "ПВХ",
        "Enums": "Перечисление",
    }
    for i, part in enumerate(parts):
        if part in kind_map and i + 1 < len(parts):
            return f"{kind_map[part]}.{parts[i+1]}"
    return Path(path).stem


# ─── MCP инструменты ─────────────────────────────────────────────────────

@mcp.tool()
def code_search(query: str, limit: int = 5, module_type: str = "") -> str:
    """
    Семантический поиск по кодовой базе проекта.
    Находит процедуры и фрагменты кода, похожие по смыслу на запрос.

    Параметры:
      query       — описание того, что ищем ("обработка проведения документа", "валидация контрагента")
      limit       — количество результатов (по умолчанию 5)
      module_type — (опционально) фильтр по типу модуля: МодульОбъекта, МодульМенеджера, МодульФормы и т.д.
    """
    vector = _embed(query)
    if vector is None:
        return _fallback_search(query, limit, module_type)

    # Формируем фильтр
    search_filter = None
    if module_type:
        search_filter = {
            "must": [{"key": "module_type", "match": {"value": module_type}}]
        }

    result = _qdrant_request("POST", f"/collections/{COLLECTION_NAME}/points/search", {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
        "filter": search_filter,
    })

    if "error" in result:
        return _fallback_search(query, limit, module_type)

    hits = result.get("result", [])
    results = []
    for hit in hits:
        payload = hit.get("payload", {})
        results.append({
            "score": round(hit.get("score", 0), 3),
            "file": payload.get("file", ""),
            "object": payload.get("object_name", ""),
            "module_type": payload.get("module_type", ""),
            "procedure": payload.get("procedure_name", ""),
            "kind": payload.get("procedure_kind", ""),
            "description": payload.get("description", ""),
            "params": payload.get("params", ""),
            "lines": payload.get("lines_count", 0),
        })

    return json.dumps({
        "query": query,
        "results_count": len(results),
        "results": results,
        "hint": "Для просмотра полного кода процедуры — откройте файл через редактор",
    }, ensure_ascii=False, indent=2)


def _fallback_search(query: str, limit: int, module_type: str) -> str:
    """Текстовый поиск-фолбэк если Qdrant недоступен."""
    results = []
    keywords = [w.lower() for w in query.split() if len(w) > 2]

    for bsl_file in Path(WORKSPACE_DIR).rglob("*.bsl"):
        try:
            content = bsl_file.read_text(encoding="utf-8-sig")
        except Exception:
            continue

        if module_type:
            detected = _detect_module_type(str(bsl_file))
            if detected != module_type:
                continue

        content_lower = content.lower()
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            obj_name = _detect_object_name(str(bsl_file))
            mod_type = _detect_module_type(str(bsl_file))
            results.append({
                "score": score,
                "file": str(bsl_file.relative_to(WORKSPACE_DIR)),
                "object": obj_name,
                "module_type": mod_type,
                "procedure": "(текстовый поиск)",
                "lines": content.count("\n") + 1,
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return json.dumps({
        "query": query,
        "search_type": "fallback_text",
        "results_count": min(len(results), limit),
        "results": results[:limit],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def code_similar(procedure_code: str, limit: int = 5) -> str:
    """
    Найти похожие процедуры в проекте.
    Полезно для поиска дублирующегося кода и паттернов.

    Параметры:
      procedure_code — код процедуры/фрагмент для поиска похожих
      limit          — количество результатов
    """
    vector = _embed(procedure_code[:500])
    if vector is None:
        return json.dumps({"error": "Модель эмбеддингов недоступна"}, ensure_ascii=False)

    result = _qdrant_request("POST", f"/collections/{COLLECTION_NAME}/points/search", {
        "vector": vector,
        "limit": limit + 1,  # +1 т.к. может найти себя же
        "with_payload": True,
    })

    if "error" in result:
        return json.dumps({"error": f"Qdrant: {result['error']}"}, ensure_ascii=False)

    hits = result.get("result", [])
    results = []
    for hit in hits:
        payload = hit.get("payload", {})
        score = round(hit.get("score", 0), 3)
        if score > 0.99:  # Пропускаем точное совпадение (сам с собой)
            continue
        results.append({
            "similarity": score,
            "file": payload.get("file", ""),
            "object": payload.get("object_name", ""),
            "procedure": payload.get("procedure_name", ""),
            "description": payload.get("description", ""),
        })

    return json.dumps({
        "similar_count": len(results),
        "results": results[:limit],
        "hint": "similarity > 0.8 — вероятный дубликат, > 0.6 — похожий паттерн",
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def code_patterns(
    object_name: str,
    limit: int = 20,
    offset: int = 0,
    max_scan: int = 500,
) -> str:
    """
    Извлечь паттерны использования объекта метаданных в коде проекта.
    Показывает как объект используется в разных модулях.

    Параметры:
      object_name — имя объекта (например "Аук_Аукционы", "Справочники.Номенклатура")
      limit       — макс. детальных результатов (1-100, по умолчанию 20)
      offset      — смещение для пагинации
      max_scan    — защитный лимит: максимум найденных вхождений до остановки
                    (по умолчанию 500). Нужен для популярных объектов типа
                    "Контрагенты", которые могут встречаться в тысячах строк.

    Возвращает:
      - total_usages: общее число найденных вхождений (до max_scan)
      - scan_limit_hit: True если упёрлись в max_scan
      - usage_by_type: группировка типов использования с примерами
      - items: детальные вхождения постранично
      - has_more: есть ли ещё страницы
    """
    # Валидация параметров
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    max_scan = max(100, min(max_scan, 2000))

    patterns = []
    search_terms = [object_name]
    if "." in object_name:
        search_terms.append(object_name.split(".")[-1])

    scan_limit_hit = False

    for bsl_file in Path(WORKSPACE_DIR).rglob("*.bsl"):
        if len(patterns) >= max_scan:
            scan_limit_hit = True
            break
        try:
            content = bsl_file.read_text(encoding="utf-8-sig")
        except Exception:
            continue

        lines_b = content.split("\n")
        for i, line in enumerate(lines_b):
            if len(patterns) >= max_scan:
                scan_limit_hit = True
                break
            for term in search_terms:
                if term in line and not line.strip().startswith("//"):
                    context_start = max(0, i - 2)
                    context_end = min(len(lines_b), i + 3)
                    context = "\n".join(lines_b[context_start:context_end])

                    try:
                        rel = str(bsl_file.relative_to(WORKSPACE_DIR))
                    except ValueError:
                        rel = str(bsl_file)

                    patterns.append({
                        "file": rel,
                        "object": _detect_object_name(str(bsl_file)),
                        "module_type": _detect_module_type(str(bsl_file)),
                        "line_number": i + 1,
                        "usage_line": line.strip()[:200],
                        "context": context.strip()[:500],
                    })
                    break

    usage_types = defaultdict(list)
    for p in patterns:
        usage = _classify_usage(p["usage_line"])
        usage_types[usage].append(p)

    usage_by_type_summary = {
        utype: {
            "count": len(items),
            "examples": [
                {"file": it["file"], "line": it["line_number"], "code": it["usage_line"]}
                for it in items[:3]
            ],
        }
        for utype, items in sorted(usage_types.items(), key=lambda x: -len(x[1]))
    }

    total = len(patterns)
    end = offset + limit
    page = patterns[offset:end]
    has_more = end < total
    unique_files = sorted(set(p["file"] for p in patterns))

    response = {
        "object_name": object_name,
        "total_usages": total,
        "scan_limit_hit": scan_limit_hit,
        "returned": len(page),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "next_offset": end if has_more else None,
        "usage_by_type": usage_by_type_summary,
        "unique_files_count": len(unique_files),
        "unique_files_preview": unique_files[:10],
        "items": page,
    }

    if scan_limit_hit:
        response["hint"] = (
            f"Достигнут защитный лимит {max_scan} вхождений. Объект очень "
            "популярный — уточните поиск (укажите полное имя с префиксом типа, "
            "например 'Справочники.Контрагенты')."
        )
    elif total > limit:
        response["hint"] = (
            f"Показаны первые {limit} вхождений из {total}. "
            f"Для следующей страницы вызовите с offset={end}."
        )

    return json.dumps(response, ensure_ascii=False, indent=2)


from collections import defaultdict


def _classify_usage(line: str) -> str:
    """Классифицирует тип использования объекта в строке кода."""
    line_lower = line.lower().strip()
    if any(w in line_lower for w in ["создатьэлемент", "создатьдокумент", "создатьнаборзаписей"]):
        return "создание"
    elif any(w in line_lower for w in ["записать(", ".записать("]):
        return "запись"
    elif any(w in line_lower for w in ["найтипонаименованию", "найтипокоду", "найтипоссылке"]):
        return "поиск"
    elif "запрос" in line_lower or "выбрать" in line_lower:
        return "запрос"
    elif any(w in line_lower for w in ["удалить", "пометкаудаления"]):
        return "удаление"
    elif any(w in line_lower for w in ["движения", "регистратор"]):
        return "движения"
    elif any(w in line_lower for w in ["реквизитформы", "элементыформы"]):
        return "форма"
    elif "тип(" in line_lower or "typeof(" in line_lower:
        return "проверка_типа"
    else:
        return "прочее"


@mcp.tool()
def code_reindex() -> str:
    """
    Переиндексировать все .bsl файлы проекта в Qdrant.
    Запускайте после изменения кода конфигурации.
    """
    if not _ensure_collection():
        return json.dumps({"error": "Не удалось создать коллекцию Qdrant"}, ensure_ascii=False)

    # Собираем все .bsl файлы
    bsl_files = list(Path(WORKSPACE_DIR).rglob("*.bsl"))
    total_chunks = 0
    errors = 0

    for bsl_file in bsl_files:
        chunks = _parse_bsl_file(str(bsl_file))
        for chunk in chunks:
            vector = _embed_passage(chunk["text"])
            if vector is None:
                errors += 1
                continue

            point_id = int(hashlib.md5(chunk["id"].encode()).hexdigest()[:8], 16)
            result = _qdrant_request("PUT", f"/collections/{COLLECTION_NAME}/points", {
                "points": [{
                    "id": point_id,
                    "vector": vector,
                    "payload": chunk["metadata"],
                }]
            })
            if "error" not in result:
                total_chunks += 1
            else:
                errors += 1

    return json.dumps({
        "status": "completed",
        "files_processed": len(bsl_files),
        "chunks_indexed": total_chunks,
        "errors": errors,
    }, ensure_ascii=False, indent=2)


# ─── Инкрементальная переиндексация (для watcher'а, задача 2.3) ──────────

def _normalize_rel_path(filepath: str) -> str:
    """
    Приводит путь к тому же виду, в котором он хранится в payload.file.

    Логика индексации (_parse_bsl_file): если абсолютный путь начинается с
    WORKSPACE_DIR — пишется относительный, иначе — абсолютный. Воспроизводим
    то же здесь, чтобы фильтры Qdrant по file совпадали байт-в-байт.
    """
    p = Path(filepath)
    try:
        if p.is_absolute() and str(p).startswith(WORKSPACE_DIR):
            return str(p.relative_to(WORKSPACE_DIR))
    except ValueError:
        pass
    # Относительный путь — считаем, что уже в нужном виде.
    return str(p)


def _delete_points_for_file(rel_path: str) -> dict:
    """
    Удаляет из Qdrant все точки, у которых payload.file == rel_path.
    Возвращает ответ Qdrant как есть (или объект с ключом error).
    """
    return _qdrant_request(
        "POST",
        f"/collections/{COLLECTION_NAME}/points/delete",
        {
            "filter": {
                "must": [{"key": "file", "match": {"value": rel_path}}]
            }
        },
    )


def _index_chunks(chunks: list[dict]) -> tuple[int, int]:
    """Пишет чанки в Qdrant. Возвращает (indexed, errors)."""
    indexed = 0
    errors = 0
    for chunk in chunks:
        vector = _embed_passage(chunk["text"])
        if vector is None:
            errors += 1
            continue
        point_id = int(hashlib.md5(chunk["id"].encode()).hexdigest()[:8], 16)
        result = _qdrant_request("PUT", f"/collections/{COLLECTION_NAME}/points", {
            "points": [{
                "id": point_id,
                "vector": vector,
                "payload": chunk["metadata"],
            }]
        })
        if "error" not in result:
            indexed += 1
        else:
            errors += 1
    return indexed, errors


@mcp.tool()
def code_reindex_file(filepath: str) -> str:
    """
    Переиндексировать один .bsl/.os файл: удалить его старые чанки
    и записать свежие. Используется file-watcher'ом при modified/created.

    Параметры:
      filepath — абсолютный путь внутри контейнера (например, '/workspace/src/...')
                 или относительный от WORKSPACE_DIR.
    Возвращает JSON со статусом, количеством удалённых старых точек и новых чанков.
    """
    abs_path = Path(filepath)
    if not abs_path.is_absolute():
        abs_path = Path(WORKSPACE_DIR) / abs_path

    if not abs_path.exists():
        return json.dumps({
            "status": "skipped",
            "reason": "file_not_found",
            "filepath": str(abs_path),
        }, ensure_ascii=False)

    if abs_path.suffix.lower() not in (".bsl", ".os"):
        return json.dumps({
            "status": "skipped",
            "reason": "unsupported_extension",
            "filepath": str(abs_path),
        }, ensure_ascii=False)

    if not _ensure_collection():
        return json.dumps({"error": "Не удалось обеспечить коллекцию Qdrant"},
                          ensure_ascii=False)

    rel = _normalize_rel_path(str(abs_path))

    # 1) Удаляем старые точки этого файла (если были).
    delete_result = _delete_points_for_file(rel)
    delete_error = delete_result.get("error") if isinstance(delete_result, dict) else None

    # 2) Парсим файл и записываем свежие чанки.
    chunks = _parse_bsl_file(str(abs_path))
    indexed, errors = _index_chunks(chunks) if chunks else (0, 0)

    return json.dumps({
        "status": "reindexed",
        "file": rel,
        "chunks_indexed": indexed,
        "errors": errors,
        "delete_error": delete_error,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def code_remove_file(filepath: str) -> str:
    """
    Удалить из Qdrant все чанки, относящиеся к указанному файлу.
    Используется file-watcher'ом при событии deleted/moved.

    Параметры:
      filepath — абсолютный путь или относительный от WORKSPACE_DIR.
                 Физическое отсутствие файла допустимо (для deleted это норма).
    """
    abs_path = Path(filepath)
    if not abs_path.is_absolute():
        abs_path = Path(WORKSPACE_DIR) / abs_path

    rel = _normalize_rel_path(str(abs_path))
    result = _delete_points_for_file(rel)

    if isinstance(result, dict) and "error" in result:
        return json.dumps({
            "status": "error",
            "file": rel,
            "error": result.get("error"),
            "detail": result.get("detail", ""),
        }, ensure_ascii=False)

    return json.dumps({
        "status": "removed",
        "file": rel,
    }, ensure_ascii=False, indent=2)


# ─── Запуск ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    app = mcp.sse_app()
    port = int(os.environ.get("MCP_PORT", 8011))
    uvicorn.run(app, host="0.0.0.0", port=port)
