"""
Юнит-тесты для workspace_watcher.py (изменения в рамках задачи 4.6.5).

Конкретно проверяется:
  • DebouncedQueue теперь дедупит по (path, target), а НЕ по path. Это
    позволяет одному .bsl-событию жить как два независимых debounce-таймера
    (один для code-rag, другой для metadata-graph).
  • CodeXmlHandler фанаутит .bsl в оба таргета, когда METADATA_WATCH_ENABLED=true.
  • При METADATA_WATCH_ENABLED=false поведение остаётся прежним (только code).
  • .xml идёт только в metadata-таргет (никогда в code).

Тесты пользуются прямой подменой модульных переменных через
`workspace_watcher.METADATA_WATCH_ENABLED = ...` — это требует impорта
самого модуля, не FromImport (как и в реальном коде).
"""
from __future__ import annotations

import sys
import threading
import time
import types
import unittest


# Прежде чем импортировать workspace_watcher — подсунем заглушки для его
# опциональных deps (`mcp` и `watchdog`), которых в окружении юнит-тестов
# может не быть. Реальные dependencies нужны только для async-MCP-вызовов
# и для запуска Observer'а — оба пути в тестах не активируются.

def _stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


if "mcp" not in sys.modules:
    _stub_module("mcp", ClientSession=object)
    _stub_module("mcp.client.sse", sse_client=lambda *a, **kw: None)

