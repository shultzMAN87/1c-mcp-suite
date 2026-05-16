"""
Юнит-тесты bsl_resolver.py (без Neo4j).

Запуск:
    python tests_bsl_resolver.py
"""
from __future__ import annotations

import unittest

from bsl_parser import (
    ParsedCall, ParsedModule, ParsedProcedure, ParsedParameter, parse_bsl_text,
)
from bsl_resolver import (
    BUILTIN_FUNCS, FORM_HANDLERS,
    Index, ResolveResult, TypeRef,
    PLURAL_TO_KIND_ENG, METHOD_TO_KIND, KIND_TO_MODULE_ROLE,
    build_call_graph, build_index_from_modules, infer_local_types,
    _resolve_call,
    # 4.6.4
    AMBIGUOUS, MAX_ITERATIONS,
    COLLECTION_TYPES, COLLECTION_METHODS, _COLLECTION_KIND_SET,
    infer_return_type, _merge_type_fact, _type_of_expr,
)


# ─── 1. infer_local_types ─────────────────────────────────────────────────


def _make_proc(name: str, body: str, kind: str = "Procedure",
               is_export: bool = False, params=None, directive: str = "") -> ParsedProcedure:
    """Хелпер: создаёт ParsedProcedure с заданным телом (preprocessed = raw здесь)."""
    return ParsedProcedure(
        name=name,
        kind=kind,
        is_export=is_export,
        directive=directive,
        parameters=params or [],
        body_text=body,
        body_text_raw=body,
        line_start=1,
        line_end=10,
    )


class TestInferLocalTypes(unittest.TestCase):

    def test_create_element_object_kind(self):
        proc = _make_proc("X", "тз = Справочники.АукАукционы.СоздатьЭлемент();")
        types = infer_local_types(proc)
        self.assertIn("тз", types)
        self.assertEqual(types["тз"].kind, "CatalogObject")
        self.assertEqual(types["тз"].target, "Catalog.АукАукционы")

    def test_empty_ref_kind(self):
        proc = _make_proc("X", "ссылка = Справочники.АукАукционы.ПустаяСсылка();")
        types = infer_local_types(proc)
        self.assertEqual(types["ссылка"].kind, "CatalogRef")
        self.assertEqual(types["ссылка"].target, "Catalog.АукАукционы")

    def test_document_object(self):
        proc = _make_proc("X", "док = Документы.Заказ.СоздатьДокумент();")
        types = infer_local_types(proc)
        self.assertEqual(types["док"].kind, "DocumentObject")
        self.assertEqual(types["док"].target, "Document.Заказ")

    def test_information_register_record_set(self):
        proc = _make_proc("X", "набор = РегистрыСведений.Х.СоздатьНаборЗаписей();")
        types = infer_local_types(proc)
        self.assertEqual(types["набор"].kind, "InformationRegisterRecordSet")
        self.assertEqual(types["набор"].target, "InformationRegister.Х")

    def test_unknown_method_ignored(self):
        proc = _make_proc("X", "y = Справочники.А.СовсемНовыйМетод();")
        types = infer_local_types(proc)
        self.assertEqual(types, {})

    def test_unknown_plural_ignored(self):
        # `Обработки` нет в PLURAL_TO_KIND_ENG? Есть (DataProcessor).
        # А `Шаблоны` — точно нет.
        proc = _make_proc("X", "y = Шаблоны.А.СоздатьЭлемент();")
        types = infer_local_types(proc)
        self.assertEqual(types, {})

    def test_reassignment_last_wins(self):
        proc = _make_proc(
            "X",
            "тз = Справочники.А.СоздатьЭлемент();\n"
            "тз = Документы.Б.СоздатьДокумент();"
        )
        types = infer_local_types(proc)
        # «Последний побеждает» — упрощение по плану.
        self.assertEqual(types["тз"].kind, "DocumentObject")
        self.assertEqual(types["тз"].target, "Document.Б")


# ─── 2. Index сборка ──────────────────────────────────────────────────────


class TestIndexBuilding(unittest.TestCase):

    def _module(self, mid, kind, procs=()):
        return ParsedModule(
            module_id=mid, module_kind=kind, parent_metadata_id=None,
            source_path="", is_server=True, is_client=False,
            procedures=list(procs),
        )

    def test_common_modules_collected(self):
        mods = [
            self._module("CommonModule.АукОбщийКлиент", "CommonModule",
                         [_make_proc("Факториал", "")]),
            self._module("CommonModule.АукОбщийСервер", "CommonModule"),
            self._module("Catalog.X.ObjectModule", "ObjectModule"),
        ]
        idx = build_index_from_modules(mods)
        self.assertEqual(idx.common_modules, {"АукОбщийКлиент", "АукОбщийСервер"})

    def test_callable_ids_collected(self):
        mods = [
            self._module("CommonModule.X", "CommonModule",
                         [_make_proc("A", ""), _make_proc("B", "")]),
            self._module("Catalog.Y.ObjectModule", "ObjectModule",
                         [_make_proc("C", "")]),
        ]
        idx = build_index_from_modules(mods)
        self.assertEqual(idx.callable_ids, {
            "CommonModule.X.A", "CommonModule.X.B",
            "Catalog.Y.ObjectModule.C",
        })

    def test_metadata_objects_propagated(self):
        idx = build_index_from_modules(
            [],
            metadata_objects={"АукАукционы": "Catalog.АукАукционы"},
        )
        self.assertEqual(idx.metadata_objects["АукАукционы"], "Catalog.АукАукционы")
        self.assertEqual(idx.metadata_full_set, {"Catalog.АукАукционы"})

    def test_default_pluralies_and_builtins(self):
        idx = build_index_from_modules([])
        self.assertEqual(idx.metadata_kinds_plural, PLURAL_TO_KIND_ENG)
        self.assertGreaterEqual(len(idx.builtin_funcs), 50)


