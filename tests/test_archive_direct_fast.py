from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from muli_sorter.archive_jobs import ArchiveJobs
from muli_sorter.archive_io import ArchiveError
from muli_sorter.review import digest
import test_archive_direct_move as direct_tests

spec = importlib.util.spec_from_file_location('source_fixture_bench', Path(__file__).resolve().parents[2] / 'benchmark-preflight-source.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class ReceiptReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'fixture'
        self.model, self.receipts = fixtures._prepare_fixture(self.root, 12, 'COPY_SIZE_VERIFIED')
        self.env = patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.receipts)})
        self.env.start()
        units = {u['unit_id']:u for u in self.model['units']}
        self.decisions = {'schema_version':'0.2', 'mode':'classification_confirmation_only',
            'media_write_authorized':False, 'report_id':self.model['report_id'],
            'created_at':datetime.now(timezone.utc).isoformat(),
            'segments':[{**s,'decision':'confirmed','project_id':units[s['unit_ids'][0]]['candidate_project_ids'][0]}
                        for s in self.model['initial_segments']],
            'archive_options':{'mode':'move','existing':'skip_identical'}}
        self.service = ArchiveJobs(self.root/'staging', self.root/'projects', self.root/'state-fast',
            lambda:self.model, lambda:json.loads((self.root/'runtime-state.json').read_text()),
            enabled=True, move_enabled=True,
            direct_move_view={'root':str(self.root),'staging':'staging','projects':'projects'})

    def tearDown(self):
        self.service.close()
        self.env.stop()
        self.tmp.cleanup()

    def submit(self):
        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview['status'], 'ready', preview)
        return self.service.submit(preview['preview_id'], self.decisions, True)

    def test_receipt_backed_unchanged_files_move_without_second_content_read(self):
        job = self.submit()
        before = {p: (self.root/'staging'/p).stat().st_ino for p in job['file_plans']}
        with patch('muli_sorter.archive_direct_move.hash_fd', side_effect=AssertionError('duplicate media read')), patch('muli_sorter.archive_direct_move.digest', wraps=digest) as digests:
            done = self.service.run_job(job['job_id'])
        # Hashing the complete multi-unit confirmation once per receipt makes
        # finalization quadratic for large batches, despite instant renames.
        decision_digests = [c for c in digests.call_args_list
                            if c.args[0] == self.decisions]
        self.assertEqual(len(decision_digests), 1)
        self.assertEqual(done['status'], 'completed', done)
        self.assertEqual(done['verification']['reused_files'], 12)
        self.assertEqual(done['verification']['hashed_files'], 0)
        self.assertEqual(done['summary']['direct_moved_files'], 12)
        for p, row in job['file_plans'].items():
            self.assertFalse((self.root/'staging'/p).exists())
            self.assertEqual((self.root/'projects'/row['target_path']).stat().st_ino, before[p])

    def test_v2_receipt_reuses_verified_content_and_keeps_inode_on_direct_move(self):
        from muli_sorter.postcopy_receipt import source_signature
        for path in self.receipts.glob('BATCH_*.json'):
            receipt=json.loads(path.read_text());receipt['schema']='postcopy-verification/2'
            for row in receipt['files']:
                fd=os.open(self.root/'staging'/row['resolved_path'],os.O_RDONLY)
                try:row['source_signature']=source_signature(fd,receipt['schema'])
                finally:os.close(fd)
            path.write_text(json.dumps(receipt))
        self.test_receipt_backed_unchanged_files_move_without_second_content_read()

    def test_same_size_edit_with_restored_mtime_cannot_reuse_receipt(self):
        job = self.submit()
        path = self.root/'staging'/next(iter(job['file_plans']))
        st = path.stat();path.write_bytes(b'x'*st.st_size);os.utime(path, ns=(st.st_atime_ns,st.st_mtime_ns))
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'failed', done)
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))

    def test_attribute_change_after_confirmation_rehashes_only_changed_file_and_moves(self):
        from muli_sorter.archive_direct_move import hash_fd
        job=self.submit();before={p:(self.root/'staging'/p).stat().st_ino for p in job['file_plans']}
        changed=next(iter(job['file_plans']));source=self.root/'staging'/changed
        original_digest=job['request_digest']
        os.chmod(source,0o640 if source.stat().st_mode & 0o777 != 0o640 else 0o600)
        with patch('muli_sorter.archive_direct_move.hash_fd',wraps=hash_fd) as reads:
            done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        self.assertEqual(reads.call_count,1)
        self.assertEqual(done['verification']['hashed_files'],1)
        self.assertEqual(done['verification']['reused_files'],11)
        self.assertEqual(done['verification']['checked_bytes'],done['verification']['total_bytes'])
        self.assertEqual(done['request_digest'],original_digest)
        self.assertEqual(done['summary']['direct_moved_files'],12)
        for p,row in job['file_plans'].items():
            self.assertFalse((self.root/'staging'/p).exists())
            self.assertEqual((self.root/'projects'/row['target_path']).stat().st_ino,before[p])

    def test_changed_source_after_prepare_blocks_first_rename(self):
        from muli_sorter.archive_direct_move import _checked
        job = self.submit();changed=[]
        def race(fd, name, row, *args, **kwargs):
            if kwargs.get('verified_source') is not None and not changed:
                p=self.root/'staging'/row['source_path'];p.write_bytes(b'x'*p.stat().st_size);changed.append(p)
            return _checked(fd,name,row,*args,**kwargs)
        with patch('muli_sorter.archive_direct_move._checked', side_effect=race):
            done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'failed', done)
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))

    def test_old_request_keeps_full_content_read(self):
        from muli_sorter.archive_direct_move import hash_fd
        job=self.submit();path=self.service.state/'requests'/('request-'+job['job_id']+'.json')
        request=json.loads(path.read_text());request.pop('verification_policy');request.pop('validated_signatures')
        path.write_text(json.dumps(request));job['request_digest']=digest(request);self.service._save(job)
        with patch('muli_sorter.archive_direct_move.hash_fd', wraps=hash_fd) as reads:
            done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        self.assertEqual(reads.call_count,12)
        self.assertEqual(done['verification']['reused_files'],0)

    def test_full_read_publishes_byte_progress_before_file_finishes(self):
        from muli_sorter.archive_direct_move import hash_fd
        job=self.submit();path=self.service.state/'requests'/('request-'+job['job_id']+'.json')
        request=json.loads(path.read_text());request.pop('verification_policy');request.pop('validated_signatures')
        path.write_text(json.dumps(request));job['request_digest']=digest(request);self.service._save(job)
        observations=[];save=self.service._save
        def capture(current):
            if 'verification' in current:observations.append(deepcopy(current['verification']))
            return save(current)
        def read(fd, *, progress=None):
            size=os.fstat(fd).st_size
            progress(1);progress(size-1)
            return hash_fd(fd)
        with patch('muli_sorter.archive_direct_move.hash_fd',side_effect=read), patch.object(self.service,'_save',side_effect=capture), patch('muli_sorter.archive_direct_move.time.monotonic',side_effect=range(10000)):
            done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        self.assertTrue(any(s['checked_files']==0 and s['checked_bytes']==1 for s in observations))
        self.assertEqual(done['verification']['checked_bytes'],done['verification']['total_bytes'])


