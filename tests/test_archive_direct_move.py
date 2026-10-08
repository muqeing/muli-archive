import errno
import json
import os
import unittest
from unittest.mock import patch
from test_archive_jobs import Fixture
from muli_sorter.archive_io import ArchiveError, directory
from muli_sorter.archive_rename_io import STRATEGY, rename_noreplace
from muli_sorter.archive_layout import studio_target_rows


class DirectMoveTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service.move_enabled = True
        self.service.direct_move_view = {'root':str(self.root),'staging':'staging','projects':'projects'}
        self.decisions['archive_options'] = {'mode':'move','existing':'skip_identical'}

    def first(self):
        segment=next(s for s in self.decisions['segments'] if s['decision']=='confirmed')
        unit=next(u for u in self.model['units'] if u['unit_id']==segment['unit_ids'][0])
        project=next(p for p in self.model['projects'] if p['project_id']==segment['project_id'])
        row=studio_target_rows(unit,project)[0]
        return self.root/'staging'/row['source_path'],self.root/'projects'/row['target_path']

    def restart(self):
        self.service.close()
        self.service=self.make_service(move_enabled=True, direct_move_view={'root':str(self.root),'staging':'staging','projects':'projects'})

    def complete(self, job=None):
        job=job or self.submit()
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        return done

    def test_direct_move_keeps_inode_writes_no_media_and_history_hides_units(self):
        job=self.submit()
        self.assertEqual(job['execution_strategy'],STRATEGY)
        plans=job['file_plans']
        before={p:((self.root/'staging'/p).stat().st_ino,(self.root/'staging'/p).read_bytes()) for p in plans}
        with patch('muli_sorter.archive.stage_file',side_effect=AssertionError('must not copy')):
            done=self.complete(job)
        self.assertEqual(done['summary']['direct_moved_files'],4)
        self.assertEqual(done['summary']['copy_files'],0)
        self.assertEqual(done['summary']['removed_sources'],4)
        for p,r in plans.items():
            dst=self.root/'projects'/r['target_path']
            self.assertFalse((self.root/'staging'/p).exists())
            self.assertEqual((dst.stat().st_ino,dst.read_bytes()),before[p])
            self.assertEqual(dst.stat().st_nlink,1)
        self.restart()
        snap=self.service.history.snapshot(self.model)
        self.assertEqual(len(snap['archived_units']),2,snap)
        self.assertEqual(snap['warnings'],[])
        self.assertEqual(self.service.run_job(job['job_id'])['status'],'completed')

    def test_crash_after_rename_before_result_recovers_by_inode(self):
        job=self.submit();triggered=[]
        def checkpoint(phase,row):
            if phase=='direct_move_renamed' and not triggered:
                triggered.append(row['source_path'])
                raise RuntimeError('simulated process exit')
        self.service.checkpoint=checkpoint
        with self.assertRaises(RuntimeError):self.service.run_job(job['job_id'])
        self.assertFalse((self.root/'staging'/triggered[0]).exists())
        self.restart();done=self.complete(job)
        self.assertEqual(done['summary']['direct_moved_files'],4)

    def test_replacement_destination_after_crash_is_not_adopted(self):
        job=self.submit();moved=[]
        def checkpoint(phase,row):
            if phase=='direct_move_renamed':
                moved.append(row);raise RuntimeError('crash')
        self.service.checkpoint=checkpoint
        with self.assertRaises(RuntimeError):self.service.run_job(job['job_id'])
        target=self.root/'projects'/moved[0]['target_path']
        content=target.read_bytes();target.rename(target.with_suffix('.saved'));target.write_bytes(content)
        self.restart();done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed')
        self.assertEqual(target.read_bytes(),content)
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans'] if p!=moved[0]['source_path']))

    def test_racing_destination_is_never_overwritten(self):
        job=self.submit();triggered=[]
        def checkpoint(phase,row):
            if phase=='direct_move_intent' and not triggered:
                triggered.append(row)
                (self.root/'projects'/row['target_path']).write_bytes(b'unrelated')
        self.service.checkpoint=checkpoint
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed')
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))
        self.assertEqual((self.root/'projects'/triggered[0]['target_path']).read_bytes(),b'unrelated')

    def test_parent_swapped_at_rename_is_not_reported_complete(self):
        from muli_sorter.archive_direct_move import rename_noreplace as real
        job=self.submit();row=next(iter(job['file_plans'].values()))
        parent=(self.root/'projects'/row['target_path']).parent
        def race(src,sname,dst,dname):
            parent.rename(parent.with_name(parent.name+'-detached'));parent.mkdir()
            return real(src,sname,dst,dname)
        with patch('muli_sorter.archive_direct_move.rename_noreplace',side_effect=race):
            done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed')
        self.assertTrue(done['direct_move_recovery_required'])
        self.assertEqual(self.service.history.snapshot(self.model)['archived_units'],[])
        self.assertTrue((parent.with_name(parent.name+'-detached')/row['target_name']).exists())

    def test_source_mutation_after_intent_preserves_all_sources(self):
        job=self.submit()
        def checkpoint(phase,row):
            if phase=='direct_move_intent':(self.root/'staging'/row['source_path']).write_bytes(b'changed')
        self.service.checkpoint=checkpoint
        self.assertEqual(self.service.run_job(job['job_id'])['status'],'failed')
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))

    def test_bad_content_in_last_file_blocks_before_first_rename(self):
        job=self.submit();last=list(job['file_plans'])[-1]
        p=self.root/'staging'/last;p.write_bytes(b'X'*p.stat().st_size)
        self.assertEqual(self.service.run_job(job['job_id'])['status'],'failed')
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))

    def test_skipped_identical_uses_existing_verified_cleanup(self):
        src,dst=self.first();dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(src.read_bytes());ino=dst.stat().st_ino
        job=self.submit();self.assertEqual(job['execution_strategy'],STRATEGY)
        done=self.complete(job);self.assertEqual(done['summary']['skipped_files'],1)
        self.assertEqual(dst.stat().st_ino,ino);self.assertFalse(src.exists())

    def test_cross_mount_blocks_without_silent_copy_fallback(self):
        with patch('muli_sorter.archive_rename_io.mount_id',side_effect=[1,2]*20):
            result=self.service.preflight(self.decisions)
        self.assertEqual(result['status'],'blocked')
        self.assertIn('未自动改成复制',str(result['errors']))
        self.assertEqual(self.service.list_jobs(),[])

    def test_old_move_request_stays_copy_after_enabling_new_mode(self):
        self.service.direct_move_view=None;job=self.submit()
        self.restart()
        with patch('muli_sorter.archive_direct_move.run_direct_move',side_effect=AssertionError('old request')):
            done=self.complete(job)
        self.assertEqual(done['summary']['copy_files'],4)

    def test_copy_mode_preserves_source_and_distinct_inode(self):
        self.decisions['archive_options']['mode']='copy';src,dst=self.first()
        job=self.submit();self.complete(job)
        self.assertTrue(src.exists());self.assertNotEqual(src.stat().st_ino,dst.stat().st_ino)

    def test_changed_common_view_is_blocked_before_any_move(self):
        job=self.submit();self.service.direct_move_view['staging']='projects'
        self.assertEqual(self.service.run_job(job['job_id'])['status'],'failed')
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))

    def test_exdev_after_intent_does_not_silently_copy_or_remove(self):
        job=self.submit()
        with patch('muli_sorter.archive_direct_move.rename_noreplace',side_effect=OSError(errno.EXDEV,'changed mount')):
            done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed')
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))

    def test_new_manual_project_works(self):
        self.manual();self.complete()

    def test_duplicate_content_name_gets_numbered_target(self):
        src,dst=self.first();inode=src.stat().st_ino
        dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(b'unrelated')
        done=self.complete()
        self.assertEqual(done['summary']['renamed_files'],1)
        self.assertEqual(dst.read_bytes(),b'unrelated')
        self.assertEqual(dst.with_stem(dst.stem+'_1').stat().st_ino,inode)

    def test_primitive_never_overwrites_target(self):
        with directory(self.root) as root:
            (self.root/'one').write_bytes(b'one');(self.root/'two').write_bytes(b'two')
            with self.assertRaises(FileExistsError):rename_noreplace(root,'one',root,'two')
            self.assertEqual((self.root/'one').read_bytes(),b'one')
            self.assertEqual((self.root/'two').read_bytes(),b'two')

    def test_preflight_moves_nothing_and_identifies_zero_copies(self):
        before={p:p.stat().st_ino for p in (self.root/'staging').rglob('*') if p.is_file()}
        preview=self.service.preflight(self.decisions)
        self.assertEqual(preview['summary']['direct_move_files'],4)
        self.assertEqual(preview['summary']['copy_files'],0)
        self.assertEqual(before,{p:p.stat().st_ino for p in before})

    def test_manifest_change_after_intent_blocks(self):
        job=self.submit()
        def checkpoint(phase,row):
            if phase=='direct_move_intent':
                path=self.root/'staging'/row['source_path'].split('/')[0]/'ingest_complete.json'
                path.write_text('{}')
        self.service.checkpoint=checkpoint
        self.assertEqual(self.service.run_job(job['job_id'])['status'],'failed')
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))

    def test_source_reappearance_after_crash_is_not_removed(self):
        job=self.submit();moved=[]
        def checkpoint(phase,row):
            if phase=='direct_move_renamed':
                moved.append(row);raise RuntimeError('crash')
        self.service.checkpoint=checkpoint
        with self.assertRaises(RuntimeError):self.service.run_job(job['job_id'])
        p=self.root/'staging'/moved[0]['source_path'];p.write_bytes(b'new import')
        self.restart()
        self.assertEqual(self.service.run_job(job['job_id'])['status'],'failed')
        self.assertEqual(p.read_bytes(),b'new import')

    def test_changed_target_parent_after_intent_blocks(self):
        job=self.submit()
        def checkpoint(phase,row):
            if phase=='direct_move_intent':
                parent=(self.root/'projects'/row['target_path']).parent
                parent.rename(parent.with_name(parent.name+'-saved'));parent.mkdir()
        self.service.checkpoint=checkpoint
        self.assertEqual(self.service.run_job(job['job_id'])['status'],'failed')
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))

    def test_success_uses_one_content_read_per_file(self):
        from muli_sorter.archive_direct_move import hash_fd
        with patch('muli_sorter.archive_direct_move.hash_fd',wraps=hash_fd) as reader:
            self.complete()
        self.assertEqual(reader.call_count,4)

    def test_receipt_checkpoint_crash_can_finish_after_restart(self):
        job=self.submit()
        def checkpoint(phase,row):
            if phase=='direct_move_receipt':raise RuntimeError('crash')
        self.service.checkpoint=checkpoint
        with self.assertRaises(RuntimeError):self.service.run_job(job['job_id'])
        self.restart();self.complete(job)
        self.assertEqual(len(self.service.history.snapshot(self.model)['archived_units']),2)

    def test_active_backup_blocks_direct_move(self):
        job=self.submit();old=self.service.runtime
        def active():
            data=old();data['batches'].append({'batch_id':'another','state':'COPYING'});return data
        self.service.runtime=active
        self.assertEqual(self.service.run_job(job['job_id'])['status'],'failed')
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))

    def test_size_only_with_independent_receipt_supports_direct_move(self):
        import test_postcopy_move as fixture
        self.postcopy=self.root/'postcopy';self.postcopy.mkdir()
        fixture.PostcopyMoveTests._make_size_only(self)
        with patch.dict(os.environ,{'MULI_POSTCOPY_RECEIPTS':str(self.postcopy)}):
            done=self.complete()
        self.assertEqual(done['summary']['direct_moved_files'],4)

    def test_independent_receipt_change_after_intent_blocks(self):
        import test_postcopy_move as fixture
        self.postcopy=self.root/'postcopy';self.postcopy.mkdir()
        fixture.PostcopyMoveTests._make_size_only(self)
        with patch.dict(os.environ,{'MULI_POSTCOPY_RECEIPTS':str(self.postcopy)}):
            job=self.submit()
            def checkpoint(phase,row):
                if phase=='direct_move_intent':next(self.postcopy.glob('*.json')).write_text('{}')
            self.service.checkpoint=checkpoint
            self.assertEqual(self.service.run_job(job['job_id'])['status'],'failed')
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))

    def test_corrected_timestamps_support_direct_move(self):
        import test_time_correction_receipt as fixture
        fixture.ArchiveCorrectionReceiptTests.install_corrected_archive(self)
        self.decisions['archive_options']={'mode':'move','existing':'skip_identical'}
        done=self.complete()
        self.assertEqual(done['summary']['direct_moved_files'],4)

    def test_corrected_receipt_change_after_intent_blocks(self):
        import test_time_correction_receipt as fixture
        fixture.ArchiveCorrectionReceiptTests.install_corrected_archive(self)
        self.decisions['archive_options']={'mode':'move','existing':'skip_identical'}
        job=self.submit()
        def checkpoint(phase,row):
            if phase=='direct_move_intent':
                p=self.root/'staging/BATCH_20260928_000001/time_correction_receipt.json'
                v=json.loads(p.read_text());v['correction_id']='changed';p.write_text(json.dumps(v))
        self.service.checkpoint=checkpoint
        self.assertEqual(self.service.run_job(job['job_id'])['status'],'failed')
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))


import test_archive_companions as companion_fixture
class DirectCompanionTests(unittest.TestCase):
    setUp=companion_fixture.CompanionArchiveTests.setUp
    tearDown=companion_fixture.CompanionArchiveTests.tearDown
    runtime=companion_fixture.CompanionArchiveTests.runtime
    select=companion_fixture.CompanionArchiveTests.select
    run_ready=companion_fixture.CompanionArchiveTests.run_ready

    def test_main_and_companions_move_and_hide_together(self):
        self.service.direct_move_view={'root':str(self.root),'staging':'staging','projects':'projects'}
        self.decisions['archive_options']['mode']='move'
        ids=set(self.links)|set(self.links.values());self.select(ids)
        done=self.run_ready()
        self.assertEqual(done['execution_strategy'],STRATEGY)
        self.assertEqual(done['summary']['direct_moved_files'],5)
        archived={r['unit_id'] for r in self.service.history.snapshot(self.model)['archived_units'] if not r.get('content_copy_of')}
        self.assertEqual(archived,ids)
