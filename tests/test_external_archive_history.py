from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import unittest

from blake3 import blake3

from test_archive_jobs import Fixture
from muli_sorter.archive_io import directory, open_file, signature, subdirectory
from muli_sorter.order_feed_io import atomic_json, digest


class ExternalHistoryTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.external = self.service.state / 'external-history'

    def _write_receipt(self, unit=None, *, project=None, rows=None, mutate=None, filename=None, digest_id=True):
        unit = unit or self.model['units'][0]
        project = project or next(p for p in self.model['projects'] if p['project_id'] in unit['candidate_project_ids'])
        self.external.mkdir(mode=0o700, exist_ok=True)
        if rows is None:
            rows = []
            for source in unit['files']:
                target = project['path'] + '/外部归档/' + source['name']
                target_path = self.root / 'projects' / target
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes((self.root / 'staging' / source['source_path']).read_bytes())
                with directory(self.root / 'projects') as root_fd:
                    with subdirectory(root_fd, str(Path(target).parent).replace('\\', '/')) as parent_fd:
                        fd = open_file(parent_fd, target_path.name)
                        try:
                            target_signature = signature(fd)
                        finally:
                            os.close(fd)
                rows.append({'source_path': source['source_path'], 'name': source['name'],
                             'size_bytes': source['size_bytes'], 'blake3': source['blake3'],
                             'target_path': target, 'target_signature': target_signature})
        receipt = {'schema_version': 'external-archive-receipt/1',
                   'receipt_id': '', 'status': 'completed', 'example_data': True,
                   'roots': deepcopy(self.service.identity),
                   'completed_at': '2026-10-02T12:00:00+08:00', 'unit_id': unit['unit_id'],
                   'project': {'name': project['name'], 'path': project['path']}, 'files': rows}
        if mutate:
            mutate(receipt)
        unsigned = {k: v for k, v in receipt.items() if k != 'receipt_id'}
        receipt['receipt_id'] = digest(unsigned) if digest_id else '0' * 64
        name = filename or ('external-' + receipt['receipt_id'] + '.json')
        atomic_json(self.external / name, receipt)
        return receipt, self.external / name

    @staticmethod
    def _tree(root):
        result = {}
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            value = (stat.S_IFMT(info.st_mode), info.st_ino, info.st_nlink,
                     info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if stat.S_ISREG(info.st_mode):
                value += (path.read_bytes(),)
            result[relative] = value
        return result

    def test_happy_path_is_readonly_and_projects_existing_files(self):
        receipt, _ = self._write_receipt()
        before = self._tree(self.root)
        state = self.service.history.snapshot(self.model)
        after = self._tree(self.root)
        self.assertEqual(before, after)
        item = next(row for row in state['archived_units'] if row['unit_id'] == receipt['unit_id'])
        self.assertEqual(item['job_id'], receipt['receipt_id'])
        self.assertEqual(item['mode'], 'copy')
        self.assertEqual(item['origin'], 'existing_project_files')
        self.assertTrue(item['in_current_model'])
        self.assertEqual(item['file_count'], len(receipt['files']))
        self.assertFalse(state['warnings'])

    def test_absent_directory_has_no_effect_or_creation(self):
        self.assertFalse(self.external.exists())
        before = self._tree(self.root)
        self.assertEqual(self.service.history.snapshot(self.model)['archived_units'], [])
        self.assertFalse(self.external.exists())
        self.assertEqual(before, self._tree(self.root))

    def test_bad_digest_filename_roots_mode_and_file_content_are_pending(self):
        cases = [
            ('external-' + 'f' * 64 + '.json', None, False),
            (None, lambda r: r.update(receipt_id='0' * 64), False),
            (None, lambda r: r.update(roots={}), True),
            (None, lambda r: r.update(example_data=False), True),
            (None, lambda r: r['files'][0].update(blake3='0' * 64), True),
            (None, lambda r: r['files'][0].update(size_bytes=r['files'][0]['size_bytes'] + 1), True),
            (None, lambda r: r['files'][0].update(name='DCIM/OTHER.JPG'), True),
        ]
        for index, (filename, mutate, digest_id) in enumerate(cases):
            with self.subTest(filename=filename, mutate=mutate):
                self._write_receipt(mutate=mutate, filename=filename, digest_id=digest_id)
                state = self.service.history.snapshot(self.model)
                if index == 4:
                    self.assertEqual(len(state['archived_units']), 1)
                    self.assertFalse(state['archived_units'][0]['in_current_model'])
                else:
                    self.assertEqual(state['archived_units'], [])
                self.assertEqual(state['warnings'], ['既有归档记录无法核对，相关素材仍保留待处理；请刷新归档状态或查看核验报告。'])
                for path in self.external.glob('*'):
                    path.unlink()

    def test_changed_or_missing_target_is_pending(self):
        receipt, _ = self._write_receipt()
        target = self.root / 'projects' / receipt['files'][0]['target_path']
        target.write_bytes(b'X' * target.stat().st_size)
        state = self.service.history.snapshot(self.model)
        self.assertEqual(state['archived_units'], [])
        self.assertTrue(state['warnings'])
        target.unlink()
        state = self.service.history.snapshot(self.model)
        self.assertEqual(state['archived_units'], [])
        self.assertTrue(state['warnings'])

    def test_replaced_projects_root_is_pending(self):
        receipt, _ = self._write_receipt()
        original = self.root / 'projects'
        moved = self.root / 'projects-before-replacement'
        original.rename(moved)
        original.mkdir(mode=0o700)
        state = self.service.history.snapshot(self.model)
        self.assertEqual(state['archived_units'], [])
        self.assertEqual(state['warnings'], ['既有归档记录无法核对，相关素材仍保留待处理；请刷新归档状态或查看核验报告。'])

    def test_symlink_hardlink_and_traversal_are_rejected(self):
        receipt, path = self._write_receipt()
        target = self.root / 'projects' / receipt['files'][0]['target_path']
        original = target.read_bytes()
        target.unlink()
        target.symlink_to(self.root / 'staging' / receipt['files'][0]['source_path'])
        self.assertTrue(self.service.history.snapshot(self.model)['warnings'])
        path.unlink()
        target.unlink()

        receipt, path = self._write_receipt()
        target = self.root / 'projects' / receipt['files'][0]['target_path']
        target.unlink()
        target.write_bytes(original)
        os.link(target, target.with_name('hardlink-copy'))
        self.assertTrue(self.service.history.snapshot(self.model)['warnings'])
        path.unlink()

        def traversal(r):
            row = r['files'][0]
            row['target_path'] = r['project']['path'] + '/../outside/' + Path(row['name']).name

        self._write_receipt(mutate=traversal)
        state = self.service.history.snapshot(self.model)
        self.assertEqual(state['archived_units'], [])
        self.assertTrue(state['warnings'])

    def test_partial_file_set_does_not_match_current_unit(self):
        unit = self.model['units'][0]
        rows = []
        self._write_receipt(unit, rows=rows)  # establish the directory
        for path in self.external.glob('*'):
            path.unlink()
        self._write_receipt(unit, rows=None, mutate=lambda r: r.update(files=r['files'][:1]))
        state = self.service.history.snapshot(self.model)
        item = next(row for row in state['archived_units'] if row['unit_id'] == unit['unit_id'])
        self.assertFalse(item['in_current_model'])
        self.assertEqual(item['file_count'], 1)

    def test_conflicting_external_receipts_for_one_unit_are_not_projected(self):
        unit = self.model['units'][0]
        self._write_receipt(unit)
        other_project = self.model['projects'][1]
        self._write_receipt(unit, project=other_project)
        state = self.service.history.snapshot(self.model)
        self.assertFalse(any(row['unit_id'] == unit['unit_id'] for row in state['archived_units']))
        self.assertEqual(state['warnings'], ['既有归档记录无法核对，相关素材仍保留待处理；请刷新归档状态或查看核验报告。'])

    def test_existing_service_receipt_wins_without_duplicate(self):
        before = self.service.preflight(self.decisions)
        job = self.service.submit(before['preview_id'], self.decisions, True)
        done = self.service.run_job(job['job_id'])
        uid = done['outcomes'][0]['unit_id']
        service_receipt = json.loads((self.service.state / 'units' / done['outcomes'][0]['receipt']).read_text())
        rows = [{k: row[k] for k in ('source_path', 'name', 'size_bytes', 'blake3', 'target_path', 'target_signature')}
                for row in service_receipt['files']]
        self._write_receipt(next(u for u in self.model['units'] if u['unit_id'] == uid), rows=rows)
        state = self.service.history.snapshot(self.model)
        matches = [row for row in state['archived_units'] if row['unit_id'] == uid]
        self.assertEqual(len(matches), 1)
        self.assertNotIn('origin', matches[0])
        self.assertEqual(matches[0]['job_id'], done['job_id'])


if __name__ == '__main__':
    unittest.main()