class JournalRecoveryTests(direct_tests.DirectMoveTests):
    # Inherit the established race/crash suite against the new journal format.
    def test_missing_per_file_intent_after_rename_fails_closed(self):
        job=self.submit();moved=[]
        def stop(phase,row):
            if phase=='direct_move_renamed':
                moved.append(row);raise RuntimeError('power loss')
        self.service.checkpoint=stop
        with self.assertRaises(RuntimeError):self.service.run_job(job['job_id'])
        path=self.service.state/'requests'/('direct-entry-'+job['job_id']+'-'+digest(moved[0]['source_path'])+'.json')
        path.unlink()
        self.restart();done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans'] if p!=moved[0]['source_path']))

    def test_corrupt_per_file_target_identity_is_not_adopted(self):
        job=self.submit();moved=[]
        def stop(phase,row):
            if phase=='direct_move_renamed':moved.append(row);raise RuntimeError('power loss')
        self.service.checkpoint=stop
        with self.assertRaises(RuntimeError):self.service.run_job(job['job_id'])
        path=self.service.state/'requests'/('direct-entry-'+job['job_id']+'-'+digest(moved[0]['source_path'])+'.json')
        saved=json.loads(path.read_text());saved['entry'].update(state='removed',target_signature=None);path.write_text(json.dumps(saved))
        self.restart();done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)

    def test_old_whole_request_journal_still_recovers(self):
        job=self.submit()
        def stop(phase,row):
            if phase=='direct_move_prepared':
                path=self.service.state/'requests'/('direct-move-'+job['job_id']+'.json')
                old=json.loads(path.read_text());old.pop('entry_storage');path.write_text(json.dumps(old))
                raise RuntimeError('legacy journal checkpoint')
        self.service.checkpoint=stop
        with self.assertRaises(RuntimeError):self.service.run_job(job['job_id'])
        self.restart();done=self.complete(job)
        self.assertEqual(done['summary']['direct_moved_files'],4)

    def test_receipt_crash_before_index_write_recovers(self):
        job=self.submit()
        def stop(phase,row):
            if phase=='direct_move_receipt':raise RuntimeError('index not flushed yet')
        self.service.checkpoint=stop
        with self.assertRaises(RuntimeError):self.service.run_job(job['job_id'])
        self.restart();done=self.complete(job)
        self.assertEqual(len(self.service.history.snapshot(self.model)['archived_units']),2)

    def test_crash_after_intent_before_rename_recovers(self):
        job=self.submit()
        def stop(phase,row):
            if phase=='direct_move_intent':raise RuntimeError('power loss before rename')
        self.service.checkpoint=stop
        with self.assertRaises(RuntimeError):self.service.run_job(job['job_id'])
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))
        self.restart();done=self.complete(job)
        self.assertEqual(done['summary']['direct_moved_files'],4)