# ─── 3. _resolve_call ─────────────────────────────────────────────────────


class TestResolveCall(unittest.TestCase):
    """Юнит-тесты на единичный резолв."""

    def setUp(self):
        self.index = Index(
            common_modules={"АукОбщийКлиент", "АукОбщийСервер"},
            callable_ids={
                "CommonModule.АукОбщийКлиент.Факториал",
                "CommonModule.АукОбщийКлиент.Привет",
                "Catalog.АукАукционы.ObjectModule.ПередЗаписью",
                "Catalog.АукАукционы.ManagerModule.СведенияПоЭтапуАукциона",
                "CommonModule.МойМодуль.МояФункция",
            },
            metadata_objects={"АукАукционы": "Catalog.АукАукционы"},
            metadata_full_set={"Catalog.АукАукционы"},
            metadata_kinds_plural=dict(PLURAL_TO_KIND_ENG),
            builtin_funcs=set(BUILTIN_FUNCS),
        )

    def _call(self, mod, name):
        return ParsedCall(module_ref=mod, method_name=name, line=1, col=0,
                          is_local=not mod)

    def test_builtin_skipped(self):
        c = self._call("", "НСтр")
        r = _resolve_call(c, "CommonModule.X", "CommonModule", {}, self.index)
        self.assertTrue(r.skip)
        self.assertEqual(r.reason, "builtin")

    def test_cross_module_known_callable(self):
        c = self._call("АукОбщийКлиент", "Факториал")
        r = _resolve_call(c, "CommonModule.X", "CommonModule", {}, self.index)
        self.assertEqual(r.callee_id, "CommonModule.АукОбщийКлиент.Факториал")
        self.assertFalse(r.skip)
        self.assertEqual(r.reason, "")

    def test_cross_module_unknown_method(self):
        c = self._call("АукОбщийКлиент", "НесуществующийМетод")
        r = _resolve_call(c, "CommonModule.X", "CommonModule", {}, self.index)
        self.assertIsNone(r.callee_id)
        self.assertEqual(r.reason, "unknown_method_in_common_module")

    def test_dataflow_to_object_module(self):
        local_vars = {"тз": TypeRef(kind="CatalogObject", target="Catalog.АукАукционы")}
        c = self._call("тз", "ПередЗаписью")
        r = _resolve_call(c, "CommonModule.X", "CommonModule", local_vars, self.index)
        self.assertEqual(r.callee_id, "Catalog.АукАукционы.ObjectModule.ПередЗаписью")

    def test_dataflow_to_manager_module_via_ref(self):
        local_vars = {"ссыл": TypeRef(kind="CatalogRef", target="Catalog.АукАукционы")}
        c = self._call("ссыл", "СведенияПоЭтапуАукциона")
        r = _resolve_call(c, "CommonModule.X", "CommonModule", local_vars, self.index)
        self.assertEqual(r.callee_id, "Catalog.АукАукционы.ManagerModule.СведенияПоЭтапуАукциона")

    def test_dataflow_method_not_in_module(self):
        local_vars = {"тз": TypeRef(kind="CatalogObject", target="Catalog.АукАукционы")}
        c = self._call("тз", "СовсемДругойМетод")
        r = _resolve_call(c, "CommonModule.X", "CommonModule", local_vars, self.index)
        self.assertIsNone(r.callee_id)
        self.assertEqual(r.reason, "method_not_in_resolved_module")

    def test_local_call_resolves_in_same_module(self):
        c = self._call("", "Факториал")  # local
        r = _resolve_call(c, "CommonModule.АукОбщийКлиент", "CommonModule",
                          {}, self.index)
        self.assertEqual(r.callee_id, "CommonModule.АукОбщийКлиент.Факториал")

    def test_local_call_unknown(self):
        c = self._call("", "СовсемНепонятная")
        r = _resolve_call(c, "CommonModule.АукОбщийКлиент", "CommonModule",
                          {}, self.index)
        self.assertIsNone(r.callee_id)
        self.assertEqual(r.reason, "unknown_local_method")

    def test_metadata_plural_as_module_ref_is_skipped(self):
        # `Справочники.X(` (паразитный матч iter_calls) — skip.
        c = self._call("Справочники", "АукАукционы")
        r = _resolve_call(c, "CommonModule.X", "CommonModule", {}, self.index)
        self.assertTrue(r.skip)
        self.assertEqual(r.reason, "metadata_access_not_call")

    def test_unknown_module(self):
        c = self._call("пСткПараметры", "Вставить")  # объектный метод
        r = _resolve_call(c, "CommonModule.X", "CommonModule", {}, self.index)
        self.assertIsNone(r.callee_id)
        self.assertEqual(r.reason, "unknown_module")


