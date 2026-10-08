import ast
from copy import deepcopy
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from muli_sorter.staging_coordination import staging_guard,NAME

HERE=Path(__file__).resolve().parent/'fixtures/coordination'


def ingest_protocol_module():
    """Locate the Ingest copy of the shared lock protocol.

    The two services ship the same module verbatim. The Ingest tree lives in
    its own work directory, so accept the current layout and the historical one
    and skip when neither is checked out beside this repository.
    """
    root = Path(__file__).resolve().parents[2]
    for relative in ('ingest/src/muli_ingest/staging_coordination.py',
                     'ingest-source-release/src/muli_ingest/staging_coordination.py',
                     'metadata-batch-repair/baseline/muli_ingest/staging_coordination.py'):
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None

class CoordinationTests(unittest.TestCase):
    def test_protocol_modules_match_and_original_ingest_worker_body_unchanged(self):
        source=HERE
        sibling=ingest_protocol_module()
        if sibling is None:
            self.skipTest('Ingest 源码目录不在本工作区，跳过跨服务协议对照')
        self.assertEqual(sibling.read_bytes(),(Path(__file__).resolve().parents[1]/'src/muli_sorter/staging_coordination.py').read_bytes())
        baseline=HERE/'engine.before.py'
        old=ast.parse(baseline.read_text());new=ast.parse((source/'engine.py').read_text())
        a=next(x for x in ast.walk(old) if isinstance(x,ast.FunctionDef) and x.name=='_run_with_slot')
        b=next(x for x in ast.walk(new) if isinstance(x,ast.FunctionDef) and x.name=='_run_with_slot_coordinated')
        self.assertEqual(ast.dump(ast.Module(body=a.body,type_ignores=[])),ast.dump(ast.Module(body=b.body,type_ignores=[])))

    def test_ingest_wrapper_waits_for_cleanup_then_executes(self):
        source=HERE/'engine.py'
        tree=ast.parse(source.read_text());fn=next(x for x in ast.walk(tree) if isinstance(x,ast.FunctionDef) and x.name=='_run_with_slot')
        namespace={'staging_guard':staging_guard};exec(compile(ast.fix_missing_locations(ast.Module(body=[deepcopy(fn)],type_ignores=[])),'wrapper','exec'),namespace)
        with tempfile.TemporaryDirectory() as tmp:
            called=threading.Event()
            class Worker:
                staging=tmp
                cancels={'batch':threading.Event()}
                def _run_with_slot_coordinated(self,uid):called.set()
            worker=Worker()
            with staging_guard(tmp,create=True):pass
            with staging_guard(tmp,exclusive=True):
                t=threading.Thread(target=namespace['_run_with_slot'],args=(worker,'batch'));t.start()
                self.assertFalse(called.wait(.2));self.assertTrue(t.is_alive())
            t.join(2);self.assertTrue(called.is_set());self.assertFalse(t.is_alive())

    def test_real_other_process_backup_excludes_cleanup_and_exit_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            with staging_guard(tmp,create=True):pass
            code="""import sys,time
from muli_sorter.staging_coordination import staging_guard
with staging_guard(sys.argv[1]):
 print('LOCKED',flush=True)
 time.sleep(20)
"""
            p=subprocess.Popen([sys.executable,'-c',code,tmp],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertEqual(p.stdout.readline().strip(),'LOCKED')
                with self.assertRaises(ValueError):
                    with staging_guard(tmp,exclusive=True):pass
                with staging_guard(tmp):pass
            finally:
                p.terminate();p.wait(timeout=3);p.stdout.close();p.stderr.close()
            with staging_guard(tmp,exclusive=True):pass

    def test_symlink_missing_and_invalid_protocol_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/NAME
            with self.assertRaises(FileNotFoundError):
                with staging_guard(tmp,exclusive=True):pass
            p.write_text('unknown protocol')
            with self.assertRaises(ValueError):
                with staging_guard(tmp,exclusive=True):pass
            p.unlink();p.symlink_to('/tmp/not-an-archive-lock')
            with self.assertRaises(OSError):
                with staging_guard(tmp,create=True):pass

    def test_wait_can_be_cancelled_without_entering(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancel=threading.Event();cancel.set()
            with staging_guard(tmp,create=True):pass
            with self.assertRaises(ValueError):
                with staging_guard(tmp,wait=True,cancel=cancel):self.fail('cancelled worker entered')
