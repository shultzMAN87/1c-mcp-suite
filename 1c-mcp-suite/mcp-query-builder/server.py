"""
MCP-сервер: Конструктор запросов 1С
=====================================
Строит запросы на языке 1С по описанию на естественном языке,
используя реальные метаданные конфигурации из Neo4j.

Инструменты:
  - query_build       — построить запрос по описанию задачи
  - query_join_hint   — подсказать как соединить две таблицы
  - query_fields      — получить доступные поля таблицы для запроса
  - query_validate    — проверить имена таблиц/полей в запросе
  - query_optimize    — предложить оптимизации для запроса

Зависимости:
  - Neo4j с проиндексированными метаданными (mcp-metadata-graph)
"""

import os
import json
import re
import base64
import urllib.request
import urllib.error
from collections import defaultdict
import logging

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("1C Query Builder")
logger = logging.getLogger(__name__)

NEO4J_URL = os.environ.get("NEO4J_URL", "http://neo4j:7474")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASS", "password1c")


# ─── Neo4j клиент ─────────────────────────────────────────────────────────

def _neo4j_query(cypher, parameters=None):
    auth = base64.b64encode(f"{NEO4J_USER}:{NEO4J_PASS}".encode()).decode()
    payload = json.dumps({
        "statements": [{
            "statement": cypher,
            "parameters": parameters or {},
        }]
    }).encode()
    req = urllib.request.Request(
        f"{NEO4J_URL}/db/neo4j/tx/commit",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    errors = result.get("errors", [])
    if errors:
        raise RuntimeError(f"Neo4j: {errors}")
    return result


def _neo4j_rows(cypher, params=None):
    result = _neo4j_query(cypher, params)
    columns = result["results"][0].get("columns", [])
    rows = []
    for data in result["results"][0].get("data", []):
        row = {}
        for i, col in enumerate(columns):
            row[col] = data["row"][i]
        rows.append(row)
    return rows


def _neo4j_available():
    try:
        result = _neo4j_query("MATCH (n:MetadataObject) RETURN count(n) as cnt")
        rows = result["results"][0]["data"]
        return rows and rows[0]["row"][0] > 0
    except Exception:
        return False


# ─── Маппинг типов объектов → имена таблиц запросов ─────────────────────

KIND_TO_QUERY_TABLE = {
    "Справочник": "Справочник",
    "Документ": "Документ",
    "РегистрСведений": "РегистрСведений",
    "РегистрНакопления": "РегистрНакопления",
    "РегистрБухгалтерии": "РегистрБухгалтерии",
    "РегистрРасчета": "РегистрРасчета",
    "ПланСчетов": "ПланСчетов",
    "ПланВидовХарактеристик": "ПланВидовХарактеристик",
    "ПланВидовРасчета": "ПланВидовРасчета",
    "ПланОбмена": "ПланОбмена",
    "БизнесПроцесс": "БизнесПроцесс",
    "Задача": "Задача",
    "Перечисление": "Перечисление",
    "Catalog": "Справочник",
    "Document": "Документ",
    "InformationRegister": "РегистрСведений",
    "AccumulationRegister": "РегистрНакопления",
    "AccountingRegister": "РегистрБухгалтерии",
    "CalculationRegister": "РегистрРасчета",
    "ChartOfAccounts": "ПланСчетов",
    "ChartOfCharacteristicTypes": "ПланВидовХарактеристик",
    "ChartOfCalculationTypes": "ПланВидовРасчета",
    "ExchangePlan": "ПланОбмена",
    "BusinessProcess": "БизнесПроцесс",
    "Task": "Задача",
    "Enum": "Перечисление",
}

# Виртуальные таблицы регистров
VIRTUAL_TABLES = {
    "РегистрНакопления": [
        "{name}.Остатки",
        "{name}.Обороты",
        "{name}.ОстаткиИОбороты",
    ],
    "РегистрСведений": [
        "{name}.СрезПоследних",
        "{name}.СрезПервых",
    ],
    "РегистрБухгалтерии": [
        "{name}.Остатки",
        "{name}.Обороты",
        "{name}.ОстаткиИОбороты",
        "{name}.ДвиженияССубконто",
    ],
}


# ─── Получение метаданных из Neo4j ──────────────────────────────────────

def _get_object_info(full_name: str) -> dict | None:
    """Получить информацию об объекте метаданных."""
    rows = _neo4j_rows("""
        MATCH (o:MetadataObject {full_name: $name})
        OPTIONAL MATCH (o)-[:HAS_ATTRIBUTE]->(a:Attribute)
        OPTIONAL MATCH (o)-[:HAS_TABULAR_SECTION]->(ts:TabularSection)
        OPTIONAL MATCH (ts)-[:HAS_ATTRIBUTE]->(tsa:Attribute)
        RETURN o.full_name as full_name, o.name as name, o.kind as kind,
               o.synonym as synonym,
               collect(distinct {name: a.name, type: a.type, synonym: a.synonym}) as attributes,
               collect(distinct {ts_name: ts.name, attr_name: tsa.name, attr_type: tsa.type}) as ts_attrs
    """, {"name": full_name})
    if not rows:
        return None
    row = rows[0]
    # Группируем атрибуты табличных частей
    ts_map = defaultdict(list)
    for ta in row.get("ts_attrs", []):
        if ta.get("ts_name"):
            ts_map[ta["ts_name"]].append({"name": ta["attr_name"], "type": ta.get("attr_type", "")})
    return {
        "full_name": row["full_name"],
        "name": row["name"],
        "kind": row["kind"],
        "synonym": row.get("synonym", ""),
        "attributes": [a for a in row.get("attributes", []) if a.get("name")],
        "tabular_sections": dict(ts_map),
    }


def _search_objects(query: str, kind: str = "", limit: int = 10) -> list:
    """Нечёткий поиск объектов."""
    if kind:
        rows = _neo4j_rows("""
            MATCH (o:MetadataObject)
            WHERE o.kind = $kind
              AND (toLower(o.name) CONTAINS toLower($q) OR toLower(o.synonym) CONTAINS toLower($q))
            RETURN o.full_name as full_name, o.name as name, o.kind as kind, o.synonym as synonym
            LIMIT $lim
        """, {"q": query, "kind": kind, "lim": limit})
    else:
        rows = _neo4j_rows("""
            MATCH (o:MetadataObject)
            WHERE toLower(o.name) CONTAINS toLower($q) OR toLower(o.synonym) CONTAINS toLower($q)
            RETURN o.full_name as full_name, o.name as name, o.kind as kind, o.synonym as synonym
            LIMIT $lim
        """, {"q": query, "lim": limit})
    return rows


def _find_join_path(obj1_full: str, obj2_full: str) -> list:
    """Найти связи между двумя объектами (через ссылки)."""
    rows = _neo4j_rows("""
        MATCH (a:MetadataObject {full_name: $n1})-[:HAS_ATTRIBUTE]->(attr:Attribute)
        WHERE attr.type CONTAINS $n2_name
        RETURN 'direct' as direction, attr.name as via_field, attr.type as field_type
        UNION
        MATCH (b:MetadataObject {full_name: $n2})-[:HAS_ATTRIBUTE]->(attr:Attribute)
        WHERE attr.type CONTAINS $n1_name
        RETURN 'reverse' as direction, attr.name as via_field, attr.type as field_type
    """, {
        "n1": obj1_full,
        "n2": obj2_full,
        "n1_name": obj1_full.split(".")[-1] if "." in obj1_full else obj1_full,
        "n2_name": obj2_full.split(".")[-1] if "." in obj2_full else obj2_full,
    })
    return rows


def _get_query_table_name(obj_info: dict) -> str:
    """Получить имя таблицы для запроса по объекту метаданных."""
    kind = obj_info["kind"]
    name = obj_info["name"]
    prefix = KIND_TO_QUERY_TABLE.get(kind, kind)
    return f"{prefix}.{name}"


# ─── MCP инструменты ─────────────────────────────────────────────────────

@mcp.tool()
def query_fields(object_name: str) -> str:
    """
    Получить все доступные поля объекта для использования в запросе.
    Включает реквизиты, стандартные реквизиты, табличные части и виртуальные таблицы.

    Параметры:
      object_name — имя объекта (полное "Справочник.Номенклатура" или частичное "Номенклатура")
    """
    # Поиск объекта
    if "." in object_name:
        info = _get_object_info(object_name)
    else:
        found = _search_objects(object_name, limit=1)
        if not found:
            return json.dumps({"error": f"Объект '{object_name}' не найден"}, ensure_ascii=False)
        info = _get_object_info(found[0]["full_name"])

    if not info:
        return json.dumps({"error": f"Объект '{object_name}' не найден в метаданных"}, ensure_ascii=False)

    table_name = _get_query_table_name(info)
    kind = info["kind"]

    # Стандартные реквизиты по типу объекта
    std_fields = []
    if kind in ("Справочник", "Catalog"):
        std_fields = ["Ссылка", "Код", "Наименование", "Родитель", "Владелец",
                       "ЭтоГруппа", "ПометкаУдаления", "Предопределённый"]
    elif kind in ("Документ", "Document"):
        std_fields = ["Ссылка", "Номер", "Дата", "Проведён", "ПометкаУдаления"]
    elif kind in ("РегистрСведений", "InformationRegister"):
        std_fields = ["Период", "Регистратор"]
    elif kind in ("РегистрНакопления", "AccumulationRegister"):
        std_fields = ["Период", "Регистратор", "НомерСтроки", "Активность"]

    # Виртуальные таблицы
    vt = []
    kind_ru = KIND_TO_QUERY_TABLE.get(kind, kind)
    if kind_ru in VIRTUAL_TABLES:
        vt = [t.format(name=table_name) for t in VIRTUAL_TABLES[kind_ru]]

    result = {
        "table_name": table_name,
        "object": info["full_name"],
        "synonym": info.get("synonym", ""),
        "standard_fields": std_fields,
        "attributes": [
            {"name": a["name"], "type": a.get("type", ""), "synonym": a.get("synonym", "")}
            for a in info.get("attributes", [])
        ],
        "tabular_sections": {
            ts_name: [{"name": a["name"], "type": a.get("type", "")} for a in attrs]
            for ts_name, attrs in info.get("tabular_sections", {}).items()
        },
        "virtual_tables": vt,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def query_join_hint(table1: str, table2: str) -> str:
    """
    Подсказать как соединить две таблицы в запросе 1С.
    Анализирует ссылочные связи между объектами метаданных.

    Параметры:
      table1 — первая таблица (например "Справочник.Номенклатура" или "Номенклатура")
      table2 — вторая таблица
    """
    # Резолвим имена
    def resolve(name):
        if "." in name:
            return name
        found = _search_objects(name, limit=1)
        return found[0]["full_name"] if found else name

    full1 = resolve(table1)
    full2 = resolve(table2)

    info1 = _get_object_info(full1)
    info2 = _get_object_info(full2)

    if not info1 or not info2:
        missing = []
        if not info1:
            missing.append(table1)
        if not info2:
            missing.append(table2)
        return json.dumps({"error": f"Не найдены объекты: {', '.join(missing)}"}, ensure_ascii=False)

    tbl1 = _get_query_table_name(info1)
    tbl2 = _get_query_table_name(info2)

    # Ищем прямые и обратные связи
    paths = _find_join_path(full1, full2)

    joins = []
    for p in paths:
        if p["direction"] == "direct":
            joins.append({
                "type": "ЛЕВОЕ СОЕДИНЕНИЕ" if len(joins) == 0 else "ВНУТРЕННЕЕ СОЕДИНЕНИЕ",
                "condition": f"Т1.{p['via_field']} = Т2.Ссылка",
                "explanation": f"{tbl1}.{p['via_field']} ссылается на {tbl2}",
                "query_fragment": f"""ЛЕВОЕ СОЕДИНЕНИЕ {tbl2} КАК Т2
    ПО Т1.{p['via_field']} = Т2.Ссылка""",
            })
        elif p["direction"] == "reverse":
            joins.append({
                "type": "ЛЕВОЕ СОЕДИНЕНИЕ",
                "condition": f"Т2.{p['via_field']} = Т1.Ссылка",
                "explanation": f"{tbl2}.{p['via_field']} ссылается на {tbl1}",
                "query_fragment": f"""ЛЕВОЕ СОЕДИНЕНИЕ {tbl2} КАК Т2
    ПО Т2.{p['via_field']} = Т1.Ссылка""",
            })

    if not joins:
        # Ищем через табличные части
        for ts_name, ts_attrs in info1.get("tabular_sections", {}).items():
            for a in ts_attrs:
                if info2["name"] in a.get("type", ""):
                    joins.append({
                        "type": "ЧЕРЕЗ ТАБЛИЧНУЮ ЧАСТЬ",
                        "condition": f"Т1ТЧ.{a['name']} = Т2.Ссылка",
                        "explanation": f"Табличная часть {tbl1}.{ts_name}.{a['name']} ссылается на {tbl2}",
                        "query_fragment": f"""ЛЕВОЕ СОЕДИНЕНИЕ {tbl1}.{ts_name} КАК Т1ТЧ
    ПО Т1ТЧ.Ссылка = Т1.Ссылка
ЛЕВОЕ СОЕДИНЕНИЕ {tbl2} КАК Т2
    ПО Т1ТЧ.{a['name']} = Т2.Ссылка""",
                    })

    result = {
        "table1": tbl1,
        "table2": tbl2,
        "joins_found": len(joins),
        "joins": joins,
    }

    if not joins:
        result["hint"] = (
            "Прямая ссылочная связь не найдена. Возможные варианты: "
            "1) Соединение через промежуточную таблицу (регистр); "
            "2) Соединение по значению реквизита (не по ссылке); "
            "3) Подзапрос."
        )

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def query_build(
    description: str,
    tables: str = "",
    fields: str = "",
    conditions: str = "",
    group_by: bool = False,
) -> str:
    """
    Построить запрос 1С по описанию задачи.
    Использует метаданные конфигурации для подстановки реальных имён.

    Параметры:
      description — описание что нужно получить ("остатки товаров на складе", "продажи за период по контрагентам")
      tables      — (опционально) через запятую имена таблиц, которые точно нужны
      fields      — (опционально) через запятую поля, которые нужны в результате
      conditions  — (опционально) условия отбора
      group_by    — нужна ли группировка (итоги/суммы)
    """
    # 1. Определяем таблицы из описания или параметров
    resolved_tables = []
    table_infos = {}

    if tables:
        for t in [x.strip() for x in tables.split(",") if x.strip()]:
            if "." in t:
                info = _get_object_info(t)
            else:
                found = _search_objects(t, limit=1)
                info = _get_object_info(found[0]["full_name"]) if found else None
            if info:
                resolved_tables.append(info)
                table_infos[info["name"]] = info
    else:
        # Пробуем извлечь объекты из описания
        keywords = [w for w in re.split(r'[\s,]+', description) if len(w) > 3]
        seen = set()
        for kw in keywords:
            found = _search_objects(kw, limit=3)
            for f in found:
                if f["full_name"] not in seen:
                    seen.add(f["full_name"])
                    info = _get_object_info(f["full_name"])
                    if info:
                        resolved_tables.append(info)
                        table_infos[info["name"]] = info

    if not resolved_tables:
        return json.dumps({
            "error": "Не удалось определить таблицы из описания. Укажите таблицы явно в параметре tables.",
            "hint": "Например: tables='Справочник.Номенклатура, РегистрНакопления.ТоварыНаСкладах'",
        }, ensure_ascii=False, indent=2)

    # 2. Строим запрос
    main_table = resolved_tables[0]
    main_tbl_name = _get_query_table_name(main_table)
    kind_ru = KIND_TO_QUERY_TABLE.get(main_table["kind"], main_table["kind"])

    # Определяем какую таблицу использовать (базовую или виртуальную)
    use_virtual = ""
    desc_lower = description.lower()
    if kind_ru == "РегистрНакопления":
        if any(w in desc_lower for w in ["остат", "баланс", "наличие"]):
            use_virtual = f"{main_tbl_name}.Остатки"
        elif any(w in desc_lower for w in ["оборот", "движени", "приход", "расход"]):
            use_virtual = f"{main_tbl_name}.Обороты"
        elif any(w in desc_lower for w in ["остат"]) and any(w in desc_lower for w in ["оборот"]):
            use_virtual = f"{main_tbl_name}.ОстаткиИОбороты"
    elif kind_ru == "РегистрСведений":
        if any(w in desc_lower for w in ["последн", "актуальн", "текущ"]):
            use_virtual = f"{main_tbl_name}.СрезПоследних"
        elif any(w in desc_lower for w in ["перв", "начальн"]):
            use_virtual = f"{main_tbl_name}.СрезПервых"

    source_table = use_virtual or main_tbl_name

    # 3. Формируем поля
    select_fields = []
    if fields:
        for f in [x.strip() for x in fields.split(",") if x.strip()]:
            select_fields.append(f"Т.{f}")
    else:
        # Автоподбор полей
        for attr in main_table.get("attributes", [])[:10]:
            select_fields.append(f"Т.{attr['name']}")
        if not select_fields:
            select_fields = ["Т.*"]

    # Соединения с другими таблицами
    join_clauses = []
    alias_idx = 2
    for other in resolved_tables[1:]:
        other_tbl = _get_query_table_name(other)
        paths = _find_join_path(main_table["full_name"], other["full_name"])
        if paths:
            p = paths[0]
            alias = f"Т{alias_idx}"
            if p["direction"] == "direct":
                join_clauses.append(
                    f"ЛЕВОЕ СОЕДИНЕНИЕ {other_tbl} КАК {alias}\n"
                    f"\tПО Т.{p['via_field']} = {alias}.Ссылка"
                )
            else:
                join_clauses.append(
                    f"ЛЕВОЕ СОЕДИНЕНИЕ {other_tbl} КАК {alias}\n"
                    f"\tПО {alias}.{p['via_field']} = Т.Ссылка"
                )
            alias_idx += 1

    # 4. Условия
    where_parts = []
    if conditions:
        where_parts.append(conditions)

    # 5. Собираем запрос
    query_lines = ["ВЫБРАТЬ"]
    if group_by:
        query_lines[0] = "ВЫБРАТЬ"
        # Если группировка — помечаем поля
        for i, f in enumerate(select_fields):
            sep = "," if i < len(select_fields) - 1 else ""
            query_lines.append(f"\t{f}{sep}")
    else:
        for i, f in enumerate(select_fields):
            sep = "," if i < len(select_fields) - 1 else ""
            query_lines.append(f"\t{f}{sep}")

    query_lines.append(f"ИЗ")
    query_lines.append(f"\t{source_table} КАК Т")

    for jc in join_clauses:
        query_lines.append(jc)

    if where_parts:
        query_lines.append("ГДЕ")
        for wp in where_parts:
            query_lines.append(f"\t{wp}")

    if group_by:
        group_fields = [f for f in select_fields if not any(
            agg in f.upper() for agg in ["СУММА(", "КОЛИЧЕСТВО(", "МАКСИМУМ(", "МИНИМУМ("]
        )]
        if group_fields:
            query_lines.append("СГРУППИРОВАТЬ ПО")
            for i, gf in enumerate(group_fields):
                sep = "," if i < len(group_fields) - 1 else ""
                query_lines.append(f"\t{gf}{sep}")

    query_text = "\n".join(query_lines)

    # 6. Формируем результат с объяснениями
    result = {
        "query": query_text,
        "source_table": source_table,
        "is_virtual_table": bool(use_virtual),
        "tables_used": [_get_query_table_name(t) for t in resolved_tables],
        "available_fields": {
            _get_query_table_name(t): {
                "attributes": [a["name"] for a in t.get("attributes", [])],
                "tabular_sections": list(t.get("tabular_sections", {}).keys()),
            }
            for t in resolved_tables
        },
        "hints": [],
    }

    # Подсказки
    if use_virtual:
        result["hints"].append(
            f"Используется виртуальная таблица '{use_virtual}'. "
            f"Параметры виртуальной таблицы задаются в скобках после имени."
        )
    if kind_ru == "РегистрНакопления" and not use_virtual:
        result["hints"].append(
            "Для регистра накопления рекомендуется использовать виртуальные таблицы "
            "(Остатки, Обороты) вместо основной таблицы движений."
        )
    if len(resolved_tables) > 1 and not join_clauses:
        result["hints"].append(
            "Прямые связи между таблицами не найдены автоматически. "
            "Уточните условия соединения вручную."
        )

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def query_validate(query_text: str) -> str:
    """
    Проверить имена таблиц и полей в тексте запроса 1С.
    Сверяет с реальными метаданными конфигурации.

    Параметры:
      query_text — текст запроса на языке 1С
    """
    if not _neo4j_available():
        return json.dumps({"error": "Neo4j недоступен"}, ensure_ascii=False)

    errors = []
    warnings = []
    info_messages = []

    # Извлекаем имена таблиц из ИЗ / СОЕДИНЕНИЕ
    table_pattern = re.compile(
        r'(?:ИЗ|СОЕДИНЕНИЕ|JOIN|FROM)\s+'
        r'((?:Справочник|Документ|РегистрСведений|РегистрНакопления|'
        r'РегистрБухгалтерии|ПланСчетов|ПланВидовХарактеристик|'
        r'Перечисление)\.[А-Яа-яёЁA-Za-z0-9_]+(?:\.[А-Яа-яёЁA-Za-z0-9_]+)?)',
        re.IGNORECASE | re.UNICODE
    )

    tables_in_query = table_pattern.findall(query_text)

    for tbl in tables_in_query:
        parts = tbl.split(".")
        if len(parts) >= 2:
            # Проверяем основной объект
            base_name = f"{parts[0]}.{parts[1]}"
            # Ищем в Neo4j
            rows = _neo4j_rows("""
                MATCH (o:MetadataObject)
                WHERE o.full_name = $name OR o.name = $short_name
                RETURN o.full_name as full_name, o.kind as kind
                LIMIT 1
            """, {"name": base_name, "short_name": parts[1]})

            if not rows:
                # Нечёткий поиск для подсказки
                similar = _search_objects(parts[1], limit=3)
                suggestion = ""
                if similar:
                    suggestion = f" Возможно: {', '.join(s['full_name'] for s in similar)}"
                errors.append(f"Таблица '{tbl}' не найдена в метаданных.{suggestion}")
            else:
                info_messages.append(f"✓ Таблица '{tbl}' → {rows[0]['full_name']}")

                # Если есть третья часть — проверяем виртуальную таблицу или ТЧ
                if len(parts) == 3:
                    vt_name = parts[2]
                    known_vt = ["Остатки", "Обороты", "ОстаткиИОбороты",
                                "СрезПоследних", "СрезПервых", "ДвиженияССубконто"]
                    obj_info = _get_object_info(rows[0]["full_name"])
                    ts_names = list(obj_info.get("tabular_sections", {}).keys()) if obj_info else []

                    if vt_name not in known_vt and vt_name not in ts_names:
                        errors.append(
                            f"'{vt_name}' — не виртуальная таблица и не табличная часть "
                            f"объекта {rows[0]['full_name']}."
                        )

    # Проверяем использование * (антипаттерн)
    if re.search(r'ВЫБРАТЬ\s+\*', query_text, re.IGNORECASE):
        warnings.append("SELECT * — выбирайте только нужные поля для производительности.")

    # Проверяем наличие ГДЕ в запросе к большим таблицам
    if not re.search(r'ГДЕ|WHERE', query_text, re.IGNORECASE) and tables_in_query:
        warnings.append("Запрос без условий ГДЕ — может вернуть слишком много данных.")

    # Проверка на типичные ошибки
    if re.search(r'В\s*\(ВЫБРАТЬ', query_text, re.IGNORECASE):
        warnings.append(
            "Подзапрос в операторе В() может быть медленным. "
            "Рассмотрите использование СОЕДИНЕНИЕ вместо подзапроса."
        )

    result = {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "info": info_messages,
        "tables_checked": len(tables_in_query),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def query_optimize(query_text: str) -> str:
    """
    Предложить оптимизации для запроса 1С.
    Анализирует текст запроса и даёт рекомендации по производительности.

    Параметры:
      query_text — текст запроса
    """
    recommendations = []
    query_upper = query_text.upper()

    # 1. Использование виртуальных таблиц
    if "РЕГИСТРНАКОПЛЕНИЯ" in query_upper.replace(" ", "") or "РЕГИСТР НАКОПЛЕНИЯ" in query_upper:
        if not any(vt in query_upper for vt in ["ОСТАТКИ", "ОБОРОТЫ", "ОСТАТКИИОБОРОТЫ"]):
            recommendations.append({
                "priority": "HIGH",
                "rule": "Виртуальные таблицы",
                "issue": "Запрос к основной таблице регистра накопления вместо виртуальной",
                "fix": "Используйте .Остатки(), .Обороты() или .ОстаткиИОбороты() — "
                       "они оптимизированы на уровне платформы",
            })

    # 2. Параметры виртуальных таблиц
    vt_pattern = re.compile(r'\.(Остатки|Обороты|ОстаткиИОбороты|СрезПоследних|СрезПервых)\s*(?!\()', re.UNICODE)
    if vt_pattern.search(query_text):
        recommendations.append({
            "priority": "HIGH",
            "rule": "Параметры виртуальных таблиц",
            "issue": "Виртуальная таблица без параметров — условия в ГДЕ не оптимизируются",
            "fix": "Перенесите условия отбора в параметры виртуальной таблицы: "
                   ".Остатки(&Период, Номенклатура = &Номенклатура)",
        })

    # 3. Соединение с подзапросом
    if re.search(r'СОЕДИНЕНИЕ\s*\(?\s*ВЫБРАТЬ', query_text, re.IGNORECASE):
        recommendations.append({
            "priority": "MEDIUM",
            "rule": "Соединение с подзапросом",
            "issue": "Соединение с подзапросом может быть медленным",
            "fix": "Рассмотрите использование временных таблиц (ПОМЕСТИТЬ ВтИмя) "
                   "вместо подзапросов в соединениях",
        })

    # 4. РАЗЛИЧНЫЕ
    if "РАЗЛИЧНЫЕ" in query_upper or "DISTINCT" in query_upper:
        recommendations.append({
            "priority": "LOW",
            "rule": "РАЗЛИЧНЫЕ (DISTINCT)",
            "issue": "РАЗЛИЧНЫЕ добавляет сортировку — проверьте, нужно ли это",
            "fix": "Если дубликаты появляются из-за соединений — "
                   "исправьте соединения вместо РАЗЛИЧНЫЕ",
        })

    # 5. Функции в условиях
    func_in_where = re.search(
        r'ГДЕ.*(?:ПОДСТРОКА|SUBSTRING|ВЫРАЗИТЬ|CAST|ГОД|МЕСЯЦ|ДЕНЬ)\s*\(',
        query_text, re.IGNORECASE | re.DOTALL
    )
    if func_in_where:
        recommendations.append({
            "priority": "HIGH",
            "rule": "Функции в условиях",
            "issue": "Функции в ГДЕ препятствуют использованию индексов",
            "fix": "По возможности перенесите вычисления на сторону параметров, "
                   "а не применяйте к полям таблицы",
        })

    # 6. УПОРЯДОЧИТЬ ПО без индекса
    if re.search(r'УПОРЯДОЧИТЬ\s+ПО|ORDER\s+BY', query_text, re.IGNORECASE):
        recommendations.append({
            "priority": "LOW",
            "rule": "Сортировка",
            "issue": "УПОРЯДОЧИТЬ ПО может быть медленным на больших выборках",
            "fix": "Убедитесь, что поля сортировки входят в индекс, "
                   "или ограничьте выборку",
        })

    # 7. Отсутствие ПЕРВЫЕ/TOP для больших таблиц
    if "ПЕРВЫЕ" not in query_upper and "TOP" not in query_upper:
        if not re.search(r'ГДЕ|WHERE', query_text, re.IGNORECASE):
            recommendations.append({
                "priority": "MEDIUM",
                "rule": "Отсутствие ограничения выборки",
                "issue": "Запрос без ПЕРВЫЕ и без условий вернёт все записи",
                "fix": "Добавьте ПЕРВЫЕ N или условия ГДЕ для ограничения выборки",
            })

    if not recommendations:
        recommendations.append({
            "priority": "INFO",
            "rule": "Общая оценка",
            "issue": "Явных проблем с производительностью не найдено",
            "fix": "Для глубокого анализа проверьте план запроса в Конфигураторе "
                   "(Отладка → Анализ запросов)",
        })

    result = {
        "recommendations_count": len(recommendations),
        "recommendations": recommendations,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


# ─── Запуск ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    app = mcp.sse_app()
    port = int(os.environ.get("MCP_PORT", 8009))
    uvicorn.run(app, host="0.0.0.0", port=port)