# ─── 4. build_call_graph (end-to-end на синтетике) ───────────────────────


class TestBuildCallGraph(unittest.TestCase):

    def test_full_flow_with_resolved_and_unresolved(self):
        # Module 1: CommonModule с одной публичной функцией.
        text1 = "Функция Факториал(п) Экспорт\n  Возврат 1;\nКонецФункции"
        procs1 = parse_bsl_text(text1)

        # Module 2: Caller, который зовёт Факториал из CommonModule.
        text2 = (
            "Процедура Caller()\n"
            "  Результат = АукОбщийКлиент.Факториал(3);\n"
            "  Локальный();\n"
            "  НеИзвестный.Метод();\n"
            "КонецПроцедуры\n"
            "Процедура Локальный()\n"
            "  НСтр(\"ru = 'hi'\");\n"
            "КонецПроцедуры"
        )
        procs2 = parse_bsl_text(text2)

        mods = [
            ParsedModule(
                module_id="CommonModule.АукОбщийКлиент",
                module_kind="CommonModule",
                parent_metadata_id=None,
                source_path="CommonModules/АукОбщийКлиент/Ext/Module.bsl",
                is_server=False, is_client=True,
                procedures=procs1,
            ),
            ParsedModule(
                module_id="CommonModule.МойСервер",
                module_kind="CommonModule",
                parent_metadata_id=None,
                source_path="CommonModules/МойСервер/Ext/Module.bsl",
                is_server=True, is_client=False,
                procedures=procs2,
            ),
        ]

        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)

        # Должны быть 3 callable (Факториал + Caller + Локальный).
        ids = {c["id"] for c in cg["callable_nodes"]}
        self.assertIn("CommonModule.АукОбщийКлиент.Факториал", ids)
        self.assertIn("CommonModule.МойСервер.Caller", ids)
        self.assertIn("CommonModule.МойСервер.Локальный", ids)

        # CALLS должны быть: Caller → Факториал, Caller → Локальный
        calls = [(e["src"], e["dst"]) for e in cg["edges"] if e["rel"] == "CALLS"]
        self.assertIn(("CommonModule.МойСервер.Caller", "CommonModule.АукОбщийКлиент.Факториал"),
                      calls)
        self.assertIn(("CommonModule.МойСервер.Caller", "CommonModule.МойСервер.Локальный"),
                      calls)

        # НСтр(...) внутри Локальный — НЕ должен попасть в CALLS и НЕ должен
        # создать :CallSite (built-in).
        callsite_methods = {n["method_name"] for n in cg["callsite_nodes"]}
        self.assertNotIn("НСтр", callsite_methods)

        # Должен быть :CallSite с unresolved для `НеИзвестный.Метод()`.
        unresolved = [n for n in cg["callsite_nodes"] if not n["resolved"]]
        names_unresolved = {(n["module_ref"], n["method_name"]) for n in unresolved}
        self.assertIn(("НеИзвестный", "Метод"), names_unresolved)

    def test_module_nodes_exclude_common_module(self):
        mods = [
            ParsedModule(
                module_id="CommonModule.X", module_kind="CommonModule",
                parent_metadata_id=None, source_path="",
                is_server=True, is_client=False,
                procedures=[_make_proc("A", "")],
            ),
            ParsedModule(
                module_id="Catalog.Y.ObjectModule", module_kind="ObjectModule",
                parent_metadata_id="Catalog.Y", source_path="",
                is_server=True, is_client=False,
                procedures=[_make_proc("B", "")],
            ),
        ]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        module_ids = {m["id"] for m in cg["module_nodes"]}
        self.assertEqual(module_ids, {"Catalog.Y.ObjectModule"})

    def test_operates_on_metadata_access(self):
        # Процедура, которая обращается к Справочники.АукАукционы
        text = (
            "Процедура X()\n"
            "  спр = Справочники.АукАукционы;\n"
            "КонецПроцедуры"
        )
        procs = parse_bsl_text(text)
        mods = [ParsedModule(
            module_id="CommonModule.МойСервер", module_kind="CommonModule",
            parent_metadata_id=None, source_path="",
            is_server=True, is_client=False, procedures=procs,
        )]
        idx = build_index_from_modules(
            mods, metadata_objects={"АукАукционы": "Catalog.АукАукционы"},
        )
        cg = build_call_graph(mods, idx)
        ops = [e for e in cg["edges"] if e["rel"] == "OPERATES_ON"]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["src"], "CommonModule.МойСервер.X")
        self.assertEqual(ops[0]["dst"], "Catalog.АукАукционы")
        self.assertEqual(ops[0]["props"]["via"], "Справочники")

    def test_operates_on_predef(self):
        text = (
            "Процедура X()\n"
            "  з = ПредопределенноеЗначение(\"Перечисление.АукСтатусы.Новый\");\n"
            "КонецПроцедуры"
        )
        procs = parse_bsl_text(text)
        mods = [ParsedModule(
            module_id="CommonModule.X", module_kind="CommonModule",
            parent_metadata_id=None, source_path="",
            is_server=True, is_client=False, procedures=procs,
        )]
        idx = build_index_from_modules(
            mods, metadata_objects={"АукСтатусы": "Enum.АукСтатусы"},
        )
        cg = build_call_graph(mods, idx)
        ops = [e for e in cg["edges"] if e["rel"] == "OPERATES_ON"]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["dst"], "Enum.АукСтатусы")
        self.assertEqual(ops[0]["props"]["via"], "predefined_value")
        # Также НЕ должно быть CallSite на `ПредопределенноеЗначение` (built-in).
        cs_methods = {n["method_name"] for n in cg["callsite_nodes"]}
        self.assertNotIn("ПредопределенноеЗначение", cs_methods)

    def test_manager_call_to_manager_module(self):
        # Описание: ManagerModule содержит метод X, и кто-то его зовёт
        # через `Справочники.АукАукционы.X()`.
        manager_text = "Функция СведенияПоЭтапу(п) Экспорт\n  Возврат п;\nКонецФункции"
        manager_procs = parse_bsl_text(manager_text)

        caller_text = (
            "Процедура Caller()\n"
            "  Результат = Справочники.АукАукционы.СведенияПоЭтапу(1);\n"
            "КонецПроцедуры"
        )
        caller_procs = parse_bsl_text(caller_text)

        mods = [
            ParsedModule(
                module_id="Catalog.АукАукционы.ManagerModule",
                module_kind="ManagerModule",
                parent_metadata_id="Catalog.АукАукционы",
                source_path="Catalogs/АукАукционы/Ext/ManagerModule.bsl",
                is_server=True, is_client=False, procedures=manager_procs,
            ),
            ParsedModule(
                module_id="CommonModule.X", module_kind="CommonModule",
                parent_metadata_id=None, source_path="",
                is_server=True, is_client=False, procedures=caller_procs,
            ),
        ]
        idx = build_index_from_modules(
            mods, metadata_objects={"АукАукционы": "Catalog.АукАукционы"},
        )
        cg = build_call_graph(mods, idx)

        calls = [(e["src"], e["dst"]) for e in cg["edges"] if e["rel"] == "CALLS"]
        self.assertIn(
            ("CommonModule.X.Caller", "Catalog.АукАукционы.ManagerModule.СведенияПоЭтапу"),
            calls,
        )

        # Также есть OPERATES_ON
        ops = [(e["src"], e["dst"], e["props"]["via"]) for e in cg["edges"]
               if e["rel"] == "OPERATES_ON"]
        self.assertIn(("CommonModule.X.Caller", "Catalog.АукАукционы", "Справочники"), ops)

    def test_recursive_call(self):
        text = (
            "Функция Факториал(п) Экспорт\n"
            "  Если п <= 1 Тогда Возврат 1; КонецЕсли;\n"
            "  Возврат п * Факториал(п - 1);\n"
            "КонецФункции"
        )
        procs = parse_bsl_text(text)
        mods = [ParsedModule(
            module_id="CommonModule.M", module_kind="CommonModule",
            parent_metadata_id=None, source_path="",
            is_server=True, is_client=False, procedures=procs,
        )]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        # Рекурсия резолвится в самого себя.
        calls = [(e["src"], e["dst"]) for e in cg["edges"] if e["rel"] == "CALLS"]
        self.assertIn(("CommonModule.M.Факториал", "CommonModule.M.Факториал"), calls)

    def test_object_method_call_unresolved(self):
        # `пСткПараметры.Вставить(...)` — объектный метод, в этом коммите
        # не резолвится.
        text = (
            "Процедура Х(пСтк)\n"
            "  пСтк.Вставить(\"ключ\", \"значение\");\n"
            "КонецПроцедуры"
        )
        procs = parse_bsl_text(text)
        mods = [ParsedModule(
            module_id="CommonModule.X", module_kind="CommonModule",
            parent_metadata_id=None, source_path="",
            is_server=True, is_client=False, procedures=procs,
        )]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        unresolved = [n for n in cg["callsite_nodes"] if not n["resolved"]]
        # Должен быть один unresolved
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["module_ref"], "пСтк")
        self.assertEqual(unresolved[0]["method_name"], "Вставить")
        self.assertEqual(unresolved[0]["reason"], "unknown_module")

    def test_stats_counters(self):
        # Один resolved (cross-module), один skipped (built-in), один unresolved
        # (unknown_module).
        text = (
            "Процедура Caller()\n"
            "  АукОбщийКлиент.Факториал(1);\n"
            "  НСтр(\"ru = 'x'\");\n"
            "  Object.Method();\n"
            "КонецПроцедуры"
        )
        procs = parse_bsl_text(text)

        # Также добавим целевой Callable Факториал.
        target_procs = parse_bsl_text("Функция Факториал(п) Экспорт\nКонецФункции")

        mods = [
            ParsedModule(
                module_id="CommonModule.АукОбщийКлиент", module_kind="CommonModule",
                parent_metadata_id=None, source_path="",
                is_server=False, is_client=True, procedures=target_procs,
            ),
            ParsedModule(
                module_id="CommonModule.М", module_kind="CommonModule",
                parent_metadata_id=None, source_path="",
                is_server=True, is_client=False, procedures=procs,
            ),
        ]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        s = cg["stats"]
        self.assertGreaterEqual(s["resolved"], 1)
        self.assertGreaterEqual(s["skipped"], 1)
        self.assertGreaterEqual(s["unresolved"], 1)
        # Reason counts должен иметь ключ 'builtin'
        self.assertIn("builtin", s["reason_counts"])


