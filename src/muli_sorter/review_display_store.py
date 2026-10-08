"""Durable, presentation-only storage for completed review displays.

The store contains derived display data only.  A ready pointer is the commit
record for one key; the files below it are immutable, content-addressed
snapshots.  Archive history is kept in small JSON pages and is read lazily so
loading a display does not load the complete history into memory.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
from typing import Any


MAX_HTML_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_ROWS = 200_000
PAGE_ROWS = 50
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PAGE_NAME = re.compile(r"^[0-9]{8}\.json$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("display cache value is not JSON serializable") from exc


def _json_load(data: bytes) -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError("non-finite JSON number")

    return json.loads(data.decode("utf-8"), parse_constant=reject_constant)


def _value(bundle: Any, name: str, default: Any = None) -> Any:
    if isinstance(bundle, Mapping):
        return bundle.get(name, default)
    return getattr(bundle, name, default)


def _normalize_key(value: Any) -> Any:
    """Return the JSON-normalized form used for stable key identity."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("display cache key contains a non-finite number")
        return value
    if isinstance(value, (tuple, list)):
        return [_normalize_key(item) for item in value]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("display cache key mapping keys must be strings")
            result[key] = _normalize_key(item)
        return result
    raise ValueError("display cache key must contain only JSON values")


def _key_bytes(key: Any) -> bytes:
    return _json_bytes(_normalize_key(key))


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_regular_file(path: Path) -> bool:
    info = _lstat(path)
    return info is not None and stat.S_ISREG(info.st_mode)