if "watchdog" not in sys.modules:
    class _FSEvent:
        is_directory = False
        src_path = ""

    class _FSEventHandler:
        def on_created(self, e): pass
        def on_modified(self, e): pass
        def on_deleted(self, e): pass
        def on_moved(self, e): pass

    class _Observer:
        def schedule(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def join(self, **kw): pass

    _stub_module("watchdog")
    _stub_module("watchdog.events",
                 FileSystemEvent=_FSEvent, FileSystemEventHandler=_FSEventHandler)
    _stub_module("watchdog.observers", Observer=_Observer)
    _stub_module("watchdog.observers.polling", PollingObserver=_Observer)


import workspace_watcher as ww  # noqa: E402


# ─── DebouncedQueue: дедупликация по (path, target) ──────────────────────


class TestDebouncedQueueKeying(unittest.TestCase):
    """
    Главный инвариант: события с одним path и РАЗНЫМИ target живут как
    два отдельных слота в очереди. До 4.6.5 ключ был просто `path`, и
    второй enqueue ПЕРЕЗАПИСЫВАЛ target первого — fan-out был невозможен.
    """

    def test_same_path_different_targets_are_separate_slots(self):
        handled: list[ww.PendingEvent] = []
        q = ww.DebouncedQueue(debounce_sec=0.05, handler=handled.append)
        # НЕ стартуем background-тред — гоняем pending руками.

        path = "/workspace/CommonModules/X/Ext/Module.bsl"
        q.enqueue(ww.PendingEvent(path=path, kind="upsert", target="code"))
        q.enqueue(ww.PendingEvent(path=path, kind="upsert", target="metadata"))

        # Внутреннее состояние — два слота.
        self.assertEqual(len(q._pending), 2)
        keys = set(q._pending.keys())
        self.assertEqual(keys, {(path, "code"), (path, "metadata")})

    def test_same_path_same_target_dedupes(self):
        q = ww.DebouncedQueue(debounce_sec=0.05, handler=lambda _e: None)
        path = "/workspace/X.bsl"

        q.enqueue(ww.PendingEvent(path=path, kind="upsert", target="code"))
        q.enqueue(ww.PendingEvent(path=path, kind="upsert", target="code"))
        q.enqueue(ww.PendingEvent(path=path, kind="upsert", target="code"))

        self.assertEqual(len(q._pending), 1)

    def test_last_event_kind_wins_within_same_key(self):
        """upsert → remove на одном (path, target) даёт итог remove."""
        q = ww.DebouncedQueue(debounce_sec=0.05, handler=lambda _e: None)
        path = "/workspace/X.bsl"

        q.enqueue(ww.PendingEvent(path=path, kind="upsert", target="code"))
        q.enqueue(ww.PendingEvent(path=path, kind="remove", target="code"))

        self.assertEqual(len(q._pending), 1)
        ev = q._pending[(path, "code")]
        self.assertEqual(ev.kind, "remove")

    def test_different_paths_are_obviously_separate(self):
        q = ww.DebouncedQueue(debounce_sec=0.05, handler=lambda _e: None)
        q.enqueue(ww.PendingEvent(path="/a.bsl", kind="upsert", target="code"))
        q.enqueue(ww.PendingEvent(path="/b.bsl", kind="upsert", target="code"))
        self.assertEqual(len(q._pending), 2)

    def test_drain_dispatches_both_targets_separately(self):
        """
        Полный путь: enqueue два таргета с одним path → ждём debounce →
        обработчик получает оба события.
        """
        handled: list[ww.PendingEvent] = []
        handler_done = threading.Event()
        # handler заранее знает, сколько событий ожидать
        expected = 2

        def _handler(ev):
            handled.append(ev)
            if len(handled) >= expected:
                handler_done.set()

        q = ww.DebouncedQueue(debounce_sec=0.1, handler=_handler)
        q.start()
        try:
            path = "/workspace/X.bsl"
            q.enqueue(ww.PendingEvent(path=path, kind="upsert", target="code"))
            q.enqueue(ww.PendingEvent(path=path, kind="upsert", target="metadata"))
            # Ждём обработки.
            self.assertTrue(handler_done.wait(timeout=2.0),
                            "Обработчик не получил оба события за 2 сек")
        finally:
            q.stop()

        targets = {ev.target for ev in handled}
        self.assertEqual(targets, {"code", "metadata"})
        # Path одинаковый.
        self.assertEqual({ev.path for ev in handled}, {path})


# ─── CodeXmlHandler: правильная маршрутизация / fan-out ──────────────────


class FakeQueue:
    """Минимальная очередь-сборщик: складывает enqueue'нутые события в list."""

    def __init__(self):
        self.events: list[ww.PendingEvent] = []

    def enqueue(self, ev: ww.PendingEvent) -> None:
        self.events.append(ev)


class TestEnqueuePathRouting(unittest.TestCase):
    """
    _enqueue_path — приватный, но это основная точка маршрутизации. Тесты
    подменяют модульный METADATA_WATCH_ENABLED, чтобы проверить оба режима.
    """

    def setUp(self):
        self._saved_meta_enabled = ww.METADATA_WATCH_ENABLED
        self.q = FakeQueue()
        self.handler = ww.CodeXmlHandler(self.q)

    def tearDown(self):
        ww.METADATA_WATCH_ENABLED = self._saved_meta_enabled

    def test_bsl_metadata_disabled_only_code_target(self):
        ww.METADATA_WATCH_ENABLED = False
        self.handler._enqueue_path("/ws/X.bsl", kind="upsert")
        self.assertEqual(len(self.q.events), 1)
        self.assertEqual(self.q.events[0].target, "code")
        self.assertEqual(self.q.events[0].kind, "upsert")

    def test_bsl_metadata_enabled_fans_out_to_both(self):
        ww.METADATA_WATCH_ENABLED = True
        self.handler._enqueue_path("/ws/X.bsl", kind="upsert")
        targets = sorted(ev.target for ev in self.q.events)
        self.assertEqual(targets, ["code", "metadata"])
        # Оба события с одинаковым path и kind.
        self.assertEqual({ev.path for ev in self.q.events}, {"/ws/X.bsl"})
        self.assertEqual({ev.kind for ev in self.q.events}, {"upsert"})

    def test_xml_metadata_disabled_no_event(self):
        ww.METADATA_WATCH_ENABLED = False
        self.handler._enqueue_path("/ws/Catalogs/X.xml", kind="upsert")
        self.assertEqual(self.q.events, [])

    def test_xml_metadata_enabled_only_metadata_target(self):
        ww.METADATA_WATCH_ENABLED = True
        self.handler._enqueue_path("/ws/Catalogs/X.xml", kind="upsert")
        self.assertEqual(len(self.q.events), 1)
        self.assertEqual(self.q.events[0].target, "metadata")

    def test_os_extension_treated_as_code(self):
        ww.METADATA_WATCH_ENABLED = False
        self.handler._enqueue_path("/ws/X.os", kind="upsert")
        self.assertEqual(len(self.q.events), 1)
        self.assertEqual(self.q.events[0].target, "code")

    def test_unknown_extension_ignored(self):
        ww.METADATA_WATCH_ENABLED = True
        self.handler._enqueue_path("/ws/README.md", kind="upsert")
        self.handler._enqueue_path("/ws/.gitignore", kind="upsert")
        self.handler._enqueue_path("/ws/data.json", kind="upsert")
        self.assertEqual(self.q.events, [])

    def test_ignored_dirs_filtered(self):
        ww.METADATA_WATCH_ENABLED = True
        self.handler._enqueue_path("/ws/.git/X.bsl", kind="upsert")
        self.handler._enqueue_path("/ws/node_modules/foo.bsl", kind="upsert")
        self.handler._enqueue_path("/ws/__pycache__/x.bsl", kind="upsert")
        self.assertEqual(self.q.events, [])

    def test_editor_temp_files_filtered(self):
        ww.METADATA_WATCH_ENABLED = True
        self.handler._enqueue_path("/ws/~X.bsl", kind="upsert")
        self.handler._enqueue_path("/ws/X.bsl.swp", kind="upsert")
        self.handler._enqueue_path("/ws/#X.bsl#", kind="upsert")
        self.assertEqual(self.q.events, [])

    def test_remove_event_fans_out_too(self):
        """remove должен дойти до обоих таргетов так же, как upsert."""
        ww.METADATA_WATCH_ENABLED = True
        self.handler._enqueue_path("/ws/X.bsl", kind="remove")
        self.assertEqual(len(self.q.events), 2)
        self.assertEqual({ev.kind for ev in self.q.events}, {"remove"})

    def test_uppercase_extension_normalized(self):
        ww.METADATA_WATCH_ENABLED = False
        self.handler._enqueue_path("/ws/X.BSL", kind="upsert")
        self.assertEqual(len(self.q.events), 1)
        self.assertEqual(self.q.events[0].target, "code")


# ─── Интеграция: handler+queue вместе ────────────────────────────────────


class TestHandlerQueueIntegration(unittest.TestCase):
    """
    Проверка: watchdog-событие на .bsl при METADATA_WATCH_ENABLED=true
    кладёт в очередь два слота, оба переживают debounce и приходят в обработчик.
    """

    def setUp(self):
        self._saved = ww.METADATA_WATCH_ENABLED

    def tearDown(self):
        ww.METADATA_WATCH_ENABLED = self._saved

    def test_bsl_modification_drains_two_events(self):
        ww.METADATA_WATCH_ENABLED = True

        handled: list[ww.PendingEvent] = []
        handler_done = threading.Event()

        def _h(ev):
            handled.append(ev)
            if len(handled) >= 2:
                handler_done.set()

        q = ww.DebouncedQueue(debounce_sec=0.1, handler=_h)
        q.start()
        try:
            handler = ww.CodeXmlHandler(q)
            handler._enqueue_path("/workspace/X.bsl", kind="upsert")
            self.assertTrue(handler_done.wait(timeout=2.0),
                            "Не дождались двух событий")
        finally:
            q.stop()

        self.assertEqual({ev.target for ev in handled}, {"code", "metadata"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