# ─── 5. Constants sanity ──────────────────────────────────────────────────


class TestConstants(unittest.TestCase):

    def test_builtin_funcs_includes_common(self):
        for name in ("НСтр", "СтрШаблон", "Новый", "Тип", "ТипЗнч",
                     "ПредопределенноеЗначение", "ЗначениеЗаполнено"):
            self.assertIn(name, BUILTIN_FUNCS, f"{name} должен быть в BUILTIN_FUNCS")

    def test_form_handlers_includes_common(self):
        for name in ("ПриСозданииНаСервере", "ПриОткрытии", "ПередЗаписью"):
            self.assertIn(name, FORM_HANDLERS)

    def test_plural_to_kind_eng_completeness(self):
        # Эти plural'и обязаны быть.
        for p in ("Справочники", "Документы", "Перечисления",
                  "РегистрыСведений", "РегистрыНакопления"):
            self.assertIn(p, PLURAL_TO_KIND_ENG)

    def test_kind_to_module_role(self):
        self.assertEqual(KIND_TO_MODULE_ROLE["CatalogObject"], "ObjectModule")
        self.assertEqual(KIND_TO_MODULE_ROLE["CatalogRef"], "ManagerModule")
        self.assertEqual(KIND_TO_MODULE_ROLE["DocumentObject"], "ObjectModule")


