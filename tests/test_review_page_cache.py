import json
import threading
import time
import unittest

from muli_sorter.review_page_cache import ReviewPageCache


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, amount):
        self.value += amount


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


class ReviewPageCacheTests(unittest.TestCase):
    def bundle(self, text="ready", report_id="sha256:r1"):
        return {"html_bytes": text.encode(), "archive_state": {"report_id": report_id}, "report_id": report_id}

    def test_concurrent_reads_start_one_build_and_publish_html(self):
        release = threading.Event()
        entered = threading.Event()
        calls = []

        def builder(set_phase):
            calls.append(1)
            set_phase("扫描素材")
            entered.set()
            release.wait(2)
            return self.bundle()

        cache = ReviewPageCache(builder, lambda: "sig")
        results = []
        threads = [threading.Thread(target=lambda: results.append(cache.read())) for _ in range(8)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(1))
        for thread in threads:
            thread.join(1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(result[0] == 202 for result in results))
        state = json.loads(cache.read()[1])
        self.assertEqual(state["status"], "building")
        self.assertEqual(state["phase"], "扫描素材")
        release.set()
        wait_until(lambda: cache.get_bundle("sha256:r1") is not None)
        self.assertEqual(cache.read(), (200, b"ready", "text/html; charset=utf-8"))

    def test_blocked_builder_keeps_reads_fast_and_shutdown_does_not_join(self):
        entered = threading.Event()
        release = threading.Event()

        def builder(set_phase):
            entered.set()
            release.wait(2)
            return self.bundle()

        cache = ReviewPageCache(builder, lambda: "sig")
        self.assertEqual(cache.read()[0], 202)
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        self.assertEqual(cache.read()[0], 202)
        self.assertLess(time.monotonic() - started, 0.2)
        cache.shutdown()
        self.assertEqual(cache.read()[0], 409)
        release.set()

    def test_ttl_and_signature_invalidation_never_serve_stale_html(self):
        clock = FakeClock()
        key = ["one"]
        count = []

        def builder(set_phase):
            count.append(1)
            return self.bundle(str(len(count)))

        cache = ReviewPageCache(builder, lambda: tuple(key), ttl=10, clock=clock)
        self.assertEqual(cache.read()[0], 202)
        wait_until(lambda: cache.get_bundle() is not None)
        self.assertEqual(cache.read()[1], b"1")
        key[:] = ["two"]
        self.assertIsNone(cache.get_bundle())
        self.assertEqual(cache.read()[0], 202)
        wait_until(lambda: cache.get_bundle() is not None)
        self.assertEqual(cache.read()[1], b"2")
        clock.advance(11)
        self.assertIsNone(cache.get_bundle())
        self.assertEqual(cache.read()[0], 202)
        wait_until(lambda: cache.get_bundle() is not None)
        self.assertEqual(cache.read()[1], b"3")

    def test_builder_error_is_409_then_retries_after_delay(self):
        clock = FakeClock()
        calls = []

        def builder(set_phase):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("builder broke")
            return self.bundle()

        cache = ReviewPageCache(builder, lambda: "sig", retry_delay=5, clock=clock)
        self.assertEqual(cache.read()[0], 202)
        wait_until(lambda: len(calls) == 1 and cache.read()[0] == 409)
        self.assertEqual(len(calls), 1)
        clock.advance(4)
        self.assertEqual(cache.read()[0], 409)
        self.assertEqual(len(calls), 1)
        clock.advance(1)
        self.assertEqual(cache.read()[0], 202)
        wait_until(lambda: cache.get_bundle() is not None)
        self.assertEqual(len(calls), 2)

    def test_changed_signature_during_build_discards_result_and_next_read_rebuilds(self):
        clock = FakeClock()
        key = ["before"]
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def builder(set_phase):
            calls.append(1)
            entered.set()
            release.wait(2)
            return self.bundle("discarded" if len(calls) == 1 else "published")

        cache = ReviewPageCache(builder, lambda: tuple(key), clock=clock)
        self.assertEqual(cache.read()[0], 202)
        self.assertTrue(entered.wait(1))
        key[:] = ["after"]
        release.set()
        wait_until(lambda: cache._state == "invalidated")
        self.assertIsNone(cache.get_bundle())
        self.assertEqual(cache.read()[0], 202)
        self.assertEqual(len(calls), 2)
        wait_until(lambda: cache.get_bundle() is not None)
        self.assertEqual(cache.read()[1], b"published")

    def test_explicit_invalidate_discards_ready_and_building_result(self):
        clock = FakeClock()
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def builder(set_phase):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            return self.bundle(str(len(calls)))

        cache = ReviewPageCache(builder, lambda: "sig", clock=clock)
        self.assertEqual(cache.read()[0], 202)
        self.assertTrue(entered.wait(1))
        cache.invalidate()
        self.assertIsNone(cache.get_bundle())
        release.set()
        wait_until(lambda: cache._state == "invalidated")
        self.assertEqual(cache.read()[0], 202)
        wait_until(lambda: cache.get_bundle() is not None)
        self.assertEqual(cache.read()[1], b"2")

    def test_mismatched_bundle_report_is_not_published(self):
        bundle = self.bundle()
        bundle['archive_state']['report_id'] = 'other'
        cache = ReviewPageCache(lambda phase: bundle, lambda: 'sig')
        self.assertEqual(cache.read()[0], 202)
        wait_until(lambda: cache._state == 'failed')
        self.assertIsNone(cache.get_bundle())
        self.assertEqual(cache.read()[0], 409)


if __name__ == "__main__":
    unittest.main()
