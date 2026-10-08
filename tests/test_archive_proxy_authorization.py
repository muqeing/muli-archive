import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from triage_fixture import prepare_triage
from muli_sorter.archive_companions import primary_file
from muli_sorter.archive_jobs import ArchiveJobs
from muli_sorter.archive_layout import file_folder
from muli_sorter.archive_options import options_for
from muli_sorter.archive_io import ArchiveError
from muli_sorter.material_triage import companion_links


class ProxyAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'fixture'
        self.model, self.decisions = prepare_triage(self.root)
        self.proxy = next(u for u in self.model['units'] if u['kind'] == 'proxy_only')
        self.project = next(p for p in self.model['projects']
                           if p['project_id'] == self.proxy['candidate_project_ids'][0])
        segment = next(s for s in self.decisions['segments'] if self.proxy['unit_id'] in s['unit_ids'])
        segment.update(decision='confirmed', project_id=self.proxy['candidate_project_ids'][0],
                       acknowledge_date_mismatch=False)
        self.runtime = lambda: json.loads((self.root / 'runtime-state.json').read_text())

    def tearDown(self):
        if hasattr(self, 'service'):
            self.service.close()
        self.tmp.cleanup()

    def service_for(self, **kwargs):
        self.service = ArchiveJobs(self.root / 'staging', self.root / 'projects', self.root / 'state',
                                   lambda: self.model, self.runtime, enabled=True, **kwargs)
        return self.service

    def test_default_rejects_orphan_proxy_and_explicit_copy_succeeds(self):
        service = self.service_for()
        self.assertEqual(service.preflight(self.decisions)['status'], 'blocked')
        decisions = deepcopy(self.decisions)
        decisions['archive_options'] = {'mode': 'copy', 'existing': 'skip_identical',
                                        'proxy_only_unit_ids': [self.proxy['unit_id']]}
        preview = service.preflight(decisions)
        self.assertEqual(preview['status'], 'ready', preview)
        job = service.submit(preview['preview_id'], decisions, True)
        done = service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        self.assertTrue((self.root / 'projects' / self.project['path']).exists())
        self.assertEqual(done['summary']['completed_files'], 1)

    def test_explicit_move_cleans_proxy_source_after_full_verification(self):
        service = self.service_for(move_enabled=True)
        decisions = deepcopy(self.decisions)
        decisions['archive_options'] = {'mode': 'move', 'existing': 'skip_identical',
                                        'proxy_only_unit_ids': [self.proxy['unit_id']]}
        preview = service.preflight(decisions)
        job = service.submit(preview['preview_id'], decisions, True)
        done = service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        self.assertFalse((self.root / 'staging' / self.proxy['files'][0]['source_path']).exists())

    def test_option_list_and_authorized_parent_are_strict(self):
        self.assertEqual(options_for({'archive_options': {'mode': 'copy', 'existing': 'error'}}),
                         {'mode': 'copy', 'existing': 'error'})
        for ids in ([self.proxy['unit_id'], self.proxy['unit_id']],):
            with self.subTest(ids=ids), self.assertRaises(ArchiveError):
                options_for({'archive_options': {'mode': 'copy', 'existing': 'error',
                                                 'proxy_only_unit_ids': ids}})
        proxy = {'unit_id': 'p', 'kind': 'proxy_only',
                 'files': [{'name': 'DCIM/DJI_001/DJI_20260711100000_0002_D.LRF'}],
                 'provenance': [{'manifest_id': 'm'}]}
        child = {'unit_id': 'c', 'kind': 'auxiliary',
                 'files': [{'name': 'MISC/THM/DJI_001/DJI_20260711100000_0002_D.THM'}],
                 'provenance': [{'manifest_id': 'm'}]}
        self.assertEqual(companion_links({'units': [proxy, child]})[0], {})
        self.assertEqual(companion_links({'units': [proxy, child]}, approved_proxy_ids=['p'])[0], {'c': 'p'})
        marked = {**proxy, '_proxy_archive_authorized': True}
        self.assertEqual(primary_file(marked)['name'].lower().endswith('.lrf'), True)
        self.assertEqual(file_folder(marked, marked['files'][0])[0], '2视频素材/侧拍')


if __name__ == '__main__':
    unittest.main()
