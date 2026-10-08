"""A bounded, single-worker cache for the review page.

The page builder may inspect slow local resources, so callers receive a small
JSON progress response while one daemon worker builds the page.  A completed
bundle is published only when the cheap source signature is unchanged across
the build.  The cache deliberately keeps at most one bundle and never serves
an invalidated result. Optional bounded display reuse is explicitly labelled;
strict bundle access still requires a fresh result.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import threading
import time
from typing import Any, Callable

from .review_refresh import display_html


HTML_CONTENT_TYPE = "text/html; charset=utf-8"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"


class ReviewPageCache:
    """Coordinate one background review-page build and its short-lived result.

    ``builder`` is called as ``builder(set_phase)``.  It must return a bundle
    containing HTML bytes, canonical archive state, and a report ID.  The
    cache does not inspect or rewrite the latter two values; they remain
    available to the route layer through :meth:`get_bundle`.
    """

    def __init__(
        self,
        builder: Callable[[Callable[[str], None]], Any],
        signature: Callable[[], Any],
        ttl: float | None = 30,
        retry_delay: float = 5,
        clock: Callable[[], float] = time.monotonic,
        display_grace: float = 0,
    ) -> None:
        if not callable(builder) or not callable(signature):
            raise TypeError("builder and signature must be callable")
        if (ttl is not None and ttl < 0) or retry_delay < 0 or display_grace < 0:
            raise ValueError("ttl and retry_delay must be non-negative")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.builder = builder
        self.signature = signature
        self.ttl = None if ttl is None else float(ttl)
        self.display_grace = float(display_grace)
        self.retry_delay = float(retry_delay)
        self.clock = clock

        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._generation = 0
        self._closed = False
        self._state = "idle"
        self._phase = "等待页面构建"
        self._started_at: float | None = None
        self._building_key: Any = None
        self._ready: dict[str, Any] | None = None
        self._failure: dict[str, Any] | None = None
        self._invalidate_after_build = False
        self._maintenance_stop = threading.Event()
        self._maintenance = None

    @staticmethod
    def _same(left: Any, right: Any) -> bool:
        try:
            result = left == right
            return bool(result)
        except Exception:
            return False

    @staticmethod
    def _bundle_value(bundle: Any, name: str) -> Any:
        if isinstance(bundle, Mapping):
            return bundle.get(name)
        return getattr(bundle, name, None)

    @classmethod
    def _validate_bundle(cls, bundle: Any) -> Any:
        html = cls._bundle_value(bundle, "html_bytes")
        if html is None:
            html = cls._bundle_value(bundle, "html")
        if isinstance(html, bytearray):
            html = bytes(html)
        if not isinstance(html, bytes):
            raise TypeError("review page bundle must contain HTML bytes")
        archive_state = cls._bundle_value(bundle, "archive_state")
        report_id = cls._bundle_value(bundle, "report_id")
        if not isinstance(report_id, str) or not report_id:
            raise ValueError("review page bundle report_id is required")
        if not isinstance(archive_state, Mapping) or archive_state.get('report_id') != report_id:
            raise ValueError("review page bundle archive_state report_id must match")
        return bundle

    def _now(self) -> float:
        return float(self.clock())

    @staticmethod
    def _json(payload: dict[str, Any]) -> tuple[int, bytes, str]:
        return 202, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), JSON_CONTENT_TYPE

    def _building_response_locked(self, now: float) -> tuple[int, bytes, str]:
        started = self._started_at if self._started_at is not None else now
        elapsed = max(0.0, now - started)
        return self._json({
            "status": "building",
            "phase": self._phase,
            "elapsed_seconds": round(elapsed, 3),
            "retry_after": 0,
        })

    def _error_response_locked(self, now: float) -> tuple[int, bytes, str]:
        failure = self._failure or {}
        retry_after = max(0.0, self.retry_delay - max(0.0, now - float(failure.get("at", now))))
        return 409, json.dumps({
            "status": "error",
            "error": str(failure.get("error", "review page build failed")),
            "retry_after": round(retry_after, 3),
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), JSON_CONTENT_TYPE

    def _start_locked(self, key: Any, now: float) -> tuple[int, bytes, str]:
        self._generation += 1
        generation = self._generation
        self._state = "building"
        self._phase = "读取当前素材清单"
        self._started_at = now
        self._building_key = key
        self._failure = None
        worker = threading.Thread(
            target=self._run,
            args=(generation, key),
            name="review-page-cache",
            daemon=True,
        )
        self._worker = worker
        try:
            worker.start()
        except BaseException as exc:
            self._worker = None
            self._state = "failed"
            self._failure = {"key": key, "at": now, "error": str(exc)}
            return self._error_response_locked(now)
        return self._building_response_locked(now)

    def _run(self, generation: int, before_key: Any) -> None:
        def set_phase(phase: str) -> None:
            with self._lock:
                if self._closed or generation != self._generation or self._state != "building":
                    return
                self._phase = str(phase)

        try:
            bundle = self._validate_bundle(self.builder(set_phase))
            after_key = self.signature()
        except BaseException as exc:
            now = self._now()
            with self._lock:
                if generation != self._generation:
                    return
                self._worker = None
                if self._closed:
                    self._state = "closed"
                    return
                self._state = "failed"
                self._failure = {"key": before_key, "at": now, "error": str(exc)}
            return

        now = self._now()
        with self._lock:
            if generation != self._generation:
                return
            self._worker = None
            if self._closed:
                self._state = "closed"
                return
            if self._invalidate_after_build or not self._same(before_key, after_key):
                self._state = "invalidated"
                self._phase = "素材来源已变化，等待重新构建"
                self._failure = None
                self._building_key = None
                self._invalidate_after_build = False
                return
            self._ready = {"key": before_key, "built_at": now, "bundle": bundle,
                           "generation": generation, "verified_at": time.time()}
            self._state = "ready"
            self._phase = "页面已准备"
            self._building_key = None
            self._failure = None
            self._invalidate_after_build = False

    def read(self) -> tuple[int, bytes, str]:
        """Return the page or a bounded JSON state response."""
        try:
            key = self.signature()
        except BaseException as exc:
            return 409, json.dumps({"status": "error", "error": str(exc), "retry_after": 0}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), JSON_CONTENT_TYPE
        now = self._now()
        with self._lock:
            if self._closed:
                return 409, json.dumps({"status": "error", "error": "review page cache is closed", "retry_after": 0}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), JSON_CONTENT_TYPE
            ready = self._ready
            if ready is not None and self._same(ready["key"], key) and (self.ttl is None or now - ready["built_at"] <= self.ttl):
                bundle = ready["bundle"]
                html = self._bundle_value(bundle, "html_bytes")
                if html is None:
                    html = self._bundle_value(bundle, "html")
                return 200, bytes(html), HTML_CONTENT_TYPE
            if (ready is not None and self.ttl is not None and self.display_grace > 0
                    and self._same(ready['key'], key)
                    and now - ready['built_at'] <= self.ttl + self.display_grace):
                running = self._worker is not None and self._worker.is_alive()
                failure = self._failure
                backoff = (failure is not None and self._same(failure.get('key'), key)
                           and now - failure['at'] < self.retry_delay)
                if not running and not backoff:
                    self._start_locked(key, now)
                html = self._bundle_value(ready['bundle'], 'html_bytes')
                if html is None:
                    html = self._bundle_value(ready['bundle'], 'html')
                return 200, display_html(bytes(html), ready['generation'], ready['verified_at']), HTML_CONTENT_TYPE
            if self._state == "building" and self._worker is not None and self._worker.is_alive():
                return self._building_response_locked(now)
            failure = self._failure
            if failure is not None and self._same(failure.get("key"), key) and now - float(failure["at"]) < self.retry_delay:
                return self._error_response_locked(now)
            return self._start_locked(key, now)

    def warm(self, can_refresh=lambda: True, interval=2) -> None:
        """Prebuild and refresh in the background while archive work is idle.

        Only one maintenance thread and one builder are allowed. No timer
        publishes stale data or performs any archive operation.
        """
        if interval <= 0:
            raise ValueError('refresh interval must be positive')
        def maintain():
            while not self._maintenance_stop.is_set():
                try:
                    key, now = self.signature(), self._now()
                    with self._lock:
                        ready = self._ready
                        due = (ready is None or not self._same(ready['key'], key)
                               or (self.ttl is not None and now - ready['built_at'] > self.ttl))
                        running = self._worker is not None and self._worker.is_alive()
                        failure = self._failure
                        backoff = (failure is not None and self._same(failure.get('key'), key)
                                   and now - failure['at'] < self.retry_delay)
                    if due and not running and not backoff and can_refresh():
                        with self._lock:
                            current = self._ready
                            still_due = (current is None or not self._same(current['key'], key)
                                         or (self.ttl is not None and self._now() - current['built_at'] > self.ttl))
                            if still_due and not self._closed and not (self._worker and self._worker.is_alive()):
                                self._start_locked(key, self._now())
                except Exception:
                    pass  # Normal reads still expose preparation failures.
                self._maintenance_stop.wait(interval)
        with self._lock:
            if self._closed or self._maintenance is not None:
                return
            self._maintenance = threading.Thread(target=maintain, name='review-page-refresh', daemon=True)
            self._maintenance.start()

    def status(self) -> dict[str, Any]:
        """Cheap polling; never rescan history or serialize the page."""
        try:
            key = self.signature()
        except Exception:
            return {'status': 'failed', 'requires_reload': True, 'ready_generation': 0}
        with self._lock:
            ready = self._ready
            valid = ready is not None and self._same(ready['key'], key)
            return {'status': self._state, 'requires_reload': ready is not None and not valid,
                    'ready_generation': ready['generation'] if valid else 0}

    def get_display_bundle(self) -> Any | None:
        """History pagination belongs to the same labelled display snapshot."""
        try:
            key = self.signature()
        except Exception:
            return None
        with self._lock:
            ready = self._ready
            if (self._closed or ready is None or not self._same(ready['key'], key)
                    or (self.ttl is not None and self._now() - ready['built_at'] > self.ttl + self.display_grace)):
                return None
            return ready['bundle']

    def get_bundle(self, report_id: str | None = None) -> Any | None:
        """Return the current ready bundle, never an expired/stale bundle."""
        try:
            key = self.signature()
        except BaseException:
            return None
        now = self._now()
        with self._lock:
            if self._closed or self._ready is None:
                return None
            ready = self._ready
            if (self.ttl is not None and now - ready["built_at"] > self.ttl) or not self._same(ready["key"], key):
                return None
            bundle = ready["bundle"]
            if report_id is not None and self._bundle_value(bundle, "report_id") != report_id:
                return None
            return bundle

    def invalidate(self) -> None:
        """Discard a ready result and require a later read to rebuild.

        An active builder is allowed to finish, but its result is discarded so
        a successful external write cannot expose a page built beforehand.
        """
        with self._lock:
            if self._closed:
                return
            self._ready = None
            self._failure = None
            if self._state == "building" and self._worker is not None and self._worker.is_alive():
                self._invalidate_after_build = True
                return
            self._state = "invalidated"
            self._phase = "等待页面重新构建"
            self._invalidate_after_build = False

    def shutdown(self) -> None:
        """Prevent future publication without joining a possibly blocked builder."""
        with self._lock:
            self._closed = True
            self._maintenance_stop.set()
            self._state = "closed"
            self._generation += 1
            self._worker = None
            self._ready = None
            self._invalidate_after_build = False


__all__ = ["HTML_CONTENT_TYPE", "JSON_CONTENT_TYPE", "ReviewPageCache"]
