"""Boundary checks for the raised console state-file read cap."""
import json
import tempfile
import unittest
from pathlib import Path

from muli_sorter import order_feed_io


def tmp_root():
    # macOS returns /var/... which is a symlink; the no-follow reader needs the
    # resolved path.
    return Path(tempfile.mkdtemp(prefix="read-cap-")).resolve()


class ReadCap(unittest.TestCase):
    def test_cap_allows_a_real_sized_confirmation_model(self):
        with tempfile.TemporaryDirectory(dir=tmp_root()) as temp:
            path = Path(temp) / "确认模型.json"
            pad = "x" * 1024
            units = [{"unit_id": "unit-%06d" % i, "pad": pad} for i in range(50_000)]
            path.write_text(json.dumps({"units": units}, ensure_ascii=False), encoding="utf-8")
            size = path.stat().st_size
            self.assertGreater(size, 48 * 1024 * 1024)
            self.assertLess(size, order_feed_io.MAX_BYTES)
            self.assertEqual(len(order_feed_io.read_json(path)["units"]), 50_000)

    def test_oversized_file_is_still_refused(self):
        with tempfile.TemporaryDirectory(dir=tmp_root()) as temp:
            path = Path(temp) / "big.json"
            with path.open("wb") as handle:
                handle.truncate(order_feed_io.MAX_BYTES + 1)
            with self.assertRaises(ValueError):
                order_feed_io.read_json(path)

    def test_symlink_and_non_regular_paths_are_still_refused(self):
        with tempfile.TemporaryDirectory(dir=tmp_root()) as temp:
            root = Path(temp)
            target = root / "target.json"
            target.write_text('{"ok": true}', encoding="utf-8")
            link = root / "link.json"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symlinks unavailable")
            # O_NOFOLLOW surfaces the link itself as ELOOP on Linux/macOS.
            with self.assertRaises((OSError, ValueError)):
                order_feed_io.read_json(link)
            with self.assertRaises(ValueError):
                order_feed_io.read_json(root)

    def test_atomic_write_stays_bounded(self):
        with tempfile.TemporaryDirectory(dir=tmp_root()) as temp:
            path = Path(temp) / "state.json"
            order_feed_io.atomic_json(path, {"ok": True})
            self.assertEqual(order_feed_io.read_json(path), {"ok": True})
        original = order_feed_io.MAX_BYTES
        try:
            order_feed_io.MAX_BYTES = 64
            with tempfile.TemporaryDirectory(dir=tmp_root()) as temp:
                path = Path(temp) / "state.json"
                with self.assertRaises(ValueError):
                    order_feed_io.atomic_json(path, {"pad": "x" * 200})
        finally:
            order_feed_io.MAX_BYTES = original


if __name__ == "__main__":
    unittest.main()
