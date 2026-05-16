"""
MCP-сервер: Тестирование кода 1С
==================================
Генерация сценариев тестирования, создание заготовок xUnit/Vanessa-тестов,
валидация тестовых данных, а также (опционально) запуск тестов в реальной
1С через интеграцию с yaxunit-stack.

Инструменты (генерация):
  - test_generate          — сгенерировать тесты для модуля/процедуры
  - test_scenario          — создать сценарий Vanessa (feature-файл)
  - test_data_suggest      — предложить тестовые данные на основе метаданных
  - test_coverage_analyze  — анализ покрытия: какие ветки кода не протестированы
  - test_template          — получить шаблон теста по типу (unit, integration, smoke)

Инструменты (выполнение через yaxunit-stack, опциональный профиль "testing"):
  - test_runner_health     — проверка готовности раннера + shared volume
  - test_run_path          — запустить тесты по путям в workspace (РЕКОМЕНДУЕТСЯ)
  - test_run               — запустить тесты через base64 zip (legacy)
  - test_run_status        — детали прогона по run_id (с JUnit XML)
  - test_run_list          — список последних прогонов
"""

import os
import json
import re
import base64
import shutil
import threading
import time
import urllib.request
import uuid
import logging
from pathlib import Path

import httpx

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("1C Testing")
logger = logging.getLogger(__name__)

NEO4J_URL = os.environ.get("NEO4J_URL", "http://neo4j:7474")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASS", "password1c")

# ─── YAxUnit Runner (опциональный, требует поднятого yaxunit-stack) ─────
YAXUNIT_URL = os.environ.get("YAXUNIT_URL", "http://onec-server:8019")
YAXUNIT_TIMEOUT = int(os.environ.get("YAXUNIT_TIMEOUT", "900"))

# Shared volume с yaxunit-stack для path-based payload (новый поток).
# Здесь mcp-testing складывает копии XML-выгрузок, раннер читает их по
# тому же пути. Если volume не смонтирован — test_run_path вернёт
# понятный error на этапе валидации (не дожидаясь HTTP в раннер).
PAYLOADS_DIR = Path(os.environ.get("PAYLOADS_DIR", "/payloads"))

# Workspace OpenCode read-only — отсюда mcp-testing берёт XML-выгрузки.
# Должен совпадать с volume `./workspace:/workspace:ro` в docker-compose.yml
# основного стека. Если каталог не смонтирован — test_run_path откажет
# с понятной диагностикой, а не упадёт где-то внутри pipeline.
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))

# ─── Async-режим test_run_path ────────────────────────────────────────
# Прогон тестов идёт от 30с до 5+ минут. MCP-клиент OpenCode имеет жёсткий
# таймаут tool-call (обычно 60с) и не настраивается per-server. Решение:
# fire-and-forget через background-тред + опрос test_run_status.
#
# _PENDING хранит состояние async-прогонов: ключ — pre_run_id (генерируется
# самим mcp-testing ДО запроса в раннер, чтобы сразу вернуть его агенту).
# После завершения — pre_run_id остаётся как алиас на real_run_id раннера.
#
# Чистка: dict растёт, но крайне медленно (один прогон в N минут × часов
# работы агента). Hard cap _PENDING_MAX держит размер ограниченным.
_PENDING: dict[str, dict] = {}
_PENDING_LOCK = threading.Lock()
_PENDING_MAX = 200


# ─── Neo4j клиент (переиспользуем паттерн) ──────────────────────────────

