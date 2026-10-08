"""Retained copies leave selection without becoming verified archive evidence."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from test_archive_jobs import Fixture
from muli_sorter.archive_retained_copies import retained_projection, retained_summary
from muli_sorter.archive_history import file_identity
from muli_sorter.order_feed_io import digest
from muli_sorter.review_projection import compact_archive, pending_view
from muli_sorter.workflow_status import snapshot as workflow_snapshot


class RetainedCopyTests(Fixture, unittest.TestCase):
    def retained(self):
        job = self.submit()
        request_path = self.service.state / 'requests' / ('request-' + job['job_id'] + '.json')
        request = json.loads(request_path.read_text())
        count = len(request['file_plans'])
        total = sum(r['size_bytes'] for r in request['file_plans'].values())
        job.update(status='copied_unverified', storage='unverified_copy',
                   source_removal_disabled=True, verification={'content_verified': False}, outcomes=[])
        job['summary'].update(completed_files=count, completed_units=job['summary']['total_units'],
                              processed_bytes=total, removed_sources=0)
        job['copy_override'] = {
            'job_id': job['job_id'], 'status': 'copied_unverified', 'source_removal': False,
            'verification': 'not_full_content_verified', 'completed_at': 1,
            'request_sha256': job['request_digest'], 'total_files': count, 'completed_files': count,
            'source_files_present': count, 'target_entries_present': count, 'total_bytes': total}
        self.service._save(job)
        return job, request, request_path

    def test_exact_bound_copy_is_separate_and_reuses_compact_metadata(self):
        job, request, path = self.retained()
        # No receipt, media read, or media stat can be used to grant verification.
        with patch.object(self.service.history, '_item', side_effect=AssertionError('verified receipt path')):
            state = self.service.history.snapshot(self.model)
        self.assertFalse(state['warnings'], state)
        self.assertEqual(len(state['retained_unit_ids']), 2)
        self.assertEqual(state['retained_current_files'], 4)
        self.assertEqual(state['archived_units'], [])
        self.assertFalse(state['retained_jobs'][0]['content_verified'])
        self.assertNotIn(path, self.service.history.cache)
        with patch.object(self.service.history, 'read', side_effect=AssertionError('record reread')):
            again = self.service.history.snapshot(self.model)
        self.assertEqual(again, state)
        parent = self.service.history.for_units(self.model, set(state['retained_unit_ids']))
        self.assertEqual(parent['archived_units'], [])
        self.assertEqual(parent['retained_unit_ids'], [])

    def test_changed_or_incomplete_audit_cannot_hide_any_units(self):
        job, request, _ = self.retained()
        invalid = []
        changed = deepcopy(request); changed['roots'] = {}; invalid.append((job, changed))
        changed = deepcopy(job); changed['request_digest'] = '0' * 64; invalid.append((changed, request))
        changed = deepcopy(job); changed['copy_override']['request_sha256'] = '0' * 64; invalid.append((changed, request))
        changed = deepcopy(job); changed['copy_override']['target_entries_present'] -= 1; invalid.append((changed, request))
        changed = deepcopy(job); changed['source_removal_disabled'] = False; invalid.append((changed, request))
        changed = deepcopy(job); changed['summary']['removed_sources'] = 1; invalid.append((changed, request))
        changed = deepcopy(job); changed['copy_override'] = {}; invalid.append((changed, request))
        for altered_job, altered_request in invalid:
            with self.subTest(altered_job=altered_job['job_id']):
                with self.assertRaises(ValueError):
                    retained_projection(altered_job, altered_request, self.service.identity)

    def test_record_change_invalidates_projection_without_touching_media(self):
        job, request, path = self.retained()
        self.assertEqual(len(self.service.history.snapshot(self.model)['retained_unit_ids']), 2)
        request['file_plans'].pop(next(iter(request['file_plans'])))
        path.write_text(json.dumps(request))
        state = self.service.history.snapshot(self.model)
        self.assertEqual(state['retained_unit_ids'], [])
        self.assertTrue(state['warnings'])

    def test_current_scope_mismatch_stays_pending_and_verified_receipt_wins(self):
        job, request, _ = self.retained()
        projection = retained_projection(job, request, self.service.identity)
        current = {u['unit_id']: digest(file_identity(u['files'])) for u in self.model['units']}
        retained = list(projection['units'])
        current[retained[0]] = 'changed'
        state, warnings = retained_summary([projection], current, {})
        self.assertEqual(state['retained_unit_ids'], [retained[1]])
        self.assertTrue(warnings)
        state, _ = retained_summary([projection], current, {retained[1]: {'in_current_model': True}})
        self.assertEqual(state['retained_unit_ids'], [])

    def test_pending_view_and_handoff_keep_unverified_separate(self):
        self.retained()
        archive = self.service.history.snapshot(self.model)
        materials = {'units': {u['unit_id']: {'category': 'shoot'} for u in self.model['units']}}
        view, _ = pending_view(self.model, archive, materials)
        self.assertEqual(len(view['units']), len(self.model['units']) - 2)
        self.assertEqual(len(view['presentation']['hidden_units']), 2)
        compact = compact_archive(archive, archive['retained_unit_ids'])
        self.assertEqual(compact['retained_unit_ids'], archive['retained_unit_ids'])
        self.assertEqual(compact['current_files'], 0)
        self.assertEqual(compact['parent_projects'], {})
        retained_model = deepcopy(self.model)
        retained_model['units'] = [u for u in self.model['units'] if u['unit_id'] in archive['retained_unit_ids']]
        handoff = workflow_snapshot(retained_model, archive, materials, None, 'http://example.test')
        self.assertTrue(handoff['batches'])
        for batch in handoff['batches']:
            self.assertEqual(batch['archived'], 0)
            self.assertEqual(batch['pending'], 0)
            self.assertEqual(batch['retained'], batch['units'])
            self.assertEqual(batch['phase'], 'copied_unverified')


if __name__ == '__main__':
    unittest.main()
