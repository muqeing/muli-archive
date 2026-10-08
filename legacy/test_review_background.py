import json
import threading
import time
import unittest
from urllib.request import Request, urlopen

from muli_sorter.archive_console import make_server
from muli_sorter.review_history_cache import ReviewHistoryCache
from muli_sorter.archive_history import file_identity
from muli_sorter.order_feed_io import digest

from test_archive_jobs import Fixture


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


class FakeClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, amount):
        self.value += amount


def simple_file(path="DCIM/IMG001.JPG", name="IMG001.JPG", size=3, digest="a" * 64):
    return {"source_path": path, "name": name, "size_bytes": size, "blake3": digest}


def simple_model(report_id="sha256:model", files=None, unit_id="unit-1"):
    return {
        "report_id": report_id,
        "units": [{"unit_id": unit_id, "files": list(files or [simple_file()])}],
    }


class ReviewHistoryCacheTests(unittest.TestCase):
    def test_single_worker_and_five_minute_expiry(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        clock = FakeClock()
        model = simple_model()

        class History:
            def snapshot(self, value):
                calls.append(value["report_id"])
                entered.set()
                release.wait(2)
                return {"report_id": value["report_id"], "archived_units": [], "warnings": []}

        cache = ReviewHistoryCache(History(), lambda: "same", interval=300, clock=clock)
        try:
            first = cache.view(model)
            second = cache.view(model)
            self.assertFalse(first[1])
            self.assertFalse(second[1])
            self.assertTrue(entered.wait(1))
            self.assertEqual(calls, [model["report_id"]])
            release.set()
            wait_until(lambda: cache.worker is None)
            self.assertTrue(cache.view(model)[1])
            clock.advance(299)
            self.assertTrue(cache.view(model)[1])
            clock.advance(2)
            self.assertFalse(cache.view(model)[1])
            wait_until(lambda: len(calls) == 2)
        finally:
            release.set()
            wait_until(lambda: cache.worker is None)
            cache.close()

    def test_signature_change_during_snapshot_is_discarded(self):
        entered = threading.Event()
        release = threading.Event()
        signature = ["before"]
        model = simple_model()

        class History:
            def snapshot(self, value):
                entered.set()
                release.wait(2)
                return {"report_id": value["report_id"], "archived_units": [], "warnings": []}

        cache = ReviewHistoryCache(History(), lambda: signature[0])
        try:
            cache.view(model)
            self.assertTrue(entered.wait(1))
            signature[0] = "after"
            release.set()
            wait_until(lambda: cache.worker is None)
            self.assertIsNone(cache.current)
            self.assertIsNone(cache.current_key)
        finally:
            release.set()
            wait_until(lambda: cache.worker is None)
            cache.close()

    def test_explicit_invalidate_during_snapshot_with_same_signature_discards_result(self):
        entered = threading.Event()
        release = threading.Event()
        model = simple_model()

        class History:
            def snapshot(self, value):
                entered.set()
                release.wait(2)
                return {"report_id": value["report_id"], "archived_units": [], "warnings": []}

        cache = ReviewHistoryCache(History(), lambda: "unchanged")
        try:
            cache.view(model)
            self.assertTrue(entered.wait(1))
            cache.invalidate()
            release.set()
            wait_until(lambda: cache.worker is None)
            self.assertIsNone(cache.current)
            self.assertIsNone(cache.current_key)
        finally:
            release.set()
            wait_until(lambda: cache.worker is None)
            cache.close()

    def test_async_failure_keeps_restored_view_read_only(self):
        entered = threading.Event()
        release = threading.Event()
        archive = {
            "report_id": "sha256:old",
            "archived_units": [],
            "warnings": [],
        }
        model = simple_model(archive["report_id"])

        class History:
            def snapshot(self, value):
                entered.set()
                release.wait(2)
                raise RuntimeError("history unavailable")

        cache = ReviewHistoryCache(History(), lambda: "same", restored=archive)
        try:
            view, fresh, error = cache.view(model)
            self.assertEqual(view, archive)
            self.assertFalse(fresh)
            self.assertIsNone(error)
            self.assertTrue(entered.wait(1))
            release.set()
            wait_until(lambda: cache.worker is None)
            view, fresh, error = cache.view(model)
            self.assertEqual(view, archive)
            self.assertFalse(fresh)
            self.assertIn("history unavailable", error)
        finally:
            release.set()
            wait_until(lambda: cache.worker is None)
            cache.close()

    def test_old_report_maps_only_matching_source_scope(self):
        first = simple_file(digest="b" * 64)
        second = simple_file(path="DCIM/IMG002.JPG", name="IMG002.JPG", digest="c" * 64)
        archive = {
            "report_id": "sha256:old",
            "archived_units": [{"unit_id": "unit-1", "files": [{"name":"IMG001.JPG","target_path":"project/IMG001.JPG","size_bytes":3}], "in_current_model": True}],
            "_display_scope_digests": {"unit-1":digest(file_identity([first]))},
            "warnings": [],
        }
        model = simple_model("sha256:new", [first])
        cache = ReviewHistoryCache(
            type("History", (), {"snapshot": lambda self, value: archive})(),
            lambda: "same",
            can_refresh=lambda: False,
            restored=archive,
        )
        try:
            mapped, fresh, error = cache.view(model)
            self.assertFalse(fresh)
            self.assertIsNone(error)
            self.assertTrue(mapped["archived_units"][0]["in_current_model"])
            saved_scopes = archive.pop("_display_scope_digests")
            unknown, _, _ = cache.view(model)
            self.assertFalse(unknown["archived_units"][0]["in_current_model"])
            archive["_display_scope_digests"] = saved_scopes

            changed = simple_model("sha256:changed", [second])
            mapped, _, _ = cache.view(changed)
            self.assertFalse(mapped["archived_units"][0]["in_current_model"])
        finally:
            cache.close()


class ReviewBackgroundHTTPTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = None
        self.thread = None
        self.release_events = []

    def tearDown(self):
        for event in self.release_events:
            event.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            if self.thread is not None:
                self.thread.join(2)
        super().tearDown()

    def start_server(self):
        self.server = make_server(self.service, self.root, host="127.0.0.1")
        self.origin = self.server.public_origin
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def get(self, route):
        request = Request(self.origin + route)
        with urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()

    def wait_page_ready(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self.get("/api/review-page")
                if self.server.page_cache.status().get("history_ready") is True:
                    return
            except (AttributeError, OSError):
                pass
            time.sleep(0.02)
        raise AssertionError("review page did not become history-ready")

    def get_page_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.get("/api/review-page")
            if predicate(last):
                return last
            time.sleep(0.02)
        raise AssertionError(f"review page response did not match predicate: {last!r}")

    def test_blocked_history_serves_read_only_page_and_summary_jobs(self):
        entered = threading.Event()
        release = threading.Event()
        self.release_events.append(release)
        original = self.service.history.snapshot

        def blocked(model):
            entered.set()
            release.wait(5)
            return original(model)

        self.service.history.snapshot = blocked
        self.start_server()

        status, _, body = self.get_page_until(lambda response: response[0] == 200)
        self.assertEqual(status, 200)
        self.assertIn(b'id="snapshot-readonly"', body)
        self.assertIn(b'"submission_enabled":false', body)
        self.assertTrue(entered.wait(1))

        status, _, raw = self.get("/api/jobs?view=summary")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertIn("jobs", payload)
        self.assertTrue(all("file_plans" not in job for job in payload["jobs"]))

        release.set()
        wait_until(lambda: self.server.history_cache.worker is None)
        for _ in range(100):
            self.get("/api/review-page")
            if self.server.page_cache.status().get("history_ready") is True:
                break
            time.sleep(0.02)
        self.assertTrue(self.server.page_cache.status().get("history_ready"))

    def test_saved_display_reopens_read_only_without_preflight(self):
        self.start_server()
        self.get("/api/review-page")
        self.wait_page_ready()
        snapshot = self.service.state / "review-display-cache" / "display-snapshot.json"
        wait_until(snapshot.is_file)

        loaded = self.server.page_cache.display_store.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(set(loaded["bundle"]), {"html_bytes", "report_id", "archive_state", "history_generation"})
        self.assertNotIn("preview_id", loaded)

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.server = None
        self.thread = None

        entered = threading.Event()
        release = threading.Event()
        self.release_events.append(release)
        original = self.service.history.snapshot

        def blocked(model):
            entered.set()
            release.wait(5)
            return original(model)

        self.service.history.snapshot = blocked
        self.start_server()
        status, _, body = self.get("/api/review-page")
        self.assertEqual(status, 200)
        self.assertIn(b'id="snapshot-readonly"', body)
        self.assertIn(b'"submission_enabled":false', body)
        self.assertTrue(entered.wait(1))
        release.set()

    def test_compact_archive_state_uses_prepared_bundle(self):
        self.start_server()
        self.get("/api/review-page")
        self.wait_page_ready()
        bundle = self.server.page_cache.get_bundle()
        self.assertIsNotNone(bundle)
        report_id = bundle["report_id"]
        calls = []

        def forbidden(model):
            calls.append(model["report_id"])
            raise AssertionError("compact archive state must not rescan history")

        self.service.history.snapshot = forbidden
        status, _, raw = self.get("/api/archive-state?view=compact&report_id=" + report_id)
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["report_id"], report_id)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
