from copy import deepcopy
import json
import re
import unittest
from test_archive_jobs import Fixture
from muli_sorter.review import validate_model, validate_decisions
from muli_sorter.review_projection import compact_archive, pending_view, history_page
from muli_sorter.review_render import render_review


class ProjectionTests(Fixture, unittest.TestCase):
    def complete(self):
        job = self.submit()
        self.service.run_job(job['job_id'])
        return self.service.history.snapshot(self.model)

    def test_only_receipt_backed_units_lose_detail_and_full_plan_stays_required(self):
        before = deepcopy(self.model)
        archive = self.complete()
        materials = self.service.material_state(self.model)
        view, roles = pending_view(self.model, archive, materials)
        archived = {r['unit_id'] for r in archive['archived_units'] if r['in_current_model']}
        self.assertEqual(view['report_id'], self.model['report_id'])
        self.assertEqual({u['unit_id'] for u in view['units']}, {u['unit_id'] for u in self.model['units']} - archived)
        self.assertEqual({r[0] for r in view['presentation']['hidden_units']}, archived)
        self.assertEqual(view['initial_segments'], self.model['initial_segments'])
        self.assertTrue(all(len(row) == 5 for row in view['presentation']['hidden_units']))
        self.assertEqual(len(roles['hidden_roles']), len(archived))
        self.assertEqual(self.model, before)
        validate_model(self.model)
        with self.assertRaises(ValueError):
            validate_model(view)  # presentation must never become canonical evidence
        validate_decisions(self.model, self.decisions)
        omitted = deepcopy(self.decisions)
        omitted['segments'][0]['unit_ids'].pop()
        with self.assertRaises(ValueError):
            validate_decisions(self.model, omitted)

    def test_compact_summary_has_identity_only_and_history_is_bounded_versioned(self):
        archive = self.complete()
        summary = compact_archive(archive)
        self.assertNotIn('archived_units', summary)
        self.assertEqual(summary['history_total'], len(archive['archived_units']))
        self.assertEqual(summary['current_files'], 4)
        video = next(u['unit_id'] for u in self.model['units'] if u['kind'] == 'video')
        parent = compact_archive(archive, {video})['parent_projects']
        self.assertEqual(set(parent), {video})
        self.assertEqual(parent[video], next(r['project'] for r in archive['archived_units'] if r['unit_id'] == video))
        rows = [dict(archive['archived_units'][0], unit_id=f'U-{i}') for i in range(123)]
        archive = dict(archive, archived_units=rows)
        bundle = {'archive_state':archive, 'history_generation':compact_archive(archive)['history_generation']}
        first = history_page(bundle, archive['report_id'], '', 0, 50)
        second = history_page(bundle, archive['report_id'], first['generation'], 50, 50)
        last = history_page(bundle, archive['report_id'], first['generation'], 100, 50)
        self.assertEqual([len(p['archived_units']) for p in (first, second, last)], [50,50,23])
        self.assertIsNone(last['next_offset'])
        self.assertEqual([r['unit_id'] for p in (first,second,last) for r in p['archived_units']], [r['unit_id'] for r in rows])
        for report, gen, offset, limit in [('other','',0,50),(archive['report_id'],'old',0,50),(archive['report_id'],'',-1,50),(archive['report_id'],'',0,51)]:
            with self.assertRaises(ValueError): history_page(bundle, report, gen, offset, limit)

    def test_rendered_pending_payload_has_no_archived_paths_hashes_or_previews(self):
        archive = self.complete()
        materials = self.service.material_state(self.model)
        view, roles = pending_view(self.model,archive,materials)
        html = render_review(view, archive_state=compact_archive(archive), material_state=roles, submission_enabled=True)
        payload = json.loads(re.search(r'<script id="review-model" type="application/json">(.*?)</script>',html,re.S)[1])
        for u in self.model['units']:
            if any(r[0] == u['unit_id'] for r in payload['presentation']['hidden_units']):
                for f in u['files']:
                    self.assertNotIn(f['blake3'], html)
                    self.assertNotIn(f['source_path'], html)
        settings = json.loads(re.search(r'<script id="review-settings" type="application/json">(.*?)</script>',html,re.S)[1])
        self.assertNotIn('archived_units', settings['archive_state'])
        self.assertIn('history_total', settings['archive_state'])