def _is_directory(path: Path) -> bool:
    info = _lstat(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def _has_symlink(path: Path) -> bool:
    """Check this cache component without treating normal OS aliases as unsafe."""
    info = _lstat(path)
    return info is not None and stat.S_ISLNK(info.st_mode)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_regular_bytes(path: Path, limit: int, expected_size: int | None = None) -> bytes | None:
    """Read one cache file through a descriptor, refusing symlink targets."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        size = info.st_size if expected_size is None else expected_size
        if size < 0 or size > limit or info.st_size != size:
            return None
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.fstat(fd).st_size != size:
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


class _LazyArchiveUnits(Sequence):
    """A bounded sequence backed by separately hashed JSON history pages."""

    def __init__(self, snapshot: Path, pages: list[dict[str, Any]], total: int) -> None:
        self._snapshot = snapshot
        self._pages = tuple(pages)
        self._total = total
        self._lock = threading.RLock()
        self._cached_index: int | None = None
        self._cached_rows: tuple[Any, ...] | None = None

    def __len__(self) -> int:
        return self._total

    def _page(self, index: int) -> tuple[Any, ...]:
        with self._lock:
            if self._cached_index == index and self._cached_rows is not None:
                return self._cached_rows
            if not 0 <= index < len(self._pages):
                raise IndexError(index)
            descriptor = self._pages[index]
            filename = descriptor.get("path")
            if not isinstance(filename, str) or not _PAGE_NAME.fullmatch(Path(filename).name) or Path(filename).parent != Path("pages"):
                raise ValueError("display cache history page path is invalid")
            pages_dir = self._snapshot / "pages"
            if _has_symlink(pages_dir) or not _is_directory(pages_dir):
                raise ValueError("display cache history pages directory is missing")
            page_path = self._snapshot / filename
            if _has_symlink(page_path) or not _is_regular_file(page_path):
                raise ValueError("display cache history page is missing")
            info = _lstat(page_path)
            assert info is not None
            size = descriptor.get("size")
            digest = descriptor.get("sha256")
            if not isinstance(size, int) or size < 0 or size > MAX_PAGE_BYTES or info.st_size != size:
                raise ValueError("display cache history page size is invalid")
            if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
                raise ValueError("display cache history page digest is invalid")
            data = _read_regular_bytes(page_path, MAX_PAGE_BYTES, size)
            if data is None or len(data) != size or _sha256(data) != digest:
                raise ValueError("display cache history page integrity check failed")
            try:
                payload = _json_load(data)
            except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("display cache history page is invalid JSON") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("archived_units"), list):
                raise ValueError("display cache history page shape is invalid")
            rows = payload["archived_units"]
            expected = descriptor.get("count")
            if not isinstance(expected, int) or expected != len(rows) or len(rows) > PAGE_ROWS:
                raise ValueError("display cache history page count is invalid")
            rows_tuple = tuple(rows)
            self._cached_index = index
            self._cached_rows = rows_tuple
            return rows_tuple

    def __getitem__(self, item: int | slice) -> Any:
        if isinstance(item, slice):
            start, stop, step = item.indices(self._total)
            if step < 0:
                if start <= stop:
                    return []
                return [self[index] for index in range(start, stop, step)]
            if start >= stop:
                return []
            result = []
            page_index = start // PAGE_ROWS
            last_page = (stop - 1) // PAGE_ROWS
            for page in range(page_index, last_page + 1):
                rows = self._page(page)
                base = page * PAGE_ROWS
                first = max(start - base, 0)
                last = min(stop - base, len(rows))
                result.extend(rows[first:last])
            if step != 1:
                return result[::step]
            return result
        if not isinstance(item, int):
            raise TypeError("archive history index must be an integer or slice")
        index = item + self._total if item < 0 else item
        if not 0 <= index < self._total:
            raise IndexError(item)
        rows = self._page(index // PAGE_ROWS)
        return rows[index % PAGE_ROWS]


class DisplayStore:
    """Persist and reload one presentation bundle per stable display key."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def _prepare_root(self) -> None:
        if _has_symlink(self.root):
            raise ValueError("display cache root contains a symlink")
        existing = _lstat(self.root)
        if existing is not None and not stat.S_ISDIR(existing.st_mode):
            raise ValueError("display cache root is not a directory")
        self.root.mkdir(parents=True, exist_ok=True)
        if not _is_directory(self.root) or _has_symlink(self.root):
            raise ValueError("display cache root is not a safe directory")

    @staticmethod
    def _validate_input(bundle: Any) -> tuple[bytes, str, dict[str, Any], str, dict[str, Any]]:
        html = _value(bundle, "html_bytes")
        if isinstance(html, bytearray):
            html = bytes(html)
        if not isinstance(html, bytes):
            raise ValueError("display bundle html_bytes must be bytes")
        if len(html) > MAX_HTML_BYTES:
            raise ValueError("display bundle HTML is too large")
        report_id = _value(bundle, "report_id")
        if not isinstance(report_id, str) or not report_id:
            raise ValueError("display bundle report_id is required")
        history_generation = _value(bundle, "history_generation")
        if not isinstance(history_generation, str) or not history_generation:
            raise ValueError("display bundle history_generation is required")
        archive_state = _value(bundle, "archive_state")
        if not isinstance(archive_state, Mapping):
            raise ValueError("display bundle archive_state is required")
        if archive_state.get("report_id") != report_id:
            raise ValueError("display bundle archive_state report_id must match")
        entries = archive_state.get("archived_units")
        if isinstance(entries, (str, bytes, bytearray)) or not isinstance(entries, Sequence):
            raise ValueError("display bundle archived_units must be a sequence")
        if len(entries) > MAX_ARCHIVE_ROWS:
            raise ValueError("display bundle archive history is too large")
        state_meta = dict(archive_state)
        state_meta.pop("archived_units", None)
        summary = _value(bundle, "archive_summary", {})
        if summary is None:
            summary = {}
        if not isinstance(summary, Mapping):
            raise ValueError("display bundle archive_summary must be a mapping")
        summary = dict(summary)
        if "report_id" in summary and summary["report_id"] != report_id:
            raise ValueError("display bundle archive_summary report_id must match")
        return html, report_id, state_meta, history_generation, summary

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    @classmethod
    def _write_json(cls, path: Path, value: Any, limit: int | None = None) -> bytes:
        data = _json_bytes(value)
        if limit is not None and len(data) > limit:
            raise ValueError("display cache JSON blob is too large")
        cls._write_bytes(path, data)
        return data

    @classmethod
    def _atomic_ready(cls, path: Path, payload: bytes) -> None:
        fd, raw = tempfile.mkstemp(prefix=".ready-", dir=path.parent)
        temp = Path(raw)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            _fsync_directory(path.parent)
        finally:
            if _lstat(temp) is not None:
                try:
                    temp.unlink()
                except OSError:
                    pass

    def save(self, key: Any, bundle: Any) -> dict[str, Any]:
        """Atomically publish a validated display bundle for ``key``."""
        html, report_id, state_meta, history_generation, summary = self._validate_input(bundle)
        key_data = _key_bytes(key)
        key_digest = _sha256(key_data)
        self._prepare_root()

        stage = Path(tempfile.mkdtemp(prefix=".display-stage-", dir=self.root))
        snapshot: Path | None = None
        try:
            pages_dir = stage / "pages"
            pages_dir.mkdir()
            html_path = stage / "html.bin"
            metadata_path = stage / "metadata.json"
            summary_path = stage / "archive_summary.json"
            self._write_bytes(html_path, html)
            metadata = {
                "report_id": report_id,
                "history_generation": history_generation,
                "archive_state": state_meta,
                "photo_index": _value(bundle, "photo_index"),
                "video_scope": _value(bundle, "video_scope"),
            }
            metadata_data = self._write_json(metadata_path, metadata, MAX_METADATA_BYTES)
            summary_data = self._write_json(summary_path, summary, MAX_METADATA_BYTES)

            entries = _value(bundle, "archive_state").get("archived_units")
            page_descriptors: list[dict[str, Any]] = []
            total = len(entries)
            for page_index, start in enumerate(range(0, total, PAGE_ROWS)):
                rows = list(entries[start : start + PAGE_ROWS])
                page_data = _json_bytes({"archived_units": rows})
                if len(page_data) > MAX_PAGE_BYTES:
                    raise ValueError("display cache history page is too large")
                filename = f"pages/{page_index:08d}.json"
                page_path = stage / filename
                self._write_bytes(page_path, page_data)
                page_descriptors.append({
                    "path": filename,
                    "size": len(page_data),
                    "sha256": _sha256(page_data),
                    "count": len(rows),
                })

            blobs = {
                "html": {"path": "html.bin", "size": len(html), "sha256": _sha256(html)},
                "metadata": {"path": "metadata.json", "size": len(metadata_data), "sha256": _sha256(metadata_data)},
                "archive_summary": {"path": "archive_summary.json", "size": len(summary_data), "sha256": _sha256(summary_data)},
                "history_pages": page_descriptors,
            }
            manifest = {
                "version": 1,
                "key_sha256": key_digest,
                "report_id": report_id,
                "history_generation": history_generation,
                "history_total": total,
                "blobs": blobs,
            }
            manifest_data = self._write_json(stage / "manifest.json", manifest, MAX_METADATA_BYTES)
            snapshot_digest = _sha256(manifest_data)

            key_dir = self.root / key_digest
            if _has_symlink(key_dir):
                raise ValueError("display cache key path contains a symlink")
            if not key_dir.exists():
                key_dir.mkdir()
            if not _is_directory(key_dir):
                raise ValueError("display cache key path is not a directory")
            snapshots_dir = key_dir / "snapshots"
            if _has_symlink(snapshots_dir):
                raise ValueError("display cache snapshot path contains a symlink")
            snapshots_dir.mkdir(exist_ok=True)
            if not _is_directory(snapshots_dir):
                raise ValueError("display cache snapshots path is not a directory")
            final_snapshot = snapshots_dir / snapshot_digest
            if _has_symlink(final_snapshot):
                raise ValueError("display cache snapshot is a symlink")
            if _lstat(final_snapshot) is None:
                os.replace(stage, final_snapshot)
                _fsync_directory(snapshots_dir)
                snapshot = final_snapshot
            elif _is_directory(final_snapshot):
                snapshot = final_snapshot
            else:
                raise ValueError("display cache snapshot path is not a directory")

            ready = {
                "version": 1,
                "key_sha256": key_digest,
                "snapshot": snapshot_digest,
                "manifest_sha256": _sha256(manifest_data),
            }
            if _has_symlink(key_dir / "ready.json"):
                raise ValueError("display cache ready record is a symlink")
            self._atomic_ready(key_dir / "ready.json", _json_bytes(ready))
            # Keep only the compact state and a lazy page descriptor in the
            # returned object.  The caller's original list remains theirs.
            lazy = _LazyArchiveUnits(snapshot, page_descriptors, total)
            returned_state = dict(state_meta)
            returned_state["report_id"] = report_id
            returned_state["archived_units"] = lazy
            return {
                "html_bytes": bytes(html),
                "report_id": report_id,
                "archive_state": returned_state,
                "history_generation": history_generation,
                "archive_summary": dict(summary),
                "photo_index": _value(bundle, "photo_index"),
                "video_scope": _value(bundle, "video_scope"),
            }
        finally:
            if _lstat(stage) is not None:
                shutil.rmtree(stage, ignore_errors=True)

    @staticmethod
    def _read_blob(snapshot: Path, descriptor: Any, limit: int) -> bytes | None:
        if not isinstance(descriptor, Mapping):
            return None
        filename = descriptor.get("path")
        size = descriptor.get("size")
        digest = descriptor.get("sha256")
        if not isinstance(filename, str) or Path(filename).name != filename or Path(filename).parent != Path("."):
            return None
        if not isinstance(size, int) or size < 0 or size > limit or not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            return None
        path = snapshot / filename
        if _has_symlink(path) or not _is_regular_file(path):
            return None
        info = _lstat(path)
        assert info is not None
        if info.st_size != size:
            return None
        data = _read_regular_bytes(path, limit, size)
        if data is None:
            return None
        if len(data) != size or _sha256(data) != digest:
            return None
        return data

    def load(self, key: Any) -> dict[str, Any] | None:
        """Return a verified display, or ``None`` for an invalid main record."""
        try:
            key_digest = _sha256(_key_bytes(key))
        except (TypeError, ValueError):
            return None
        if _has_symlink(self.root) or not _is_directory(self.root):
            return None
        key_dir = self.root / key_digest
        if _has_symlink(key_dir) or not _is_directory(key_dir):
            return None
        ready_path = key_dir / "ready.json"
        if _has_symlink(ready_path) or not _is_regular_file(ready_path):
            return None
        ready_data = _read_regular_bytes(ready_path, MAX_METADATA_BYTES)
        if ready_data is None:
            return None
        try:
            ready = _json_load(ready_data)
        except (UnicodeError, ValueError, json.JSONDecodeError):
            return None
        if (not isinstance(ready, Mapping) or ready.get("version") != 1
                or ready.get("key_sha256") != key_digest):
            return None
        snapshot_digest = ready.get("snapshot")
        if not isinstance(snapshot_digest, str) or not _HEX64.fullmatch(snapshot_digest):
            return None
        if ready.get("manifest_sha256") != snapshot_digest:
            return None
        snapshots_dir = key_dir / "snapshots"
        snapshot = snapshots_dir / snapshot_digest
        if (_has_symlink(snapshots_dir) or not _is_directory(snapshots_dir)
                or _has_symlink(snapshot) or not _is_directory(snapshot)):
            return None
        manifest_path = snapshot / "manifest.json"
        if _has_symlink(manifest_path) or not _is_regular_file(manifest_path):
            return None
        manifest_data = _read_regular_bytes(manifest_path, MAX_METADATA_BYTES)
        if manifest_data is None:
            return None
        try:
            manifest = _json_load(manifest_data)
        except (UnicodeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, Mapping) or _sha256(manifest_data) != snapshot_digest:
            return None
        if manifest.get("key_sha256") != key_digest or manifest.get("version") != 1:
            return None
        blobs = manifest.get("blobs")
        if not isinstance(blobs, Mapping):
            return None
        html_data = self._read_blob(snapshot, blobs.get("html"), MAX_HTML_BYTES)
        metadata_data = self._read_blob(snapshot, blobs.get("metadata"), MAX_METADATA_BYTES)
        summary_data = self._read_blob(snapshot, blobs.get("archive_summary"), MAX_METADATA_BYTES)
        # Summary was added after the first cache format.  A missing summary
        # remains readable as an empty mapping for those legacy snapshots.
        if html_data is None or metadata_data is None:
            return None
        try:
            metadata = _json_load(metadata_data)
            summary = {} if summary_data is None and "archive_summary" not in blobs else _json_load(summary_data or b"")
        except (UnicodeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(metadata, Mapping) or not isinstance(summary, Mapping):
            return None
        report_id = metadata.get("report_id")
        history_generation = metadata.get("history_generation")
        state = metadata.get("archive_state")
        if (not isinstance(report_id, str) or not report_id
                or not isinstance(history_generation, str) or not history_generation
                or not isinstance(state, Mapping) or "archived_units" in state
                or state.get("report_id") != report_id):
            return None
        if manifest.get("report_id") != report_id or manifest.get("history_generation") != history_generation:
            return None
        if "report_id" in summary and summary.get("report_id") != report_id:
            return None
        total = manifest.get("history_total")
        page_descriptors = blobs.get("history_pages")
        if not isinstance(total, int) or not 0 <= total <= MAX_ARCHIVE_ROWS or not isinstance(page_descriptors, list):
            return None
        expected_pages = (total + PAGE_ROWS - 1) // PAGE_ROWS
        if len(page_descriptors) != expected_pages:
            return None
        for index, descriptor in enumerate(page_descriptors):
            if not isinstance(descriptor, Mapping) or descriptor.get("path") != f"pages/{index:08d}.json":
                return None
            count = descriptor.get("count")
            if not isinstance(count, int) or not 0 <= count <= PAGE_ROWS:
                return None
            if index < len(page_descriptors) - 1 and count != PAGE_ROWS:
                return None
        if sum(descriptor.get("count", -1) for descriptor in page_descriptors) != total:
            return None
        returned_state = dict(state)
        returned_state["report_id"] = report_id
        returned_state["archived_units"] = _LazyArchiveUnits(snapshot, [dict(item) for item in page_descriptors], total)
        return {
            "html_bytes": html_data,
            "report_id": report_id,
            "archive_state": returned_state,
            "history_generation": history_generation,
            "archive_summary": dict(summary),
            "photo_index": metadata.get("photo_index"),
            "video_scope": metadata.get("video_scope"),
        }


__all__ = ["DisplayStore", "MAX_HTML_BYTES", "MAX_METADATA_BYTES", "MAX_PAGE_BYTES", "MAX_ARCHIVE_ROWS", "PAGE_ROWS"]