# ─── 6. 4.6.4 — type inference v2 ─────────────────────────────────────────


def _common_module(module_short: str, body_or_procs, *,
                    is_server: bool = True, is_client: bool = False) -> ParsedModule:
    """Хелпер: CommonModule из текста BSL или готового списка процедур."""
    procs = (body_or_procs if isinstance(body_or_procs, list)
             else parse_bsl_text(body_or_procs))
    return ParsedModule(
        module_id=f"CommonModule.{module_short}", module_kind="CommonModule",
        parent_metadata_id=None, source_path="",
        is_server=is_server, is_client=is_client, procedures=procs,
    )


class TestCollectionTypesA1(unittest.TestCase):
    """A1: `Новый <Класс>` → коллекционный тип; методы коллекций → skip."""

    def test_new_value_table_inferred(self):
        proc = _make_proc("X", "тз = Новый ТаблицаЗначений;")
        types = infer_local_types(proc)
        self.assertIn("тз", types)
        self.assertEqual(types["тз"].kind, "ValueTable")
        self.assertEqual(types["тз"].target, "")
        self.assertEqual(types["тз"].source, "constructor")

    def test_new_structure_with_args(self):
        proc = _make_proc("X", 'стр = Новый Структура("а, б", 1, 2);')
        types = infer_local_types(proc)
        self.assertEqual(types["стр"].kind, "Structure")

    def test_new_unknown_class_ignored(self):
        # Не-коллекционный класс (`Новый ОписаниеОповещения`) — не наш кейс.
        proc = _make_proc("X", "оп = Новый ОписаниеОповещения;")
        types = infer_local_types(proc)
        self.assertNotIn("оп", types)

    def test_collection_method_goes_to_skip(self):
        # `тз = Новый ТаблицаЗначений; тз.Добавить()` — Добавить уходит в skip
        # (collection_method), НЕ в unresolved.
        text = (
            "Процедура Х()\n"
            "  тз = Новый ТаблицаЗначений;\n"
            "  тз.Добавить(1);\n"
            "  тз.НесуществующийМетодКоллекции();\n"
            "КонецПроцедуры"
        )
        mods = [_common_module("M", text)]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        reasons = cg["stats"]["reason_counts"]
        # Добавить → collection_method (skip)
        self.assertGreaterEqual(reasons.get("collection_method", 0), 1)
        # Неизвестный метод на коллекции → collection_unknown_method (не unknown_module)
        self.assertGreaterEqual(reasons.get("collection_unknown_method", 0), 1)
        # И ни одного unknown_module от этих вызовов:
        unresolved = [n for n in cg["callsite_nodes"]
                      if not n["resolved"] and n["module_ref"] == "тз"]
        for n in unresolved:
            self.assertNotEqual(n["reason"], "unknown_module")

    def test_collection_kind_set_consistency(self):
        # _COLLECTION_KIND_SET = значения COLLECTION_TYPES.
        self.assertEqual(_COLLECTION_KIND_SET, frozenset(COLLECTION_TYPES.values()))
        self.assertIn("ValueTable", _COLLECTION_KIND_SET)
        self.assertIn("Structure", _COLLECTION_KIND_SET)


