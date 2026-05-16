# AGENTS.md — 1C:Enterprise Configuration "Котировки"

## Project Overview

This is a **1C:Enterprise (1С:Предприятие 8.3)** configuration named "Котировки" (Quotes) — an auction management system. It is stored in **EDT XML dump format**. The configuration compatibility mode is `Version8_3_24` with **Russian script variant**.

## Build / Lint / Test Commands

This is NOT a typical web project. There is no npm, no Jest, no webpack. The project uses **MCP servers** for tooling:

### Static Analysis (BSL Language Server)
- `bsl_check_code(code)` — check a BSL code snippet
- `bsl_check_file(file_path)` — check a single `.bsl` file
- `bsl_check_directory(dir_path)` — check all `.bsl` files in a directory (e.g. `/workspace/CommonModules/`)

### SonarQube Validation (MANDATORY gate for any generated BSL)
Every time you (the agent) write or modify BSL code, you **MUST** run SonarQube validation
before considering the task done. SonarQube is the authoritative Quality Gate.
- `sonar_scan_code(code, module_name, project_key="")` — scan a snippet; returns Quality Gate + issues
- `sonar_scan_directory(dir_path, project_key, project_name)` — full scan of a folder
- `sonar_quality_gate(project_key)` — re-check Quality Gate status (OK / ERROR / WARN)
- `sonar_get_issues(project_key, severities, limit)` — fetch open issues
- `sonar_list_projects()` — list projects on the server

**Hard rules:**
1. Always pass `module_name` to `sonar_scan_code` — use the **fully-qualified 1C module name**
   you are currently editing (e.g. `"ОбщегоНазначения"`, `"Документ.ЗаказПокупателя.МодульОбъекта"`,
   `"Справочник.Контрагенты.МодульМенеджера"`). The server derives a stable `project_key` from it,
   so the same module always lands in the same SonarQube project and issue history is preserved.
   Do NOT invent random keys and do NOT omit `module_name` — that creates a new ghost project on
   every call and pollutes the dashboard.
2. If `sonar_scan_code` or `sonar_quality_gate` returns Quality Gate status `ERROR`, fix the
   reported issues and re-scan **with the same `module_name`** until Quality Gate is `OK`.
3. Do not present code to the user as final until Quality Gate passes.

### AI Code Review (1С:Напарник)
- `naparnik_review(code, context)` — review code for logic and standards
- `naparnik_check_code(code)` — check code for errors
- `naparnik_fix(code, error_description, context)` — fix code issues
- `naparnik_generate_comment(code, context)` — generate doc comments
- `naparnik_explain(code, context)` — explain code
- `naparnik_add_code(description, context, existing_code)` — generate new code

### Metadata Graph (Neo4j)
- `metadata_search(query, kind)` — search metadata objects
- `metadata_object_details(full_name)` — get object details (attributes, tabular sections)
- `metadata_references_to(full_name)` — what objects reference this one (impact analysis)
- `metadata_references_from(full_name)` — what this object references
- `metadata_dependency_tree(full_name, depth)` — dependency tree
- `metadata_list_objects(kind)` / `metadata_list_kinds()` / `metadata_subsystems()` / `metadata_stats()` — enumerate configuration
- `metadata_cypher(query)` — raw read-only Cypher for advanced exploration
- `metadata_reload()` — force reindex after XML changes

### Platform Help & Templates
- `platform_help_search(query)` — semantic search in 1C platform docs
- `platform_help_details(name)` — full docs for a method/type
- `platform_help_categories()` / `platform_help_stats()` — enumerate help content
- `its_search(query)` — search ITS articles (standards, best practices)
- `search_all(query)` — search both platform help and ITS in one call
- `templates_search(query)` / `templates_by_tags(tags)` / `templates_list_tags()` / `templates_count()` — find BSL code templates

