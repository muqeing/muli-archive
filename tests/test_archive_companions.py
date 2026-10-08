from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from triage_fixture import prepare_triage, DJI
from muli_sorter.archive_jobs import ArchiveJobs
from muli_sorter.archive_layout import studio_target_rows
from muli_sorter.material_triage import companion_links


class CompanionArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'fixture'
        self.model, self.decisions = prepare_triage(self.root)
        self.service = ArchiveJobs(self.root/'staging', self.root/'projects', self.root/'console-state', lambda: self.model, self.runtime, enabled=True, move_enabled=True)
        self.units = {u['unit_id']: u for u in self.model['units']}
        self.links, _ = companion_links(self.model)
        self.project = next(p for p in self.model['projects'] if p['dates'] == ['2026-07-10'])
        self.decisions['archive_options'] = {'mode': 'copy', 'existing': 'skip_identical'}

    def tearDown(self):
        self.service.close(); self.tmp.cleanup()

    def runtime(self):
        data = json.loads((self.root/'runtime-state.json').read_text()); data['generated_at'] = datetime.now(timezone.utc).isoformat(); return data

    def select(self, ids, project=None):
        self.decisions['segments'] = [{'segment_id': 's-' + str(n), 'label': '合成验证', 'unit_ids': [uid],
            'project_id': (project or self.project)['project_id'] if uid in ids else None,
            'decision': 'confirmed' if uid in ids else 'deferred', 'acknowledge_date_mismatch': uid in ids}
            for n, uid in enumerate(self.units)]

    def run_ready(self):
        before = self.service.preflight(self.decisions)
        self.assertEqual(before['status'], 'ready', before)
        job = self.service.submit(before['preview_id'], self.decisions, True)
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        return done

    def test_related_files_archive_and_keep_independent_history_identity(self):
        ids = set(self.links) | set(self.links.values()); self.select(ids)
        done = self.run_ready()
        self.assertEqual(done['summary']['completed_files'], 5)
        history = self.service.history.snapshot(self.model)
        # Byte-identical units are reported as content copies of the archived
        # original; they are archived records too, but not independently moved.
        archived = {r['unit_id'] for r in history['archived_units'] if not r.get('content_copy_of')}
        self.assertEqual(archived, ids)
        for uid in ids:
            self.assertTrue(all((self.root/'staging'/f['source_path']).exists() for f in self.units[uid]['files']))

    def test_supplement_only_uses_verified_parent_project(self):
        self.select(set(self.links.values())); self.run_ready()
        self.select(set(self.links)); done = self.run_ready()
        self.assertEqual(done['summary']['completed_files'], 3)
        self.select(set(self.links), next(p for p in self.model['projects'] if p != self.project))
        self.assertEqual(self.service.preflight(self.decisions)['status'], 'blocked')

    def test_auxiliary_cannot_be_submitted_without_parent_authorization(self):
        self.select(set(self.links))
        self.assertEqual(self.service.preflight(self.decisions)['status'], 'blocked')
        self.select({u['unit_id'] for u in self.units.values() if any('._' in f['name'] for f in u['files'])})
        self.assertEqual(self.service.preflight(self.decisions)['status'], 'blocked')

    def test_parent_rename_is_carried_into_companion_name_and_receipt(self):
        self.select(set(self.links) | set(self.links.values()))
        parent = next(self.units[p] for p in self.links.values() if any(DJI in f['name'] for f in self.units[p]['files']))
        row = studio_target_rows(parent, self.project)[0]
        target = self.root/'projects'/row['target_path']; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b'existing different')
        self.run_ready()
        self.assertEqual(target.read_bytes(), b'existing different')
        self.assertTrue(target.with_name(DJI + '_1.MP4').is_file())
        self.assertTrue(target.with_name(DJI + '_1.THM').is_file())
        self.assertTrue(target.with_name(DJI + '_1.SCR').is_file())
        receipts = [json.loads(p.read_text()) for p in (self.root/'console-state/units').glob('receipt-*.json')]
        sidecars = [f for r in receipts for f in r['files'] if f.get('companion_of_unit_id') == parent['unit_id']]
        self.assertTrue(all(f['companion_parent_target'].endswith('_1.MP4') for f in sidecars))

    def test_manual_project_inherits_parent_scope(self):
        ids = set(self.links) | set(self.links.values()); self.select(ids)
        descriptor = {'project_id': 'manual-'+'a'*32, 'name': '带附属文件的新项目', 'shoot_date': '2026-07-10', 'unit_ids': list(set(self.links.values()))}
        self.decisions['manual_projects'] = [descriptor]
        for s in self.decisions['segments']:
            if s['decision'] == 'confirmed': s['project_id'] = descriptor['project_id']
        self.run_ready()
        self.assertTrue((self.root/'projects/2026/7月/20260710_自建_带附属文件的新项目/2视频素材/相机/C0001M01.XML').exists())

    def shared_manual_project(self):
        first, second = list(dict.fromkeys(self.links.values()))[:2]
        children = {uid for uid, parent in self.links.items() if parent == second}
        self.select({first, second} | children)
        descriptor = {'project_id': 'manual-'+'b'*32, 'name': '同日共用的新项目',
                      'shoot_date': '2026-07-10', 'unit_ids': [first]}
        self.decisions['manual_projects'] = [descriptor]
        for segment in self.decisions['segments']:
            if segment['decision'] == 'confirmed':
                segment['project_id'] = descriptor['project_id']
        # UI emits separate companion segments; validation must be order independent.
        self.decisions['segments'].reverse()
        return second, children

    def test_same_date_reused_manual_project_authorizes_second_parents_companions(self):
        second, children = self.shared_manual_project()
        self.run_ready()
        history = self.service.history.snapshot(self.model)
        archived = {r['unit_id']: r for r in history['archived_units']}
        self.assertTrue(children <= archived.keys())
        self.assertTrue(all(archived[uid]['project']['path'] == archived[second]['project']['path'] for uid in children))

    def test_reused_manual_project_does_not_authorize_unconfirmed_or_different_parent(self):
        second, children = self.shared_manual_project()
        parent = next(s for s in self.decisions['segments'] if second in s['unit_ids'])
        original = deepcopy(parent)
        for changes in ({'decision': 'pending'}, {'decision': 'deferred'}, {'project_id': self.project['project_id']}):
            with self.subTest(changes=changes):
                parent.update(original)
                parent.update(changes)
                self.assertEqual(self.service.preflight(self.decisions)['status'], 'blocked')
        self.assertFalse(any((self.root/'console-state/requests').glob('*.json')))

    def test_unrelated_date_cannot_inherit_reused_manual_project(self):
        self.shared_manual_project()
        self.decisions['manual_projects'][0]['shoot_date'] = '2026-07-11'
        result = self.service.preflight(self.decisions)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any('素材范围' in e for e in result['errors']))

    def test_move_verifies_all_related_files_before_cleanup(self):
        ids = set(self.links) | set(self.links.values()); self.select(ids)
        self.decisions['archive_options']['mode'] = 'move'
        done = self.run_ready()
        self.assertEqual(done['summary']['removed_sources'], 5)
        self.assertTrue(all(not (self.root/'staging'/f['source_path']).exists() for uid in ids for f in self.units[uid]['files']))
        archived = [r for r in self.service.history.snapshot(self.model)['archived_units'] if not r.get('content_copy_of')]
        self.assertEqual(len(archived), len(ids))

    def test_supplement_after_parent_has_been_moved(self):
        self.select(set(self.links.values())); self.decisions['archive_options']['mode'] = 'move'; self.run_ready()
        self.select(set(self.links)); done = self.run_ready()
        self.assertEqual(done['summary']['removed_sources'], 3)

    def test_failed_main_does_not_publish_companions_or_clean_sources(self):
        child = next(uid for uid, parent in self.links.items() if any(DJI in f['name'] for f in self.units[parent]['files']))
        parent = self.links[child]; self.select({child, parent})
        self.decisions['archive_options']['mode'] = 'move'
        def fail(phase, value):
            if phase == 'unit_staged' and value['identity']['unit_id'] == parent:
                raise OSError('synthetic interrupted primary')
        self.service.checkpoint = fail
        before = self.service.preflight(self.decisions)
        job = self.service.submit(before['preview_id'], self.decisions, True)
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'partial')
        self.assertEqual(done['summary']['completed_files'], 0)
        self.assertTrue(all((self.root/'staging'/f['source_path']).exists() for uid in (child, parent) for f in self.units[uid]['files']))
        self.assertFalse(any((self.root/'projects').rglob('*.THM')))

    def test_repeated_confirmation_is_idempotent(self):
        self.select(set(self.links) | set(self.links.values()))
        first = self.run_ready(); second = self.run_ready()
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertEqual(len(self.service.list_jobs()), 1)


if __name__ == '__main__':
    unittest.main()