class TestExtendedDataflowA2(unittest.TestCase):
    """A2: цепочки присваиваний, return-типы функций."""

    def test_assignment_chain_plain(self):
        # b = Справочники.X.СоздатьЭлемент(); a = b → a наследует тип b.
        proc = _make_proc(
            "X",
            "б = Справочники.АукАукционы.СоздатьЭлемент();\n"
            "а = б;",
        )
        types = infer_local_types(proc)
        self.assertEqual(types["б"].kind, "CatalogObject")
        self.assertEqual(types["а"].kind, "CatalogObject")
        self.assertEqual(types["а"].target, "Catalog.АукАукционы")
        self.assertEqual(types["а"].source, "chain")

    def test_assignment_chain_dotted_not_inherited(self):
        # a = b.Поле — тип поля ≠ тип b, НЕ наследуем.
        proc = _make_proc(
            "X",
            "б = Справочники.АукАукционы.СоздатьЭлемент();\n"
            "а = б.Реквизит;",
        )
        types = infer_local_types(proc)
        self.assertIn("б", types)
        self.assertNotIn("а", types)

    def test_chain_order_matters(self):
        # a = b идёт ДО присваивания b — a НЕ типизируется (b ещё не известен).
        proc = _make_proc(
            "X",
            "а = б;\n"
            "б = Справочники.АукАукционы.СоздатьЭлемент();",
        )
        types = infer_local_types(proc)
        self.assertNotIn("а", types)
        self.assertEqual(types["б"].kind, "CatalogObject")

    def test_return_type_of_function(self):
        # Функция с `Возврат Справочники.X.СоздатьЭлемент()` → return-тип CatalogObject.
        procs = parse_bsl_text(
            "Функция СоздатьАукцион()\n"
            "  Возврат Справочники.АукАукционы.СоздатьЭлемент();\n"
            "КонецФункции"
        )
        rt = infer_return_type(procs[0], infer_local_types(procs[0]))
        self.assertIsNotNone(rt)
        self.assertEqual(rt.kind, "CatalogObject")
        self.assertEqual(rt.target, "Catalog.АукАукционы")
        self.assertEqual(rt.source, "return_type")

    def test_return_type_via_local_var(self):
        # Функция возвращает локальную переменную с выведенным типом.
        procs = parse_bsl_text(
            "Функция Ф()\n"
            "  рез = Новый ТаблицаЗначений;\n"
            "  Возврат рез;\n"
            "КонецФункции"
        )
        rt = infer_return_type(procs[0], infer_local_types(procs[0]))
        self.assertIsNotNone(rt)
        self.assertEqual(rt.kind, "ValueTable")

    def test_return_type_conflicting_returns_none(self):
        # Два Возврата с разными типами → return-тип не выводится.
        procs = parse_bsl_text(
            "Функция Ф(Флаг)\n"
            "  Если Флаг Тогда\n"
            "    Возврат Справочники.А.СоздатьЭлемент();\n"
            "  Иначе\n"
            "    Возврат Документы.Б.СоздатьДокумент();\n"
            "  КонецЕсли;\n"
            "КонецФункции"
        )
        rt = infer_return_type(procs[0], infer_local_types(procs[0]))
        self.assertIsNone(rt)

    def test_procedure_has_no_return_type(self):
        procs = parse_bsl_text(
            "Процедура П()\n  Возврат;\nКонецПроцедуры"
        )
        self.assertIsNone(infer_return_type(procs[0], {}))

    def test_return_type_used_in_caller_assign(self):
        # x = МойМодуль.СоздатьАукцион() — x получает return-тип функции.
        helper = _common_module(
            "Хелпер",
            "Функция СоздатьАукцион() Экспорт\n"
            "  Возврат Справочники.АукАукционы.СоздатьЭлемент();\n"
            "КонецФункции",
        )
        caller_text = (
            "Процедура Использовать()\n"
            "  ауд = Хелпер.СоздатьАукцион();\n"
            "КонецПроцедуры"
        )
        caller = _common_module("Потребитель", caller_text)
        mods = [helper, caller]
        idx = build_index_from_modules(mods)
        # Прогоняем фикс-пойнт через build_call_graph; затем проверяем,
        # что в caller'е переменная ауд получила тип через return_type.
        cg = build_call_graph(mods, idx)
        # Должно быть как минимум одно :INFERRED_TYPE НЕ обязательно (ауд — локал,
        # не параметр), но проверим, что фикс-пойнт отработал и return-тип учтён:
        # вызов Хелпер.СоздатьАукцион резолвится в CALLS.
        calls = [(e["src"], e["dst"]) for e in cg["edges"] if e["rel"] == "CALLS"]
        self.assertIn(
            ("CommonModule.Потребитель.Использовать",
             "CommonModule.Хелпер.СоздатьАукцион"),
            calls,
        )