### Query Builder (1C query language)
- `query_build(description, object_name)` — build a 1C query from natural-language description using real metadata
- `query_fields(object_name)` — list available fields for an object
- `query_join_hint(table1, table2)` — suggest the correct join between two objects
- `query_validate(query_text)` — validate query syntax and table/field names
- `query_optimize(query_text)` — suggest optimizations (indexes, rewrite patterns)

### REST Proxy (read-only access to a live 1C database)
**ONLY when explicitly asked to query real data.** Every call hits the production base.
- `connection_info()` / `test_connection()` — check if the base is reachable before any call
- `odata_list_entities(filter_kind)` — list OData entity sets
- `odata_metadata(entity_name)` — fetch entity schema
- `odata_get(entity_name, filter, select, expand, top)` — query records (GET $filter)
- `odata_get_by_key(entity_name, ref_key, expand)` — fetch one record by Ref key
- `http_service_call(...)` — call custom HTTP services (blocked for modifying methods when `ONEC_READ_ONLY=true`)

### Code RAG (semantic search over BSL in /workspace)
- `code_search(query, module_type)` — semantic search over indexed procedures
- `code_similar(procedure_code)` — find procedures similar to a given snippet
- `code_patterns(task)` — surface recurring patterns across the codebase
- `code_reindex()` — force reindex after adding new modules

### Testing (YAxUnit / Vanessa-Automation)
Generation tools work always; runner tools require the optional `yaxunit-stack` profile.
- `test_generate(code)` / `test_template(kind)` / `test_scenario(description)` / `test_data_suggest(object_name)` / `test_coverage_analyze(code)` — generation
- `test_runner_health()` — ALWAYS call first before attempting a run
- `test_run(archive_base64, mode)` — run tests; returns run_id + JUnit XML
- `test_run_status(run_id)` / `test_run_list()` — inspect results

### Orchestrator (deprecated)

The orchestrator MCP server has been removed. For complex tasks that need analysis + coding + review, just invoke the relevant MCP tools directly — opencode handles multi-step workflows natively via Tool Use API. Going through a separate orchestrator added 20–30× latency overhead with no quality benefit (see `docs/historical/orchestrator_benchmark.py` for the measurement that drove this decision).

## Code Style Guidelines

### Language & Variant
- **Script variant**: Russian (`ScriptVariant=Russian`) — all keywords in Russian: `Процедура`, `Функция`, `Если`, `Для Каждого`, `Возврат`, etc.
- **Boolean literals**: `Истина`, `Ложь`, `Неопределено`
- **String literals**: Russian strings use `НСтр("ru = '...'")`

### File Structure
- Modules are stored as `.bsl` files in `Ext/Module.bsl` subdirectories
- Common modules: `/workspace/CommonModules/<Name>/Ext/Module.bsl`
- Catalog object modules: `/workspace/Catalogs/<Name>/Ext/ObjectModule.bsl`
- Form modules: `/workspace/Catalogs/<Name>/Forms/<FormName>/Ext/Form/Module.bsl`

### Naming Conventions
- **Common modules**: prefix by subsystem, e.g. `Аук_ОбщийСервер`, `Аук_УправлениеАукционамиСервер`
- **Metadata objects**: prefix `Аук_` for auction subsystem, `УБД_` for user DB subsystem
- **Variables**: Hungarian-style prefixes observed: `п` (parameter), `стк` (structure), `мас` (array), `тз` (value table), `соо` (mapping), `чсл` (number), `бул` (boolean), `стр` (string)
- **Exported procedures/functions**: suffix `_` for internal/private helpers, e.g. `ПеревестиАукционНаНовыйЭтап_()`
- **Technical names** (ТехническоеНаименование): Latin alphanumeric, e.g. `Percent`, `Sum`, `CountOfDays`

### Module Structure
- Use `#Область ... #КонецОбласти` regions to organize code
- Standard regions: `ПрограммныйИнтерфейс`, `СлужебныеПроцедурыИФункции`, `РаботаСРегистрамиСведений`, `РаботаСФормами`, `ВалидацияДанных`, `Логирование`
- Exported API goes in `ПрограммныйИнтерфейс` region