def _neo4j_query(cypher, parameters=None):
    auth = base64.b64encode(f"{NEO4J_USER}:{NEO4J_PASS}".encode()).decode()
    payload = json.dumps({
        "statements": [{"statement": cypher, "parameters": parameters or {}}]
    }).encode()
    req = urllib.request.Request(
        f"{NEO4J_URL}/db/neo4j/tx/commit",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        errors = result.get("errors", [])
        if errors:
            return None
        return result
    except Exception:
        return None


def _neo4j_rows(cypher, params=None):
    result = _neo4j_query(cypher, params)
    if not result:
        return []
    columns = result["results"][0].get("columns", [])
    rows = []
    for data in result["results"][0].get("data", []):
        row = {}
        for i, col in enumerate(columns):
            row[col] = data["row"][i]
        rows.append(row)
    return rows


# ─── Анализ BSL-кода ────────────────────────────────────────────────────

def _parse_procedures(code: str) -> list[dict]:
    """Парсит процедуры и функции из BSL-кода."""
    procs = []
    pattern = re.compile(
        r'(Процедура|Функция|Procedure|Function)\s+'
        r'([А-Яа-яёЁA-Za-z0-9_]+)\s*\(([^)]*)\)',
        re.UNICODE | re.IGNORECASE
    )
    for match in pattern.finditer(code):
        kind = match.group(1)
        name = match.group(2)
        params_str = match.group(3).strip()
        params = []
        if params_str:
            for p in params_str.split(","):
                p = p.strip()
                # Убираем Знач
                p = re.sub(r'^\s*Знач\s+', '', p, flags=re.IGNORECASE)
                # Разделяем имя и значение по умолчанию
                parts = p.split("=", 1)
                param = {"name": parts[0].strip()}
                if len(parts) > 1:
                    param["default"] = parts[1].strip()
                    param["optional"] = True
                else:
                    param["optional"] = False
                params.append(param)

        # Определяем тип: экспортная или нет
        # Ищем от начала процедуры до КонецПроцедуры
        start = match.start()
        export = bool(re.search(
            rf'{re.escape(name)}\s*\([^)]*\)\s*Экспорт',
            code[start:start+500],
            re.UNICODE | re.IGNORECASE
        ))

        procs.append({
            "kind": "Функция" if "ункци" in kind.lower() or "unction" in kind.lower() else "Процедура",
            "name": name,
            "params": params,
            "export": export,
        })

    return procs


def _find_branches(code: str, proc_name: str) -> list[dict]:
    """Находит ветвления в процедуре (Если/ИначеЕсли/Попытка)."""
    # Извлекаем тело процедуры
    pattern = re.compile(
        rf'(Процедура|Функция)\s+{re.escape(proc_name)}\s*\([^)]*\)[^\n]*\n(.*?)'
        rf'Конец(Процедуры|Функции)',
        re.UNICODE | re.IGNORECASE | re.DOTALL
    )
    match = pattern.search(code)
    if not match:
        return []

    body = match.group(2)
    branches = []

    # Если/ИначеЕсли
    if_pattern = re.compile(r'Если\s+(.+?)\s+Тогда', re.UNICODE | re.IGNORECASE)
    for m in if_pattern.finditer(body):
        branches.append({"type": "if", "condition": m.group(1).strip()})

    elseif_pattern = re.compile(r'ИначеЕсли\s+(.+?)\s+Тогда', re.UNICODE | re.IGNORECASE)
    for m in elseif_pattern.finditer(body):
        branches.append({"type": "elseif", "condition": m.group(1).strip()})

    if re.search(r'\bИначе\b', body, re.UNICODE | re.IGNORECASE):
        branches.append({"type": "else", "condition": "else-ветка"})

    # Попытка/Исключение
    if re.search(r'\bПопытка\b', body, re.UNICODE | re.IGNORECASE):
        branches.append({"type": "try", "condition": "Попытка-Исключение"})

    # Циклы
    for_pattern = re.compile(r'Для\s+Каждого\s+(\S+)\s+Из\s+(\S+)', re.UNICODE | re.IGNORECASE)
    for m in for_pattern.finditer(body):
        branches.append({"type": "loop", "condition": f"Для Каждого {m.group(1)} Из {m.group(2)}"})

    while_pattern = re.compile(r'Пока\s+(.+?)\s+Цикл', re.UNICODE | re.IGNORECASE)
    for m in while_pattern.finditer(body):
        branches.append({"type": "loop", "condition": f"Пока {m.group(1)}"})

    # ВызватьИсключение
    if re.search(r'ВызватьИсключение', body, re.UNICODE | re.IGNORECASE):
        branches.append({"type": "throw", "condition": "ВызватьИсключение"})

    return branches


# ─── Шаблоны тестов ──────────────────────────────────────────────────────

XUNIT_TEMPLATE = """// Тест: {test_name}
// Модуль: {module_name}
// Автогенерация — проверьте и дополните

#Область СлужебныйПрограммныйИнтерфейс

Процедура ИсполняемыеСценарии() Экспорт
    
{scenarios}
    
КонецПроцедуры

{test_procedures}

#КонецОбласти
"""

XUNIT_SCENARIO = '    ЮТТесты.ДобавитьТест("{name}"{params});'
XUNIT_SCENARIO_WITH_CONTEXT = '    ЮТТесты.ДобавитьТест("{name}").СПараметрами({params});'

XUNIT_TEST_PROC = """Процедура {name}() Экспорт
    
    // Подготовка
{arrange}
    
    // Действие
{act}
    
    // Проверка
{assert_lines}
    
КонецПроцедуры
"""

VANESSA_TEMPLATE = """# language: ru

@tree
Функциональность: {feature_name}

{scenarios}
"""

VANESSA_SCENARIO = """  Сценарий: {name}
    Допустим я открываю форму "{form_name}"
{steps}
"""


# ─── MCP инструменты ─────────────────────────────────────────────────────

@mcp.tool()
def test_generate(
    code: str,
    module_name: str = "",
    test_framework: str = "xunit",
    focus: str = "",
) -> str:
    """
    Сгенерировать заготовки тестов для BSL-кода.

    Параметры:
      code           — исходный код модуля 1С (BSL)
      module_name    — имя модуля (для комментариев)
      test_framework — "xunit" (YAxUnit/ЮТ) или "vanessa" (Vanessa Automation)
      focus          — (опционально) имя конкретной процедуры для тестирования
    """
    procs = _parse_procedures(code)
    if not procs:
        return json.dumps({
            "error": "Не найдено процедур или функций в коде",
            "hint": "Убедитесь что передан код BSL с процедурами/функциями",
        }, ensure_ascii=False)

    if focus:
        procs = [p for p in procs if p["name"].lower() == focus.lower()]
        if not procs:
            return json.dumps({
                "error": f"Процедура '{focus}' не найдена в коде",
            }, ensure_ascii=False)

    # Генерируем тесты только для экспортных или всех
    test_procs = [p for p in procs if p.get("export")] or procs

    if test_framework.lower() == "vanessa":
        return _generate_vanessa(test_procs, module_name, code)
    else:
        return _generate_xunit(test_procs, module_name, code)


def _generate_xunit(procs: list, module_name: str, full_code: str) -> str:
    """Генерирует xUnit тесты."""
    scenarios = []
    test_procedures = []

    for proc in procs:
        branches = _find_branches(full_code, proc["name"])
        test_name = f"Тест_{proc['name']}"

        # Основной тест — happy path
        scenarios.append(XUNIT_SCENARIO.format(name=test_name, params=""))

        # Подготовка параметров
        arrange_lines = []
        act_params = []
        for param in proc.get("params", []):
            pname = param["name"]
            if param.get("default"):
                arrange_lines.append(f'    {pname} = {param["default"]};')
            elif any(hint in pname.lower() for hint in ["ссылка", "объект", "ref"]):
                arrange_lines.append(f'    // TODO: Создать тестовый объект')
                arrange_lines.append(f'    {pname} = Неопределено; // <- подставить тестовые данные')
            elif any(hint in pname.lower() for hint in ["дата", "date", "период"]):
                arrange_lines.append(f'    {pname} = ТекущаяДата();')
            elif any(hint in pname.lower() for hint in ["число", "количество", "сумма", "number"]):
                arrange_lines.append(f'    {pname} = 100;')
            elif any(hint in pname.lower() for hint in ["строка", "имя", "наименование", "string", "name"]):
                arrange_lines.append(f'    {pname} = "ТестовоеЗначение";')
            elif any(hint in pname.lower() for hint in ["флаг", "признак", "bool"]):
                arrange_lines.append(f'    {pname} = Истина;')
            else:
                arrange_lines.append(f'    {pname} = Неопределено; // TODO: задать значение')
            act_params.append(pname)

        # Действие
        if proc["kind"] == "Функция":
            act_line = f'    Результат = {proc["name"]}({", ".join(act_params)});'
            assert_lines = [
                '    ЮТест.ОжидаетЧто(Результат)',
                '        .НеРавно(Неопределено);',
                '    // TODO: добавить конкретные проверки результата',
            ]
        else:
            act_line = f'    {proc["name"]}({", ".join(act_params)});'
            assert_lines = [
                '    // TODO: проверить побочные эффекты процедуры',
                '    // Например: состояние объекта, записи в регистрах',
            ]

        test_procedures.append(XUNIT_TEST_PROC.format(
            name=test_name,
            arrange="\n".join(arrange_lines) if arrange_lines else "    // Нет параметров",
            act=act_line,
            assert_lines="\n".join(assert_lines),
        ))

        # Тесты для ветвлений
        for i, branch in enumerate(branches):
            if branch["type"] == "if":
                branch_test_name = f"Тест_{proc['name']}_Условие{i+1}"
                scenarios.append(XUNIT_SCENARIO.format(name=branch_test_name, params=""))
                test_procedures.append(XUNIT_TEST_PROC.format(
                    name=branch_test_name,
                    arrange=f'    // Подготовить данные для условия: {branch["condition"]}',
                    act=act_line,
                    assert_lines='    // TODO: проверить поведение при выполнении условия',
                ))

            elif branch["type"] == "throw":
                exc_test_name = f"Тест_{proc['name']}_Исключение"
                scenarios.append(XUNIT_SCENARIO.format(name=exc_test_name, params=""))
                test_procedures.append(XUNIT_TEST_PROC.format(
                    name=exc_test_name,
                    arrange='    // Подготовить невалидные данные для вызова исключения',
                    act=f'    // Ожидаем исключение:\n    // {act_line}',
                    assert_lines=(
                        '    Попытка\n'
                        f'        {proc["name"]}({", ".join(act_params)});\n'
                        '        ЮТест.ОжидаетЧто(Ложь).Равно(Истина); // Не должны дойти сюда\n'
                        '    Исключение\n'
                        '        // OK — исключение ожидаемо\n'
                        '    КонецПопытки;'
                    ),
                ))

            elif branch["type"] == "try":
                try_test_name = f"Тест_{proc['name']}_ОбработкаОшибки"
                scenarios.append(XUNIT_SCENARIO.format(name=try_test_name, params=""))
                test_procedures.append(XUNIT_TEST_PROC.format(
                    name=try_test_name,
                    arrange='    // Подготовить данные, вызывающие ошибку в Попытке',
                    act=act_line,
                    assert_lines='    // TODO: проверить корректную обработку ошибки',
                ))

    full_test = XUNIT_TEMPLATE.format(
        test_name=module_name or "Автотест",
        module_name=module_name or "Неизвестный модуль",
        scenarios="\n".join(scenarios),
        test_procedures="\n".join(test_procedures),
    )

    return json.dumps({
        "framework": "xunit",
        "test_code": full_test,
        "stats": {
            "procedures_analyzed": len(procs),
            "tests_generated": len(scenarios),
            "branches_covered": sum(len(_find_branches(full_code, p["name"])) for p in procs),
        },
        "next_steps": [
            "Замените TODO-заглушки на реальные тестовые данные",
            "Добавьте тесты граничных значений",
            "Добавьте тесты на пустые/нулевые параметры",
            "Запустите тесты через YAxUnit Runner",
        ],
    }, ensure_ascii=False, indent=2)


def _generate_vanessa(procs: list, module_name: str, full_code: str) -> str:
    """Генерирует Vanessa Automation сценарии."""
    scenarios_text = []

    for proc in procs:
        steps = []
        for param in proc.get("params", []):
            pname = param["name"]
            if any(hint in pname.lower() for hint in ["ссылка", "объект"]):
                steps.append(f'    И в поле "{pname}" я выбираю "ТестовоеЗначение"')
            elif any(hint in pname.lower() for hint in ["дата"]):
                steps.append(f'    И в поле "{pname}" я ввожу дату "01.01.2025"')
            elif any(hint in pname.lower() for hint in ["число", "сумма", "количество"]):
                steps.append(f'    И в поле "{pname}" я ввожу "100"')
            elif any(hint in pname.lower() for hint in ["строка", "наименование"]):
                steps.append(f'    И в поле "{pname}" я ввожу "Тестовое значение"')
            elif any(hint in pname.lower() for hint in ["флаг", "признак"]):
                steps.append(f'    И я устанавливаю флаг "{pname}"')

        if proc["kind"] == "Функция":
            steps.append(f'    Тогда результат выполнения не пустой')
        else:
            steps.append(f'    Тогда не появилось окно с ошибкой')

        scenarios_text.append(VANESSA_SCENARIO.format(
            name=f"Проверка {proc['name']}",
            form_name=module_name or "ФормаТеста",
            steps="\n".join(steps) if steps else '    Тогда я вижу форму',
        ))

    full_feature = VANESSA_TEMPLATE.format(
        feature_name=module_name or "Автоматический тест",
        scenarios="\n".join(scenarios_text),
    )

    return json.dumps({
        "framework": "vanessa",
        "feature_code": full_feature,
        "stats": {
            "procedures_analyzed": len(procs),
            "scenarios_generated": len(scenarios_text),
        },
        "next_steps": [
            "Уточните имена форм и элементов",
            "Добавьте шаги подготовки тестовых данных",
            "Добавьте сценарии негативного тестирования",
            "Запустите через Vanessa Runner",
        ],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def test_scenario(
    description: str,
    object_name: str = "",
    scenario_type: str = "smoke",
) -> str:
    """
    Создать сценарий тестирования для объекта 1С.

    Параметры:
      description   — что нужно протестировать ("создание документа", "проведение заказа")
      object_name   — имя объекта метаданных (опционально, для автоподстановки полей)
      scenario_type — тип: "smoke" (базовый), "regression" (полный), "boundary" (граничные)
    """
    obj_info = None
    if object_name:
        rows = _neo4j_rows("""
            MATCH (o:MetadataObject)
            WHERE o.full_name = $name OR o.name = $name
            OPTIONAL MATCH (o)-[:HAS_ATTRIBUTE]->(a:Attribute)
            RETURN o.full_name as full_name, o.name as name, o.kind as kind,
                   collect({name: a.name, type: a.type}) as attributes
            LIMIT 1
        """, {"name": object_name})
        if rows:
            obj_info = rows[0]

    scenarios = []

    if scenario_type == "smoke":
        scenarios.append({
            "name": f"Smoke: {description}",
            "steps": [
                "Создать новый объект с минимальным набором обязательных полей",
                "Записать объект",
                "Проверить что объект записался без ошибок",
                "Открыть объект повторно и проверить сохранение данных",
            ],
            "expected": "Объект создан, записан, данные сохранены корректно",
        })

    elif scenario_type == "regression":
        scenarios.append({
            "name": f"Positive: {description} — стандартный сценарий",
            "steps": [
                "Создать объект со всеми заполненными полями",
                "Записать и проверить все реквизиты",
            ],
            "expected": "Все поля сохранены корректно",
        })
        scenarios.append({
            "name": f"Negative: {description} — без обязательных полей",
            "steps": [
                "Создать объект с пустыми обязательными полями",
                "Попытаться записать",
            ],
            "expected": "Должна быть ошибка валидации",
        })
        scenarios.append({
            "name": f"Edge: {description} — граничные значения",
            "steps": [
                "Строковые поля: пустая строка, максимальная длина, спецсимволы",
                "Числовые поля: 0, отрицательные, максимально большие",
                "Даты: минимальная, максимальная, пустая",
            ],
            "expected": "Корректная обработка всех граничных значений",
        })
        if obj_info and obj_info.get("kind") in ("Документ", "Document"):
            scenarios.append({
                "name": f"Posting: {description} — проведение",
                "steps": [
                    "Создать и заполнить документ",
                    "Провести документ",
                    "Проверить движения по регистрам",
                    "Отменить проведение",
                    "Проверить что движения удалены",
                ],
                "expected": "Движения формируются и удаляются корректно",
            })

    elif scenario_type == "boundary":
        scenarios.append({
            "name": f"Boundary: пустые значения",
            "steps": ["Передать пустые/Неопределено значения во все параметры"],
            "expected": "Корректная обработка без падения",
        })
        scenarios.append({
            "name": f"Boundary: максимальные значения",
            "steps": ["Передать максимально допустимые значения"],
            "expected": "Нет переполнения или обрезки данных",
        })
        scenarios.append({
            "name": f"Boundary: concurrent access",
            "steps": [
                "Открыть объект двумя пользователями",
                "Изменить одновременно",
                "Проверить блокировку/конфликт",
            ],
            "expected": "Конфликт обрабатывается предсказуемо",
        })

    # Добавляем информацию по полям из метаданных
    fields_info = []
    if obj_info:
        for attr in obj_info.get("attributes", []):
            if attr.get("name"):
                test_values = _suggest_test_values(attr.get("name", ""), attr.get("type", ""))
                fields_info.append({
                    "field": attr["name"],
                    "type": attr.get("type", ""),
                    "test_values": test_values,
                })

    result = {
        "description": description,
        "scenario_type": scenario_type,
        "object": obj_info.get("full_name", object_name) if obj_info else object_name,
        "scenarios": scenarios,
        "fields_to_test": fields_info[:20],
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def _suggest_test_values(field_name: str, field_type: str) -> list:
    """Предлагает тестовые значения по имени и типу поля."""
    name_lower = field_name.lower()
    type_lower = field_type.lower()

    if "строка" in type_lower or "string" in type_lower:
        return ['""', '"A"', '"Тестовая строка максимальной длины..."', '"<script>"']
    elif "число" in type_lower or "number" in type_lower:
        return ["0", "1", "-1", "999999999", "0.01"]
    elif "дата" in type_lower or "date" in type_lower:
        return ["'00010101'", "ТекущаяДата()", "'20991231'"]
    elif "булево" in type_lower or "boolean" in type_lower:
        return ["Истина", "Ложь"]
    elif "ссылка" in type_lower or "ref" in type_lower:
        return ["Неопределено", "<существующая ссылка>", "<пустая ссылка>"]
    elif any(w in name_lower for w in ["сумма", "цена", "стоимость"]):
        return ["0", "1", "100.50", "-1", "99999999.99"]
    elif any(w in name_lower for w in ["количество", "кол"]):
        return ["0", "1", "0.001", "999999"]
    elif any(w in name_lower for w in ["дата"]):
        return ["'00010101'", "ТекущаяДата()", "'20991231'"]
    else:
        return ["Неопределено", "<тестовое значение>"]


@mcp.tool()
def test_data_suggest(object_name: str) -> str:
    """
    Предложить тестовые данные для объекта метаданных.
    Анализирует реквизиты, типы и связи для генерации набора тестовых значений.

    Параметры:
      object_name — полное или короткое имя объекта метаданных
    """
    rows = _neo4j_rows("""
        MATCH (o:MetadataObject)
        WHERE o.full_name = $name OR o.name = $name
        OPTIONAL MATCH (o)-[:HAS_ATTRIBUTE]->(a:Attribute)
        OPTIONAL MATCH (o)-[:HAS_TABULAR_SECTION]->(ts:TabularSection)-[:HAS_ATTRIBUTE]->(tsa:Attribute)
        RETURN o.full_name as full_name, o.name as name, o.kind as kind,
               collect(distinct {name: a.name, type: a.type, required: a.required}) as attributes,
               collect(distinct {ts: ts.name, attr: tsa.name, type: tsa.type}) as ts_attrs
        LIMIT 1
    """, {"name": object_name})

    if not rows:
        return json.dumps({"error": f"Объект '{object_name}' не найден"}, ensure_ascii=False)

    obj = rows[0]

    # Генерируем набор тестовых данных
    test_sets = {
        "happy_path": {},
        "minimal": {},
        "boundary": {},
        "negative": {},
    }

    for attr in obj.get("attributes", []):
        if not attr.get("name"):
            continue
        name = attr["name"]
        values = _suggest_test_values(name, attr.get("type", ""))

        test_sets["happy_path"][name] = values[1] if len(values) > 1 else values[0]
        test_sets["minimal"][name] = values[0]
        test_sets["boundary"][name] = values[-1] if len(values) > 1 else values[0]
        test_sets["negative"][name] = "Неопределено"

    # Табличные части
    ts_data = {}
    for ta in obj.get("ts_attrs", []):
        if ta.get("ts"):
            if ta["ts"] not in ts_data:
                ts_data[ta["ts"]] = {}
            if ta.get("attr"):
                ts_data[ta["ts"]][ta["attr"]] = _suggest_test_values(
                    ta["attr"], ta.get("type", "")
                )

    result = {
        "object": obj["full_name"],
        "kind": obj.get("kind", ""),
        "test_data_sets": test_sets,
        "tabular_sections_data": ts_data,
        "recommendations": [
            "happy_path — стандартные корректные данные",
            "minimal — минимально необходимые (обязательные) поля",
            "boundary — граничные значения для проверки ограничений",
            "negative — невалидные данные для проверки обработки ошибок",
        ],
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def test_coverage_analyze(code: str) -> str:
    """
    Анализ покрытия: находит ветки кода, которые нужно протестировать.

    Параметры:
      code — исходный код модуля BSL
    """
    procs = _parse_procedures(code)
    analysis = []

    for proc in procs:
        branches = _find_branches(code, proc["name"])

        # Считаем сложность
        complexity = 1  # базовая
        for b in branches:
            if b["type"] in ("if", "elseif"):
                complexity += 1
            elif b["type"] == "loop":
                complexity += 1
            elif b["type"] == "try":
                complexity += 1

        min_tests = complexity
        recommended_tests = complexity + len([b for b in branches if b["type"] == "throw"])

        analysis.append({
            "procedure": proc["name"],
            "kind": proc["kind"],
            "export": proc.get("export", False),
            "params_count": len(proc.get("params", [])),
            "cyclomatic_complexity": complexity,
            "branches": [
                {"type": b["type"], "condition": b["condition"]}
                for b in branches
            ],
            "minimum_tests_needed": min_tests,
            "recommended_tests": recommended_tests,
            "test_cases_suggested": _suggest_test_cases(proc, branches),
        })

    total_complexity = sum(a["cyclomatic_complexity"] for a in analysis)
    total_min_tests = sum(a["minimum_tests_needed"] for a in analysis)

    result = {
        "module_stats": {
            "procedures": len(analysis),
            "total_complexity": total_complexity,
            "total_minimum_tests": total_min_tests,
            "complexity_rating": (
                "LOW" if total_complexity < 10 else
                "MEDIUM" if total_complexity < 25 else
                "HIGH"
            ),
        },
        "procedures": analysis,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def _suggest_test_cases(proc: dict, branches: list) -> list:
    """Предлагает конкретные тест-кейсы."""
    cases = [f"Happy path: вызов {proc['name']} с корректными параметрами"]

    for i, b in enumerate(branches):
        if b["type"] == "if":
            cases.append(f"Условие ИСТИНА: {b['condition']}")
            cases.append(f"Условие ЛОЖЬ: НЕ ({b['condition']})")
        elif b["type"] == "throw":
            cases.append(f"Исключение: проверить что {b['condition']} срабатывает корректно")
        elif b["type"] == "try":
            cases.append("Попытка: успешное выполнение")
            cases.append("Попытка: обработка ошибки в Исключении")
        elif b["type"] == "loop":
            cases.append(f"Цикл ({b['condition']}): пустая коллекция")
            cases.append(f"Цикл ({b['condition']}): один элемент")
            cases.append(f"Цикл ({b['condition']}): много элементов")

    if proc.get("params"):
        optional = [p for p in proc["params"] if p.get("optional")]
        if optional:
            cases.append("Опциональные параметры: вызов без них")

    return cases


@mcp.tool()
def test_template(
    template_type: str = "unit",
    object_kind: str = "",
) -> str:
    """
    Получить шаблон теста по типу.

    Параметры:
      template_type — "unit", "integration", "smoke", "performance"
      object_kind   — (опционально) тип объекта: "Справочник", "Документ", "Обработка", "Регистр"
    """
    templates = {
        "unit": {
            "description": "Модульный тест — проверка отдельной процедуры/функции",
            "framework": "YAxUnit (ЮТ)",
            "template": """// Модульный тест
// Объект: {object_kind}

#Область СлужебныйПрограммныйИнтерфейс

Процедура ИсполняемыеСценарии() Экспорт
    
    ЮТТесты.ДобавитьТест("ТестОсновнойСценарий");
    ЮТТесты.ДобавитьТест("ТестГраничныеЗначения");
    ЮТТесты.ДобавитьТест("ТестОшибочныеДанные");
    
КонецПроцедуры

Процедура ТестОсновнойСценарий() Экспорт
    // Arrange — подготовка
    // TODO: создать тестовые данные
    
    // Act — действие
    // TODO: вызвать тестируемую процедуру
    
    // Assert — проверка
    // ЮТест.ОжидаетЧто(Результат).Равно(ОжидаемоеЗначение);
КонецПроцедуры

Процедура ТестГраничныеЗначения() Экспорт
    // Пустые строки, нулевые числа, пустые даты
КонецПроцедуры

Процедура ТестОшибочныеДанные() Экспорт
    // Невалидные параметры — ожидаем исключение
КонецПроцедуры

#КонецОбласти""",
        },
        "integration": {
            "description": "Интеграционный тест — проверка взаимодействия объектов",
            "framework": "YAxUnit (ЮТ)",
            "template": """// Интеграционный тест
// Проверка цепочки: создание → запись → проведение → проверка движений

#Область СлужебныйПрограммныйИнтерфейс

Процедура ИсполняемыеСценарии() Экспорт
    ЮТТесты.ДобавитьТест("ТестПолныйЦикл");
    ЮТТесты.ДобавитьТест("ТестОтменаПроведения");
КонецПроцедуры

Процедура ТестПолныйЦикл() Экспорт
    // 1. Создаём зависимые объекты (справочники)
    // TODO: СоздатьТестовыйСправочник()
    
    // 2. Создаём основной объект
    // TODO: Объект = Документы.ИмяДокумента.СоздатьДокумент();
    
    // 3. Заполняем и записываем
    // TODO: Заполнить реквизиты и ТЧ
    // Объект.Записать(РежимЗаписиДокумента.Проведение);
    
    // 4. Проверяем движения
    // TODO: Запрос к регистрам
    // ЮТест.ОжидаетЧто(КоличествоДвижений).Больше(0);
    
    // 5. Очистка тестовых данных
    // TODO: Удалить созданные объекты
КонецПроцедуры

Процедура ТестОтменаПроведения() Экспорт
    // Провести → Отменить проведение → Проверить удаление движений
КонецПроцедуры

#КонецОбласти""",
        },
        "smoke": {
            "description": "Дымовой тест — базовая проверка работоспособности",
            "framework": "Vanessa Automation",
            "template": """# language: ru

@smoke
Функциональность: Дымовые тесты {object_kind}

  Сценарий: Открытие формы списка
    Допустим я открываю навигационную ссылку "e1cib/list/TODO_ИмяОбъекта"
    Тогда открылась форма "TODO_ИмяОбъекта"

  Сценарий: Создание нового элемента
    Допустим я открываю навигационную ссылку "e1cib/list/TODO_ИмяОбъекта"
    И я нажимаю кнопку "Создать"
    Тогда открылась форма "TODO_ИмяОбъекта (создание)"
    И в поле "Наименование" я ввожу "Тест_Smoke"
    И я нажимаю кнопку "Записать и закрыть"
    Тогда не появилось окно с ошибкой""",
        },
        "performance": {
            "description": "Тест производительности — замер времени операций",
            "framework": "YAxUnit (ЮТ)",
            "template": """// Тест производительности

#Область СлужебныйПрограммныйИнтерфейс

Процедура ИсполняемыеСценарии() Экспорт
    ЮТТесты.ДобавитьТест("ТестПроизводительностьЗаписи");
    ЮТТесты.ДобавитьТест("ТестПроизводительностьЗапроса");
КонецПроцедуры

Процедура ТестПроизводительностьЗаписи() Экспорт
    МаксимальноеВремя = 2000; // ms
    
    НачалоЗамера = ТекущаяУниверсальнаяДатаВМиллисекундах();
    
    // TODO: выполнить тестируемую операцию
    Для Сч = 1 По 100 Цикл
        // Создать и записать объект
    КонецЦикла;
    
    ВремяВыполнения = ТекущаяУниверсальнаяДатаВМиллисекундах() - НачалоЗамера;
    
    ЮТест.ОжидаетЧто(ВремяВыполнения)
        .Меньше(МаксимальноеВремя,
            "Запись 100 объектов заняла " + Строка(ВремяВыполнения) + " мс");
КонецПроцедуры

Процедура ТестПроизводительностьЗапроса() Экспорт
    МаксимальноеВремя = 500; // ms
    
    НачалоЗамера = ТекущаяУниверсальнаяДатаВМиллисекундах();
    
    // TODO: выполнить запрос
    
    ВремяВыполнения = ТекущаяУниверсальнаяДатаВМиллисекундах() - НачалоЗамера;
    
    ЮТест.ОжидаетЧто(ВремяВыполнения)
        .Меньше(МаксимальноеВремя);
КонецПроцедуры

#КонецОбласти""",
        },
    }

    t = templates.get(template_type, templates["unit"])
    t["template"] = t["template"].replace("{object_kind}", object_kind or "ОбъектМетаданных")

    return json.dumps(t, ensure_ascii=False, indent=2)


# ─── YAxUnit Runner: запуск тестов в реальной 1С ────────────────────────
# Эти тулы проксируют HTTP API yaxunit-stack (порт 8019). Стек
# опциональный (Compose-профиль "testing"); если он не поднят —
# тулы вернут понятную ошибку "runner unreachable".


def _runner_error(message: str, **extra) -> str:
    payload = {"status": "error", "error": message, **extra}
    return json.dumps(payload, ensure_ascii=False)


def _resolve_workspace_path(raw: str) -> Path:
    """
    Привести путь к виду внутри WORKSPACE_DIR.

    Принимает либо относительный путь ("MyExtension/src/cf"), либо
    абсолютный, начинающийся с WORKSPACE_DIR ("/workspace/...").
    Любой выход за пределы WORKSPACE_DIR через .. или симлинк блокируется.
    """
    p = Path(raw)
    if not p.is_absolute():
        p = WORKSPACE_DIR / p
    resolved = p.resolve()
    try:
        resolved.relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        raise ValueError(
            f"path must be under {WORKSPACE_DIR}/, got '{raw}' "
            f"(resolved to '{resolved}')"
        )
    if not resolved.is_dir():
        raise ValueError(f"not a directory: {raw}")
    return resolved


@mcp.tool()
def test_runner_health() -> str:
    """
    Проверить готовность YAxUnit-раннера (опциональный сервис).

    Возвращает JSON со статусом и составом проверок:
      - на стороне раннера: pipeline_script, yaxunit_cfe, platform,
        license_*, payloads_volume_mounted (см. yaxunit-stack /health);
      - на стороне mcp-testing: payloads_volume_mounted_caller_side,
        workspace_dir_mounted (без них test_run_path работать не будет).

    При недоступности раннера (стек не поднят / нет лицензии) — вернёт
    {"status": "error", "error": "runner unreachable: ..."}.
    """
    caller_checks = {
        "payloads_volume_mounted_caller_side": PAYLOADS_DIR.is_dir(),
        "workspace_dir_mounted": WORKSPACE_DIR.is_dir(),
    }
    try:
        r = httpx.get(f"{YAXUNIT_URL}/health", timeout=10)
    except Exception as e:
        return _runner_error(
            f"runner unreachable: {e}",
            url=YAXUNIT_URL,
            caller_checks=caller_checks,
        )

    # Подмешиваем caller-side checks в ответ раннера, чтобы агент видел
    # обе стороны интеграции в одном JSON.
    try:
        runner_payload = json.loads(r.text)
    except Exception:
        # Раннер отдал не-JSON — отдаём как есть, но прицепим caller-чеки
        return json.dumps(
            {"raw_runner_response": r.text, "caller_checks": caller_checks},
            ensure_ascii=False,
        )
    runner_payload.setdefault("checks", {}).update(caller_checks)
    return json.dumps(runner_payload, ensure_ascii=False)


def _call_runner_sync(payload_dir: Path, mode: str) -> dict:
    """
    Блокирующий вызов раннера POST /run_tests_path и подготовка ответа.

    Используется в обоих режимах:
      - sync (wait=True): из основного треда test_run_path
      - async (wait=False): из background-треда _run_in_background

    Возвращает dict с финальным ответом для агента (готов к json.dumps).
    Не бросает исключений — все ошибки укладывает в dict со status=error.
    Сам управляет payload (чистит при passed, оставляет при error/failed).
    """
    request_body = {"payload_path": str(payload_dir), "mode": mode}
    try:
        with httpx.Client(timeout=YAXUNIT_TIMEOUT) as client:
            r = client.post(f"{YAXUNIT_URL}/run_tests_path", json=request_body)
    except httpx.TimeoutException:
        return {
            "status": "error",
            "error": f"timeout after {YAXUNIT_TIMEOUT}s",
            "payload_kept": str(payload_dir),
        }
    except Exception as e:
        shutil.rmtree(payload_dir, ignore_errors=True)
        return {"status": "error", "error": f"runner request failed: {e}"}

    if r.status_code >= 400:
        return {
            "status": "error",
            "error": f"runner returned HTTP {r.status_code}",
            "response": r.text[:2000],
            "payload_kept": str(payload_dir),
        }

    try:
        result = json.loads(r.text)
    except Exception:
        return {
            "status": "error",
            "error": "runner returned non-JSON",
            "raw": r.text[:2000],
            "payload_kept": str(payload_dir),
        }

    if result.get("status") == "passed":
        shutil.rmtree(payload_dir, ignore_errors=True)
    else:
        result["payload_kept"] = str(payload_dir)
    return result


def _run_in_background(pre_run_id: str, payload_dir: Path, mode: str) -> None:
    """
    Тело background-треда для async-прогона.

    Делает блокирующий вызов раннера и записывает результат в _PENDING.
    Любые исключения тоже укладывает в _PENDING как error — чтобы
    test_run_status всегда мог их показать.

    Тред демонический; при остановке процесса mcp-testing задача просто
    прерывается — payload остаётся на диске для ручной очистки/анализа.
    """
    try:
        result = _call_runner_sync(payload_dir, mode)
        with _PENDING_LOCK:
            entry = _PENDING.get(pre_run_id, {})
            entry.update({
                "status": result.get("status", "error"),
                "result": result,
                # Связываем pre_run_id с реальным run_id раннера для удобства
                # пользователя — он может опрашивать любым из двух id.
                "real_run_id": result.get("run_id"),
                "completed_at": time.time(),
            })
            _PENDING[pre_run_id] = entry
    except Exception as e:
        # В норме сюда не попадаем — _call_runner_sync ловит всё внутри.
        # Но на случай, если в нём что-то пройдёт мимо (OOM, etc.) —
        # фиксируем как error, чтобы тред не помер молча.
        logger.exception("async run %s crashed", pre_run_id)
        with _PENDING_LOCK:
            _PENDING[pre_run_id] = {
                **_PENDING.get(pre_run_id, {}),
                "status": "error",
                "result": {
                    "status": "error",
                    "error": f"background thread crashed: {e}",
                    "payload_kept": str(payload_dir),
                },
                "completed_at": time.time(),
            }


def _trim_pending() -> None:
    """Дёшево держим _PENDING ограниченным. Удаляем старейшие completed."""
    with _PENDING_LOCK:
        if len(_PENDING) <= _PENDING_MAX:
            return
        # Сортировка по completed_at; running записи защищены (None ставим как inf)
        items = sorted(
            _PENDING.items(),
            key=lambda kv: kv[1].get("completed_at") or float("inf"),
        )
        n_excess = len(_PENDING) - _PENDING_MAX
        for pre_id, _ in items[:n_excess]:
            _PENDING.pop(pre_id, None)


@mcp.tool()
def test_run_path(
    config_path: str,
    tests_path: str,
    mode: str = "server",
    yaxunit_json: str | None = None,
    wait: bool = False,
) -> str:
    """
    Запустить YAxUnit-тесты по путям в workspace (через shared volume).

    РЕКОМЕНДУЕТСЯ как основной способ запуска тестов из агента —
    в отличие от test_run, не передаёт байты конфигурации через LLM-контекст.

    Параметры:
        config_path: путь к каталогу XML-выгрузки основной конфигурации.
                     Относительный (от /workspace) или абсолютный под /workspace.
                     Каталог должен содержать Configuration.xml + поддиректории
                     (CommonModules, Catalogs, ...).
        tests_path:  путь к каталогу XML-выгрузки расширения с тестами.
                     Тоже относительный или абсолютный под /workspace.
                     Configuration.xml расширения должен содержать
                     <Properties><Name>...</Name> — это имя расширения
                     (может быть кириллическим), читается раннером сам.
        mode:        "server" (по умолчанию, требует серверной community-
                     лицензии 1С) или "file" (файловая ИБ, быстрее, любая
                     community-лицензия).
        yaxunit_json: опциональный JSON-текст настроек YAxUnit (записывается
                     в payload как yaxunit.json). Если не передан — раннер
                     создаст дефолтный.
        wait:        Управление длительностью tool-call.
                     False (по умолчанию) — async-режим, fire-and-forget.
                     Возвращает {run_id, status: "running"} мгновенно,
                     прогон уходит в фоновый тред mcp-testing. Опрашивать
                     через test_run_status(run_id) каждые 15-30с. Это
                     безопасный режим: tool-call MCP-клиента не упадёт
                     по таймауту даже на длинном server-mode прогоне.
                     True — синхронный режим, держит соединение открытым
                     до завершения. ВНИМАНИЕ: при прогоне дольше ~60с
                     MCP-клиент OpenCode выдаст ошибку MCP -32001
                     "Request timed out" даже если раннер продолжает
                     работать. Используйте True ТОЛЬКО для коротких
                     прогонов (file-mode + малая конфигурация + 1-3 теста).

    Поток (sync, wait=True):
        1. mcp-testing создаёт /payloads/<id>/{config,tests} и копирует
           туда XML-выгрузки из /workspace/.
        2. POST {"payload_path": "/payloads/<id>", "mode": ...} в раннер.
        3. Раннер запускает full_pipeline.sh.
        4. Возвращает JSON: run_id, status (passed/failed/error), tests,
           failures, errors, duration_sec, extension, mode, exit_code,
           junit_xml, log.

    Поток (async, wait=False):
        1-2. То же, что в sync.
        3. Тред в mcp-testing делает POST в раннер, основной поток
           возвращает {run_id, status: "running"} немедленно.
        4. Агент опрашивает test_run_status(run_id) каждые 15-30с.
           Пока тред жив — возвращается {status: "running"}; после
           завершения — полный результат как в sync-режиме.

    Payload удаляется после passed-прогона; при failed/error — остаётся
    в /payloads/<id>/ для отладки (поле payload_kept в ответе).

    Если volume или workspace не смонтированы — сразу вернёт ошибку, не
    дожидаясь HTTP в раннер. Сначала вызовите test_runner_health для
    диагностики.
    """
    if mode not in ("server", "file"):
        return _runner_error(f"invalid mode '{mode}', expected 'server' or 'file'")

    if not PAYLOADS_DIR.is_dir():
        return _runner_error(
            f"shared volume not mounted at {PAYLOADS_DIR} — check "
            f"docker-compose.yml volumes for mcp-testing",
            payloads_dir=str(PAYLOADS_DIR),
        )
    if not WORKSPACE_DIR.is_dir():
        return _runner_error(
            f"workspace not mounted at {WORKSPACE_DIR} — check "
            f"docker-compose.yml volumes for mcp-testing",
            workspace_dir=str(WORKSPACE_DIR),
        )

    # Валидация путей: только под /workspace, без выхода через .. / симлинки
    try:
        config_src = _resolve_workspace_path(config_path)
        tests_src = _resolve_workspace_path(tests_path)
    except ValueError as e:
        return _runner_error(str(e))

    # Базовая sanity-проверка структуры выгрузок до того, как тащить в payload
    if not (config_src / "Configuration.xml").is_file():
        return _runner_error(
            f"config_path missing Configuration.xml: {config_path}"
        )
    if not (tests_src / "Configuration.xml").is_file():
        return _runner_error(
            f"tests_path missing Configuration.xml: {tests_path}"
        )

    # Сборка payload в shared volume
    payload_id = uuid.uuid4().hex[:12]
    payload_dir = PAYLOADS_DIR / payload_id
    try:
        # copytree, потому что workspace смонтирован read-only — раннеру
        # нужен независимый rw-каталог (full_pipeline.sh пишет логи рядом
        # на REPORT_DIR, но сам каталог config/tests он считает доступным
        # как минимум на чтение из своего mount-неймспейса).
        # symlink тут не годится: workspace в раннере не смонтирован.
        shutil.copytree(config_src, payload_dir / "config")
        shutil.copytree(tests_src, payload_dir / "tests")
        if yaxunit_json:
            (payload_dir / "yaxunit.json").write_text(
                yaxunit_json, encoding="utf-8"
            )
    except Exception as e:
        shutil.rmtree(payload_dir, ignore_errors=True)
        return _runner_error(f"failed to stage payload: {e}")

    # ── Async-ветка: уходим в фоновый тред, возвращаем сразу ─────────
    if not wait:
        # pre_run_id — наш собственный, не от раннера. Раннер сгенерирует
        # свой run_id внутри _call_runner_sync; связь через _PENDING
        # (real_run_id записывается после завершения).
        pre_run_id = uuid.uuid4().hex[:12]
        with _PENDING_LOCK:
            _PENDING[pre_run_id] = {
                "status": "running",
                "started_at": time.time(),
                "payload_dir": str(payload_dir),
                "mode": mode,
                "result": None,
            }
        _trim_pending()

        thread = threading.Thread(
            target=_run_in_background,
            args=(pre_run_id, payload_dir, mode),
            daemon=True,
            name=f"yaxunit-async-{pre_run_id}",
        )
        thread.start()

        return json.dumps({
            "run_id": pre_run_id,
            "status": "running",
            "mode": mode,
            "payload_dir": str(payload_dir),
            "hint": (
                f"Async mode. Опрашивайте test_run_status('{pre_run_id}') "
                "каждые 15-30 секунд. Прогон обычно занимает 30с-3мин в "
                "file-mode и 2-5мин в server-mode."
            ),
        }, ensure_ascii=False)

    # ── Sync-ветка: блокирующий вызов и возврат полного результата ───
    result = _call_runner_sync(payload_dir, mode)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def test_run(archive_base64: str, mode: str = "server") -> str:
    """
    [LEGACY] Запустить YAxUnit-тесты через base64-zip.

    DEPRECATED: предпочитайте test_run_path — он не раздувает LLM-контекст
    байтами конфигурации. Этот тул оставлен для обратной совместимости
    (skim-скрипты, ручные curl, случаи без shared volume).

    Параметры:
        archive_base64: ZIP-архив, закодированный в base64. Структура:
                        config/   — выгрузка основной конфигурации в XML
                        tests/    — выгрузка расширения с тестами в XML
                        yaxunit.json — опционально, настройки прогона
        mode: "server" (по умолчанию, требует серверной лицензии 1С) или
              "file" (файловая ИБ, быстрее, любая community-лицензия).

    Возвращает JSON: run_id, status (passed/failed/error), tests, failures,
    errors, duration_sec, extension, mode, exit_code, junit_xml, log.

    Таймаут — YAXUNIT_TIMEOUT секунд (по умолчанию 900). Превышение →
    status="error", error="timeout after Ns".
    """
    try:
        archive_bytes = base64.b64decode(archive_base64, validate=True)
    except Exception as e:
        return _runner_error(f"invalid base64: {e}")

    if mode not in ("server", "file"):
        return _runner_error(f"invalid mode '{mode}', expected 'server' or 'file'")

    files = {"archive": ("tests.zip", archive_bytes, "application/zip")}
    data = {"mode": mode}
    try:
        with httpx.Client(timeout=YAXUNIT_TIMEOUT) as client:
            r = client.post(f"{YAXUNIT_URL}/run_tests", files=files, data=data)
        if r.status_code >= 400:
            return _runner_error(
                f"runner returned HTTP {r.status_code}",
                response=r.text[:2000],
            )
        return r.text
    except httpx.TimeoutException:
        return _runner_error(f"timeout after {YAXUNIT_TIMEOUT}s")
    except Exception as e:
        return _runner_error(f"runner request failed: {e}")


@mcp.tool()
def test_run_status(run_id: str) -> str:
    """
    Получить детали прогона по run_id.

    Принимает два типа id:
      - pre_run_id из ответа test_run_path(wait=False): id, выданный
        самим mcp-testing для async-прогона. Если прогон ещё идёт —
        вернётся {status: "running", ...}; если завершён — полный
        результат раннера со status в {passed, failed, error}.
      - настоящий run_id раннера (из ответа test_run_path(wait=True),
        из test_run_list, либо real_run_id внутри async-результата).
        Идёт прямо в /runs/<id> у раннера.

    Сначала ищем в локальном реестре _PENDING (mcp-testing), потом —
    в раннере. Это значит, что pre_run_id найдётся даже после рестарта
    раннера, пока mcp-testing не перезапускался. И наоборот —
    настоящий run_id раннера найдётся даже если в _PENDING его нет
    (синхронные прогоны там не регистрируются).
    """
    # 1. Сначала локальный реестр async-прогонов
    with _PENDING_LOCK:
        entry = _PENDING.get(run_id)

    if entry is not None:
        if entry["status"] == "running":
            elapsed = round(time.time() - entry["started_at"], 1)
            return json.dumps({
                "run_id": run_id,
                "status": "running",
                "mode": entry.get("mode"),
                "elapsed_sec": elapsed,
                "payload_dir": entry.get("payload_dir"),
                "hint": (
                    f"Идёт {elapsed}с. Подождите ещё 15-30с и опросите снова. "
                    "Прогон обычно занимает 30с-3мин в file-mode и 2-5мин "
                    "в server-mode."
                ),
            }, ensure_ascii=False)
        # Завершён — отдаём результат как есть, добавив локальный run_id
        # для удобства (real_run_id раннера тоже есть в result.run_id).
        result = dict(entry["result"])
        result.setdefault("pre_run_id", run_id)
        return json.dumps(result, ensure_ascii=False)

    # 2. Не наш id — пробрасываем в раннер (настоящий run_id или опечатка)
    try:
        r = httpx.get(f"{YAXUNIT_URL}/runs/{run_id}", timeout=30)
        if r.status_code == 404:
            return _runner_error(
                f"run_id '{run_id}' not found in mcp-testing async registry "
                f"and not in runner history"
            )
        return r.text
    except Exception as e:
        return _runner_error(f"runner unreachable: {e}")


@mcp.tool()
def test_run_list() -> str:
    """
    Список последних прогонов (без JUnit/log payload — компактный summary).

    Включает:
      - active async-прогоны из локального реестра _PENDING
        (status: "running" — те, что ещё идут);
      - историю прогонов раннера (последние ~100, и passed, и failed/error).

    Удобно для проверки «что сейчас выполняется» и поиска нужного run_id.
    """
    # Active async runs — их в раннере ещё нет (пока тред не дописал)
    active: list[dict] = []
    with _PENDING_LOCK:
        for pre_id, entry in _PENDING.items():
            if entry["status"] != "running":
                continue
            active.append({
                "run_id": pre_id,
                "status": "running",
                "mode": entry.get("mode"),
                "started_at": entry["started_at"],
                "elapsed_sec": round(time.time() - entry["started_at"], 1),
                "source": "mcp-testing async registry",
            })

    # История раннера
    runner_runs: list[dict] = []
    runner_error: str | None = None
    try:
        r = httpx.get(f"{YAXUNIT_URL}/runs", timeout=30)
        if r.status_code < 400:
            try:
                payload = json.loads(r.text)
                runner_runs = payload.get("runs", [])
            except Exception:
                runner_error = "runner returned non-JSON"
        else:
            runner_error = f"runner HTTP {r.status_code}"
    except Exception as e:
        runner_error = f"runner unreachable: {e}"

    return json.dumps({
        "active": active,
        "history": runner_runs,
        "runner_error": runner_error,
    }, ensure_ascii=False)


# ─── Запуск ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    app = mcp.sse_app()
    port = int(os.environ.get("MCP_PORT", 8010))
    uvicorn.run(app, host="0.0.0.0", port=port)