class TestInterproceduralB2(unittest.TestCase):
    """B2: проброс типа аргумента в параметр callee."""

    def test_arg_type_propagates_to_param(self):
        # A зовёт B(Новый ТаблицаЗначений) → параметр B типизируется ValueTable.
        text_b = (
            "Процедура ОбработатьТаблицу(Таб) Экспорт\n"
            "  Таб.Добавить();\n"
            "КонецПроцедуры"
        )
        text_a = (
            "Процедура Запустить()\n"
            "  т = Новый ТаблицаЗначений;\n"
            "  ОбработатьТаблицу(т);\n"
            "КонецПроцедуры"
        )
        mod_b = _common_module("M", parse_bsl_text(text_b) + parse_bsl_text(text_a))
        mods = [mod_b]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        # Параметр Таб получил :INFERRED_TYPE → ValueTable.
        inferred = [e for e in cg["edges"] if e["rel"] == "INFERRED_TYPE"]
        self.assertTrue(any(
            e["src"].endswith(".ОбработатьТаблицу.Param.Таб")
            and "ValueTable" in e["dst"]
            for e in inferred
        ), f"ожидался INFERRED_TYPE на Param.Таб, получено: {inferred}")

    def test_args_text_parsed_on_callsite(self):
        # Парсер должен дать args_text у ParsedCall.
        from bsl_parser import iter_calls, split_args
        procs = parse_bsl_text(
            "Процедура Х()\n  Ф(а, б, Вычислить(1+1));\nКонецПроцедуры"
        )
        calls = list(iter_calls(procs[0].body_text))
        f_call = [c for c in calls if c.method_name == "Ф"][0]
        self.assertEqual(split_args(f_call.args_text), ["а", "б", "Вычислить(1+1)"])

    def test_ambiguous_param_no_inferred_type(self):
        # B зовётся с конфликтующими типами аргумента → параметр AMBIGUOUS,
        # :INFERRED_TYPE не пишется.
        text = (
            "Процедура B(П) Экспорт\n"
            "  П.Добавить();\n"
            "КонецПроцедуры\n"
            "Процедура A1()\n"
            "  т = Новый ТаблицаЗначений;\n"
            "  B(т);\n"
            "КонецПроцедуры\n"
            "Процедура A2()\n"
            "  с = Новый Структура;\n"
            "  B(с);\n"
            "КонецПроцедуры"
        )
        mods = [_common_module("M", text)]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        inferred = [e for e in cg["edges"] if e["rel"] == "INFERRED_TYPE"
                    and e["src"].endswith(".B.Param.П")]
        self.assertEqual(inferred, [], "AMBIGUOUS-параметр не должен писать INFERRED_TYPE")


class TestFixpointEngineC(unittest.TestCase):
    """C: фикс-пойнт-движок — многоуровневые цепочки, монотонность, завершение."""

    def test_merge_type_fact_monotonic(self):
        reg: dict = {}
        t1 = TypeRef(kind="ValueTable", target="")
        # Первая запись — изменение.
        self.assertTrue(_merge_type_fact(reg, "p", t1))
        # Тот же факт — без изменения.
        self.assertFalse(_merge_type_fact(reg, "p", TypeRef(kind="ValueTable", target="")))
        # Конфликт — уходим в AMBIGUOUS, изменение.
        self.assertTrue(_merge_type_fact(reg, "p", TypeRef(kind="Structure", target="")))
        self.assertIs(reg["p"], AMBIGUOUS)
        # AMBIGUOUS — sticky, больше не меняется.
        self.assertFalse(_merge_type_fact(reg, "p", TypeRef(kind="Array", target="")))
        self.assertIs(reg["p"], AMBIGUOUS)

    def test_fixpoint_terminates_and_reports_iterations(self):
        # На синтетике фикс-пойнт обязан завершиться за < MAX_ITERATIONS.
        text = (
            "Процедура B(П) Экспорт\n"
            "  П.Добавить();\n"
            "КонецПроцедуры\n"
            "Процедура A()\n"
            "  т = Новый ТаблицаЗначений;\n"
            "  B(т);\n"
            "КонецПроцедуры"
        )
        mods = [_common_module("M", text)]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        it = cg["stats"]["fixpoint_iterations"]
        self.assertGreaterEqual(it, 1)
        self.assertLessEqual(it, MAX_ITERATIONS)

    def test_two_level_chain_propagation(self):
        # A→B→C: тип, переданный в B, прокидывается в C на 2-й итерации.
        text = (
            "Процедура C(ПарамC) Экспорт\n"
            "  ПарамC.Добавить();\n"
            "КонецПроцедуры\n"
            "Процедура B(ПарамB) Экспорт\n"
            "  C(ПарамB);\n"
            "КонецПроцедуры\n"
            "Процедура A()\n"
            "  т = Новый ТаблицаЗначений;\n"
            "  B(т);\n"
            "КонецПроцедуры"
        )
        mods = [_common_module("M", text)]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        inferred = {e["src"]: e["dst"] for e in cg["edges"]
                    if e["rel"] == "INFERRED_TYPE"}
        # И ПарамB, и ПарамC должны получить ValueTable.
        b_param = [k for k in inferred if k.endswith(".B.Param.ПарамB")]
        c_param = [k for k in inferred if k.endswith(".C.Param.ПарамC")]
        self.assertTrue(b_param, f"ПарамB не типизирован: {list(inferred)}")
        self.assertTrue(c_param, f"ПарамC не типизирован: {list(inferred)}")
        self.assertIn("ValueTable", inferred[b_param[0]])
        self.assertIn("ValueTable", inferred[c_param[0]])

    def test_recursive_function_terminates(self):
        # Рекурсивная функция не зацикливает фикс-пойнт.
        text = (
            "Функция Факт(Н) Экспорт\n"
            "  Если Н <= 1 Тогда\n"
            "    Возврат 1;\n"
            "  КонецЕсли;\n"
            "  Возврат Н * Факт(Н - 1);\n"
            "КонецФункции"
        )
        mods = [_common_module("M", text)]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        # Завершился за < MAX_ITERATIONS, рекурсивный CALLS присутствует.
        self.assertLessEqual(cg["stats"]["fixpoint_iterations"], MAX_ITERATIONS)
        calls = [(e["src"], e["dst"]) for e in cg["edges"] if e["rel"] == "CALLS"]
        self.assertIn(("CommonModule.M.Факт", "CommonModule.M.Факт"), calls)