### Documentation Comments
- Document all exported procedures/functions with structured comments:
  ```bsl
  // Параметры:
  //  пАукцион - СправочникСсылка.Аук_Аукционы - описание
  //  пВидАукциона - СправочникСсылка.Аук_ВидыАукционов - описание
  //
  // Возвращаемое значение:
  //   Структура - описание результата
  //
  Функция ИмяФункции(пАукцион, пВидАукциона) Экспорт
  ```

### Error Handling
- Use `Попытка ... Исключение ... КонецПопытки` for exception handling
- Use `ВызватьИсключение НСтр("ru = '...'")` for throwing errors
- Return result structures with `Успех`/`Ошибка` pattern via `ЭдоОбщий._Успех()` / `ЭдоОбщий._Ошибка()`
- User notifications: `ОбщегоНазначенияКлиентСервер.СообщитьПользователю(ТекстОшибки)`

### Client/Server Directives
- Use `&НаКлиенте` for client-side code
- Use `&НаСервере` for server-side code (implicit when absent in server modules)
- Server call modules: `*ВызовСервера` — thin layer for client→server calls
- Client-server modules: `*КлиентСервер` — shared utility code

### Query Style
- SQL-like queries in Russian: `ВЫБРАТЬ ... ИЗ ... ГДЕ`
- Use parameterized queries with `&Параметр` syntax
- Replace table names dynamically: `СтрЗаменить(Запрос.Текст, "&Таблица", ИмяТаблицы)`

### Data Writing
- Use `ОбщегоНазначения.ЗаписатьОбъектВБазу()` for writing objects to DB
- Set `ОбменДанными.Загрузка = Загрузка` for data exchange mode

## Available Skills (in `.opencode/skills/`)

| Skill | Purpose |
|---|---|
| `explore-metadata` | Navigate configuration metadata via the Neo4j graph — objects, attributes, subsystems, dependencies |
| `platform-help-lookup` | Search 1C platform docs and ITS articles — syntax, methods, best practices |
| `impact-analysis` | Assess change impact on dependent objects before refactoring |
| `architect-design` | Design a new object or subsystem — metadata structure, attributes, registers, rollout plan |
| `module-scaffold` | Create a new module / data processor / report from scratch (metadata + templates + docs + checks) |
| `code-from-template` | Apply a ready template for a typical BSL task (queries, processings, forms, data exchange) |
| `query-build` | Build a 1C query from a natural-language description with metadata-aware table/field checks |
| `bsl-review` | Static analysis via BSL Language Server and auto-fix of findings |
| `naparnik-review` | AI code review via 1С:Напарник — review, fix, explain, generate doc comments |
| `rest-query` | Query a live 1C base via OData / HTTP services (read-only by default) |
| `test-generate` | Generate tests for BSL code — YAxUnit, Vanessa, coverage analysis, test data |

## Workflow for Adding Code

1. **Explore metadata** — use `metadata_search` / `metadata_object_details` to find real attribute names
2. **Look for existing patterns** — use `code_search` to see how similar tasks are already solved in this codebase
3. **Find templates** — use `templates_search` for code patterns
4. **Check docs** — use `platform_help_search` / `search_all` for API details
5. **For queries** — use `query_build` with real metadata instead of hand-writing; validate with `query_validate`
6. **Write code** — follow conventions above
7. **Validate** — run `bsl_check_code` + `naparnik_review` + **`sonar_scan_code` (must pass Quality Gate — see Hard rules above)**
8. **Fix issues** — use `naparnik_fix` for any problems found; re-scan with the same `module_name` until Quality Gate is OK
9. **Tests (for non-trivial code)** — use `test_generate` / `test_template`; if the optional `yaxunit-stack` is up, run via `test_run`
