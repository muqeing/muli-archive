from copy import deepcopy
import json
import unittest
from unittest.mock import patch
import test_archive_jobs as baseline
from muli_sorter.review import digest
from muli_sorter.order_feed_io import atomic_json
from muli_sorter.archive_io import ArchiveError


class ScopedHistoryTests(baseline.Fixture, unittest.TestCase):
    def make_service(self,**kwargs):
        kwargs.setdefault('indexed_history',True)
        kwargs.setdefault('build_history_catalog',True)
        kwargs.setdefault('indexed_reservations',True)
        return super().make_service(**kwargs)

    def complete(self):
        job=self.submit();done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        return done

    def test_copy_move_page_and_restart_use_original_proofs(self):
        done=self.complete()
        history=self.service.history
        state=history.snapshot(self.model)
        self.assertEqual(len(state['archived_units']),2)
        self.assertEqual(state['history_total'],2)
        self.assertFalse(state['warnings'],state)
        page=history.page(self.model['report_id'],state['history_generation'],0,1)
        self.assertEqual(page['total'],2)
        self.assertEqual(len(page['archived_units']),1)
        self.assertEqual(page['next_offset'],1)
        self.service.close();self.service=self.make_service()
        self.assertEqual(len(self.service.history.snapshot(self.model)['archived_units']),2)
        # A derived row alone never hides media after its original proof changes.
        receipt=self.service.state/'units'/done['outcomes'][0]['receipt']
        receipt.write_text('{}')
        current=self.service.history.snapshot(self.model)
        self.assertEqual(len(current['archived_units']),1)
        self.assertTrue(current['warnings'])

    def test_presentation_reuses_original_projection_but_execution_reads_receipts(self):
        done=self.complete();history=self.service.history
        # Synchronize once to bind the original index to receipt/journal metadata.
        history.snapshot(self.model)
        original=history.legacy.read
        def no_receipt(path):
            if path.name.startswith('receipt-'):
                raise AssertionError('unchanged display reopened original receipt')
            return original(path)
        with patch.object(history.legacy,'read',side_effect=no_receipt):
            self.assertEqual(len(history.snapshot(self.model)['archived_units']),2)
        with patch.object(history.legacy,'read',wraps=original) as reader:
            state=history.for_units(self.model,{self.model['units'][0]['unit_id']})
        self.assertEqual(len(state['archived_units']),1)
        self.assertTrue(any(c.args[0].name.startswith('receipt-') for c in reader.call_args_list))
        # Invalidating any dependency prevents reuse, so changed proofs still
        # fall back to the selected original receipt validation path.
        receipt=self.service.state/'units'/done['outcomes'][0]['receipt']
        receipt.write_text('{}')
        self.assertEqual(len(history.snapshot(self.model)['archived_units']),1)

    def test_completed_history_stays_cold_when_current_model_has_no_old_units(self):
        self.complete();history=self.service.history
        history.snapshot(self.model)
        empty={**self.model,'report_id':'sha256:'+'f'*64,'units':[]}
        with patch.object(history.legacy,'read',side_effect=AssertionError('cold receipt read')):
            state=history.snapshot(empty)
        self.assertEqual(state['archived_units'],[])
        self.assertEqual(state['history_total'],2)
        self.assertEqual(len(history.catalog.page(0,50)['items']),2)
        self.assertEqual(history.legacy.projected_units,0)
        self.assertEqual(history.legacy.cache_bytes,0)

    def test_content_repeat_routes_to_origin_without_scanning_other_units(self):
        self.complete();history=self.service.history
        history.snapshot(self.model)
        unit=deepcopy(self.model['units'][0]);unit['unit_id']='new-card-copy'
        for row in unit['files']:row['source_path']='new-card/'+row['source_path']
        model={**self.model,'report_id':'sha256:'+'e'*64,'units':[unit]}
        expected=digest(unit['files'])
        with patch.object(history.legacy,'read',wraps=history.legacy.read) as reader:
            state=history.snapshot(model)
        self.assertEqual(len(state['archived_units']),1,state)
        self.assertEqual(state['archived_units'][0]['unit_id'],'new-card-copy')
        self.assertTrue(state['archived_units'][0].get('content_copy_of'))
        read_receipts=[c.args[0] for c in reader.call_args_list if c.args[0].name.startswith('receipt-')]
        self.assertLessEqual(len(read_receipts),1,read_receipts)
        self.assertEqual(expected,digest(unit['files']))

    def test_requires_explicit_offline_initialization(self):
        self.service.close()
        (self.root/'console-state'/'history-catalog.sqlite3').unlink()
        with self.assertRaisesRegex(ArchiveError,'尚未迁移'):
            self.make_service(build_history_catalog=False)
        self.service=self.make_service()

    def test_unverified_copy_stays_protected_and_is_not_companion_authority(self):
        import test_retained_copies as retained_fixture
        job,request,path=retained_fixture.RetainedCopyTests.retained(self)
        history=self.service.history
        state=history.snapshot(self.model)
        self.assertEqual(len(state['retained_unit_ids']),2,state)
        self.assertEqual(state['archived_units'],[])
        self.assertFalse(state['retained_jobs'][0]['content_verified'])
        parent=history.for_units(self.model,set(state['retained_unit_ids']))
        self.assertEqual(parent['archived_units'],[])
        self.assertEqual(parent['retained_unit_ids'],[])
        original=self.service.get(job['job_id'])
        self.assertTrue(original['source_removal_disabled'])
        self.assertEqual(original['summary']['removed_sources'],0)
        self.assertEqual(original['status'],'copied_unverified')
        request['file_plans'].pop(next(iter(request['file_plans'])))
        path.write_text(json.dumps(request))
        state=history.snapshot(self.model)
        self.assertEqual(state['retained_unit_ids'],[])
        self.assertTrue(state['warnings'])

    def test_external_receipt_stays_scoped_and_validated(self):
        import test_external_archive_history as external_fixture
        self.external=self.service.state/'external-history'
        receipt,path=external_fixture.ExternalHistoryTests._write_receipt(self)
        state=self.service.history.snapshot(self.model)
        self.assertEqual(len(state['archived_units']),1,state)
        self.assertEqual(state['history_total'],1)
        self.assertEqual(state['archived_units'][0]['origin'],'existing_project_files')
        target=self.root/'projects'/receipt['files'][0]['target_path']
        target.write_bytes(b'changed')
        state=self.service.history.snapshot(self.model)
        self.assertEqual(state['archived_units'],[])
        self.assertTrue(state['warnings'])

    def test_derived_route_cannot_escape_original_request_directory(self):
        self.complete();history=self.service.history
        history.snapshot(self.model)
        with patch.object(history.catalog,'matching_jobs',return_value={'../../outside'}):
            with self.assertRaisesRegex(ArchiveError,'任务编号无效'):
                history.snapshot(self.model)

    def test_live_move_remains_exact_and_source_cleanup_finishes(self):
        self.service.move_enabled=True
        self.service.direct_move_view={'root':str(self.root),'staging':'staging','projects':'projects'}
        self.decisions['archive_options']={'mode':'move','existing':'skip_identical'}
        done=self.complete()
        state=self.service.history.snapshot(self.model)
        self.assertEqual(len(state['archived_units']),2)
        self.assertEqual(done['summary']['direct_moved_files'],4)
        self.assertFalse(state['warnings'],state)
        self.assertTrue(all(not(self.root/'staging'/p).exists() for p in done['file_plans']))