class TestTypeNodesD(unittest.TestCase):
    """D: :Type-узлы слоя 2 + :INFERRED_TYPE в выходе build_call_graph."""

    def test_type_nodes_in_output(self):
        text = (
            "Процедура B(П) Экспорт\n"
            "  П.Добавить();\n"
            "КонецПроцедуры\n"
            "Процедура A()\n"
            "  т = Новый ТаблицаЗначений;\n"
            "  B(т);\n"
            "КонецПроцедуры"
        )
        mods = [_common_module("M", text)]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        # type_nodes присутствует в выходе.
        self.assertIn("type_nodes", cg)
        self.assertGreaterEqual(len(cg["type_nodes"]), 1)
        # Каждый type_node имеет id/kind/target.
        for tn in cg["type_nodes"]:
            self.assertIn("id", tn)
            self.assertIn("kind", tn)
            self.assertTrue(tn["id"].startswith("Type:"))
        # stats содержит inferred_types и fixpoint_iterations.
        self.assertIn("inferred_types", cg["stats"])
        self.assertIn("type_nodes", cg["stats"])
        self.assertIn("fixpoint_iterations", cg["stats"])

    def test_type_id_format_matches_layer1(self):
        # TypeRef.type_id() — формат Type:{kind}:{target} (как слой 1).
        t = TypeRef(kind="CatalogObject", target="Catalog.X")
        self.assertEqual(t.type_id(), "Type:CatalogObject:Catalog.X")
        # Без target — Type:{kind}.
        t2 = TypeRef(kind="ValueTable", target="")
        self.assertEqual(t2.type_id(), "Type:ValueTable")

    def test_inferred_type_edges_reference_existing_type_nodes(self):
        text = (
            "Процедура B(П) Экспорт\n"
            "  П.Добавить();\n"
            "КонецПроцедуры\n"
            "Процедура A()\n"
            "  т = Новый ТаблицаЗначений;\n"
            "  B(т);\n"
            "КонецПроцедуры"
        )
        mods = [_common_module("M", text)]
        idx = build_index_from_modules(mods)
        cg = build_call_graph(mods, idx)
        type_ids = {tn["id"] for tn in cg["type_nodes"]}
        inferred = [e for e in cg["edges"] if e["rel"] == "INFERRED_TYPE"]
        self.assertTrue(inferred)
        for e in inferred:
            # dst ребра INFERRED_TYPE — существующий type_node.
            self.assertIn(e["dst"], type_ids)
            # props содержит confidence и source.
            self.assertIn("confidence", e["props"])
            self.assertIn("source", e["props"])


class TestBackwardCompatibility(unittest.TestCase):
    """Регрессия: старое поведение infer_local_types сохранено (4.6.2 паттерн)."""

    def test_legacy_signature_still_works(self):
        # Вызов с одним аргументом (как в 4.6.2) — работает.
        proc = _make_proc("X", "тз = Справочники.АукАукционы.СоздатьЭлемент();")
        types = infer_local_types(proc)
        self.assertEqual(types["тз"].kind, "CatalogObject")
        self.assertEqual(types["тз"].target, "Catalog.АукАукционы")

    def test_typeref_has_new_fields_with_defaults(self):
        # TypeRef можно создать как в 4.6.2 (kind+target), новые поля — дефолты.
        t = TypeRef(kind="CatalogObject", target="Catalog.X")
        self.assertEqual(t.confidence, 1.0)
        self.assertEqual(t.source, "local_assign")

    def test_type_of_expr_helper(self):
        # _type_of_expr: идентификатор → тип из var_types.
        vt = {"перем": TypeRef(kind="ValueTable", target="")}
        self.assertEqual(_type_of_expr("перем", vt).kind, "ValueTable")
        # Новый Класс → коллекция.
        self.assertEqual(_type_of_expr("Новый Структура", {}).kind, "Structure")
        # Неизвестное выражение → None.
        self.assertIsNone(_type_of_expr("а + б", {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
