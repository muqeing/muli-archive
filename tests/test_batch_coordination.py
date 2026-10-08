"""Cross-process tests for the v1/v2 staging coordination contract.

These tests intentionally use only temporary local directories.  They exercise
the public coordination API from independent Python processes and keep the
frozen v1 fixture as the compatibility reader for an activated v2 staging
root.  The production implementation is expected to provide the v2 symbols;
the test is allowed to fail until that implementation lands.
"""

from contextlib import contextmanager
from pathlib import Path
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from muli_sorter.staging_coordination import (
    BATCH_LOCK_DIR,
    BATCH_PROTOCOL,
    NAME,
    PROTOCOL,
    activate_batch_protocol,
    batch_guard,
    staging_guard,
)


SORTER_SRC = Path(__file__).resolve().parents[1] / "src"
V1_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "coordination" / "staging_coordination.py"
BATCH_A = "BATCH_20261004_000001"
BATCH_B = "BATCH_20261004_000002"


def _child_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SORTER_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _child(code, *args):
    return subprocess.Popen(
        [sys.executable, "-c", code, *map(str, args)],
        cwd=str(SORTER_SRC.parent.parent),
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _wait_ready(process, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([process.stdout], [], [], min(0.1, remaining))
        line = process.stdout.readline() if readable else ""
        if line:
            if line.strip() == "LOCKED":
                return
            raise AssertionError(f"coordination child failed before ready: {line.strip()!r}")
        if process.poll() is not None:
            stderr = process.stderr.read()
            raise AssertionError(f"coordination child exited before ready: {stderr!r}")
        time.sleep(0.01)
    raise AssertionError("coordination child did not acquire its lock")


def _stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    if process.stdout:
        process.stdout.close()
    if process.stderr:
        process.stderr.close()


@contextmanager
def _v1_staging(*batch_ids):
    with tempfile.TemporaryDirectory(prefix="muli-batch-coordination-") as root:
        staging = Path(root)
        for batch_id in batch_ids:
            (staging / batch_id).mkdir()
        with staging_guard(staging, create=True):
            pass
        yield staging


@contextmanager
def _v2_staging(*batch_ids):
    with _v1_staging(*batch_ids) as staging:
        activate_batch_protocol(staging, list(batch_ids))
        yield staging


class BatchCoordinationTests(unittest.TestCase):
    def test_activate_requires_existing_real_batches_and_idle_root(self):
        with _v1_staging(BATCH_A) as staging:
            with self.assertRaises((OSError, ValueError)):
                activate_batch_protocol(staging, [BATCH_A, BATCH_B])
            self.assertEqual((staging / NAME).read_bytes(), PROTOCOL)
            self.assertFalse((staging / BATCH_LOCK_DIR).exists())

            holder = _child(
                """
from muli_sorter.staging_coordination import staging_guard
import sys, time
with staging_guard(sys.argv[1]):
    print('LOCKED', flush=True)
    time.sleep(30)
""",
                staging,
            )
            try:
                _wait_ready(holder)
                with self.assertRaises((OSError, ValueError)):
                    activate_batch_protocol(staging, [BATCH_A])
            finally:
                _stop(holder)

            activate_batch_protocol(staging, [BATCH_A])
            self.assertEqual((staging / NAME).read_bytes(), BATCH_PROTOCOL)
            self.assertEqual((staging / BATCH_LOCK_DIR / (BATCH_A + ".lock")).read_bytes(), BATCH_PROTOCOL)

    def test_different_batches_shared_backup_and_exclusive_archive_can_parallel(self):
        with _v2_staging(BATCH_A, BATCH_B) as staging:
            holder = _child(
                """
from muli_sorter.staging_coordination import batch_guard
import sys, time
with batch_guard(sys.argv[1], [sys.argv[2]], create=False):
    print('LOCKED', flush=True)
    time.sleep(30)
""",
                staging,
                BATCH_A,
            )
            try:
                _wait_ready(holder)
                with batch_guard(staging, [BATCH_B], exclusive=True, wait=False) as v2:
                    self.assertIs(v2, True)
            finally:
                _stop(holder)

    def test_same_batch_is_mutually_exclusive(self):
        with _v2_staging(BATCH_A) as staging:
            holder = _child(
                """
from muli_sorter.staging_coordination import batch_guard
import sys, time
with batch_guard(sys.argv[1], [sys.argv[2]], exclusive=True, create=False):
    print('LOCKED', flush=True)
    time.sleep(30)
""",
                staging,
                BATCH_A,
            )
            try:
                _wait_ready(holder)
                with self.assertRaises(ValueError):
                    with batch_guard(staging, [BATCH_A], wait=False):
                        self.fail("same-batch shared lock entered during exclusive lock")
                with self.assertRaises(BlockingIOError):
                    with batch_guard(staging, [BATCH_A], wait=False, readonly=True):
                        self.fail("readonly same-batch probe entered during exclusive lock")
            finally:
                _stop(holder)

    def test_global_exclusive_blocks_all_v2_batch_locks(self):
        with _v2_staging(BATCH_A, BATCH_B) as staging:
            holder = _child(
                """
from muli_sorter.staging_coordination import staging_guard
import sys, time
with staging_guard(sys.argv[1], exclusive=True, create=False):
    print('LOCKED', flush=True)
    time.sleep(30)
""",
                staging,
            )
            try:
                _wait_ready(holder)
                for batch_id in (BATCH_A, BATCH_B):
                    with self.subTest(batch_id=batch_id):
                        with self.assertRaises(ValueError):
                            with batch_guard(staging, [batch_id], wait=False):
                                self.fail("batch lock entered while global exclusive lock was held")
            finally:
                _stop(holder)

    def test_wait_cancel_and_process_exit_release_locks(self):
        with _v2_staging(BATCH_A) as staging:
            holder = _child(
                """
from muli_sorter.staging_coordination import batch_guard
import sys, time
with batch_guard(sys.argv[1], [sys.argv[2]], exclusive=True, create=False):
    print('LOCKED', flush=True)
    time.sleep(30)
""",
                staging,
                BATCH_A,
            )
            try:
                _wait_ready(holder)
                cancel = threading.Event()
                outcome = []

                def wait_for_lock():
                    try:
                        with batch_guard(staging, [BATCH_A], wait=True, cancel=cancel):
                            outcome.append("entered")
                    except ValueError as exc:
                        outcome.append(str(exc))

                waiter = threading.Thread(target=wait_for_lock)
                waiter.start()
                time.sleep(0.15)
                cancel.set()
                waiter.join(timeout=2)
                self.assertFalse(waiter.is_alive())
                self.assertEqual(len(outcome), 1)
                self.assertNotEqual(outcome[0], "entered")
            finally:
                _stop(holder)

            with batch_guard(staging, [BATCH_A], exclusive=True, wait=False) as v2:
                self.assertIs(v2, True)

    def test_v1_fallback_is_global_and_waits(self):
        with _v1_staging(BATCH_A, BATCH_B) as staging:
            holder = _child(
                """
from muli_sorter.staging_coordination import staging_guard
import sys, time
with staging_guard(sys.argv[1]):
    print('LOCKED', flush=True)
    time.sleep(30)
""",
                staging,
            )
            try:
                _wait_ready(holder)
                with self.assertRaises(ValueError):
                    with batch_guard(staging, [BATCH_A], exclusive=True, wait=False):
                        self.fail("v1 fallback ignored the global shared lock")
                with self.assertRaises(BlockingIOError):
                    with batch_guard(staging, [BATCH_B], exclusive=True, wait=False, readonly=True):
                        self.fail("readonly v1 probe ignored the global shared lock")
                entered = []

                def wait_for_global():
                    with batch_guard(staging, [BATCH_B], exclusive=True, wait=True) as v2:
                        entered.append(v2)

                waiter = threading.Thread(target=wait_for_global)
                waiter.start()
                time.sleep(0.15)
                self.assertTrue(waiter.is_alive())
            finally:
                _stop(holder)
            waiter.join(timeout=2)
            self.assertFalse(waiter.is_alive())
            self.assertEqual(entered, [False])

    def test_frozen_v1_reader_rejects_activated_v2(self):
        with _v2_staging(BATCH_A) as staging:
            code = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('frozen_v1', sys.argv[2])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    with module.staging_guard(sys.argv[1], exclusive=True):
        print('ACQUIRED', flush=True)
except ValueError:
    print('REJECTED', flush=True)
"""
            result = subprocess.run(
                [sys.executable, "-c", code, str(staging), str(V1_FIXTURE)],
                cwd=str(SORTER_SRC.parent.parent),
                env=_child_env(),
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), "REJECTED")

    def test_unknown_missing_symlink_and_hardlink_batch_locks_fail_closed(self):
        with _v2_staging(BATCH_A) as staging:
            unknown_id = "BATCH_20261004_999999"
            unknown = staging / BATCH_LOCK_DIR / (unknown_id + ".lock")
            unknown.write_bytes(BATCH_PROTOCOL)
            with self.assertRaises((FileNotFoundError, OSError, ValueError)):
                with batch_guard(staging, [unknown_id], wait=False):
                    self.fail("unknown batch lock entered")

        with _v2_staging(BATCH_A) as staging:
            lock = staging / BATCH_LOCK_DIR / (BATCH_A + ".lock")
            lock.unlink()
            with self.assertRaises((FileNotFoundError, OSError, ValueError)):
                with batch_guard(staging, [BATCH_A], wait=False):
                    self.fail("missing batch lock entered")

        with _v2_staging(BATCH_A) as staging:
            lock = staging / BATCH_LOCK_DIR / (BATCH_A + ".lock")
            lock.unlink()
            unknown = staging / "unknown-lock"
            unknown.write_bytes(b"unknown protocol\n")
            lock.symlink_to(unknown)
            with self.assertRaises((OSError, ValueError)):
                with batch_guard(staging, [BATCH_A], wait=False):
                    self.fail("symlink batch lock entered")

        with _v2_staging(BATCH_A) as staging:
            lock = staging / BATCH_LOCK_DIR / (BATCH_A + ".lock")
            lock.unlink()
            real = staging / "real-lock"
            real.write_bytes(BATCH_PROTOCOL)
            os.link(real, lock)
            with self.assertRaises((OSError, ValueError)):
                with batch_guard(staging, [BATCH_A], wait=False):
                    self.fail("hard-linked batch lock entered")


if __name__ == "__main__":
    unittest.main()
