from copy import deepcopy
import json
from pathlib import PurePosixPath
import unittest
from blake3 import blake3

from muli_sorter.archive_fixture import save_manifest
from muli_sorter.archive_io import ArchiveError
from muli_sorter.archive_layout import studio_target_rows
from muli_sorter.review import digest
from test_archive_jobs import Fixture


class ReservationTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.decisions['archive_options'] = {'mode': 'copy', 'existing': 'skip_identical'}

    def _base_assignment(self):
        segment = next(s for s in self.decisions['segments'] if s['decision'] == 'confirmed')
        unit = next(u for u in self.model['units'] if u['unit_id'] == segment['unit_ids'][0])
        project = next(p for p in self.model['projects'] if p['project_id'] == segment['project_id'])
        return segment, unit, project

    def _add_duplicate_source_unit(self, *, keep_other_confirmed=False, different=False):
        segment, source_unit, _ = self._base_assignment()
        batch_id = source_unit['files'][0]['source_path'].split('/', 1)[0]
        manifest_path = self.root / 'staging' / batch_id / 'ingest_manifest.json'
        manifest = json.loads(manifest_path.read_text())
        duplicate_files = []
        for number, original in enumerate(source_unit['files']):
            source = self.root / 'staging' / original['source_path']
            relative_name = PurePosixPath(original['name'])
            duplicate_name = PurePosixPath('DUPLICATE') / relative_name
            duplicate_source = self.root / 'staging' / batch_id / 'SOURCE_DATA' / duplicate_name
            duplicate_source.parent.mkdir(parents=True, exist_ok=True)
            data = source.read_bytes()
            if different:
                data = bytes([data[0] ^ 1]) + data[1:]
            duplicate_source.write_bytes(data)

            record = next(item for item in manifest['files'] if item['relative_path'] == original['name'])
            duplicate_record = deepcopy(record)
            duplicate_record['file_id'] = f'duplicate-{number}'
            duplicate_record['relative_path'] = duplicate_name.as_posix()
            duplicate_record['destination_relative_path'] = 'SOURCE_DATA/' + duplicate_name.as_posix()
            duplicate_record['hash']['source'] = duplicate_record['hash']['destination'] = blake3(data).hexdigest()
            manifest['files'].append(duplicate_record)
            duplicate_files.append({
                **original,
                'source_path': f'{batch_id}/{duplicate_record["destination_relative_path"]}',
                'name': duplicate_record['relative_path'],
                'blake3': duplicate_record['hash']['source'],
            })

        manifest['summary']['selected_file_count'] += len(duplicate_files)
        manifest['summary']['verified_file_count'] += len(duplicate_files)
        manifest['summary']['selected_bytes'] += sum(f['size_bytes'] for f in duplicate_files)
        save_manifest(self.root, manifest)

        duplicate = deepcopy(source_unit)
        duplicate['files'] = duplicate_files
        duplicate['file_names'] = [f['name'] for f in duplicate_files]
        duplicate['bytes'] = sum(f['size_bytes'] for f in duplicate_files)
        duplicate['unit_id'] = 'unit-' + digest(sorted(duplicate_files, key=lambda f: f['source_path']))[:24]
        self.model['units'].append(duplicate)
        segment['unit_ids'].append(duplicate['unit_id'])
        if not keep_other_confirmed:
            for other in self.decisions['segments']:
                if other is not segment and other['decision'] == 'confirmed':
                    other['decision'] = 'deferred'
        self.model['report_id'] = 'sha256:' + digest({k: v for k, v in self.model.items() if k != 'report_id'})
        self.decisions['report_id'] = self.model['report_id']
        return source_unit, duplicate

    def _write_reservations(self, reservations):
        (self.root / 'console-state' / 'reservations.json').write_text(json.dumps(reservations))

    def _write_reservation(self, source_path, target_path, content_hash):
        self._write_reservations({source_path: {'target_path': target_path, 'blake3': content_hash}})

    def _base_target(self):
        _, unit, project = self._base_assignment()
        return studio_target_rows(unit, project)[0]

    def test_identical_sources_share_reserved_targets_copy_once_and_keep_both_sources(self):
        original, duplicate = self._add_duplicate_source_unit()

        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview['status'], 'ready', preview)
        self.assertEqual(preview['summary']['files'], 4)
        self.assertEqual(preview['summary']['skipped_files'], 2)
        job = self.service.submit(preview['preview_id'], self.decisions, True)
        self.service.close()
        self.service = self.make_service()
        done = self.service.run_job(job['job_id'])

        self.assertEqual(done['status'], 'completed', done)
        self.assertEqual(sorted(o['skipped_files'] for o in done['outcomes']), [0, 2])
        self.assertEqual(done['summary']['completed_files'], 4)
        for unit in (original, duplicate):
            for row in unit['files']:
                self.assertTrue((self.root / 'staging' / row['source_path']).is_file())
        _, _, project = self._base_assignment()
        for row in studio_target_rows(original, project):
            target = self.root / 'projects' / row['target_path']
            self.assertTrue(target.is_file())
            self.assertEqual(blake3(target.read_bytes()).hexdigest(), row['blake3'])
            self.assertFalse(target.with_stem(target.stem + '_1').exists())

    def test_same_name_same_size_different_sources_keep_both_contents_with_suffix(self):
        original, duplicate = self._add_duplicate_source_unit(different=True)
        job = self.submit()
        self.assertEqual(job['summary']['renamed_files'], 2)
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        self.assertEqual(done['summary']['skipped_files'], 0)
        for unit in (original, duplicate):
            for row in unit['files']:
                target = self.root / 'projects' / job['file_plans'][row['source_path']]['target_path']
                self.assertEqual(target.read_bytes(), (self.root / 'staging' / row['source_path']).read_bytes())

    def test_identical_sources_move_once_and_cleanup_both_sources_after_verification(self):
        original, duplicate = self._add_duplicate_source_unit()
        self.service.move_enabled = True
        self.decisions['archive_options']['mode'] = 'move'

        job = self.submit()
        done = self.service.run_job(job['job_id'])

        self.assertEqual(done['status'], 'completed', done)
        self.assertEqual(done['summary']['removed_sources'], 4)
        self.assertEqual(sorted(o['skipped_files'] for o in done['outcomes']), [0, 2])
        for unit in (original, duplicate):
            for row in unit['files']:
                self.assertFalse((self.root / 'staging' / row['source_path']).exists())
        _, _, project = self._base_assignment()
        self.assertTrue(all((self.root / 'projects' / row['target_path']).is_file()
                            for row in studio_target_rows(original, project)))

    def test_failed_duplicate_task_retries_after_service_restart(self):
        self._add_duplicate_source_unit(keep_other_confirmed=True)
        job = self.submit()
        interrupted = [False]

        def stop_once(phase, row):
            if phase == 'copy_chunk' and '/2视频素材/' in row['target_path'] and not interrupted[0]:
                interrupted[0] = True
                raise ArchiveError('synthetic interruption')

        self.service.checkpoint = stop_once
        partial = self.service.run_job(job['job_id'])
        self.assertEqual(partial['status'], 'partial', partial)

        self.service.close()
        self.service = self.make_service()
        self.service.retry(job['job_id'], True)
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        self.assertGreater(sum(o['resumed_bytes'] for o in done['outcomes']), 0)

    def test_external_reservation_uses_numbered_target(self):
        row = self._base_target()
        external = 'OTHER/BATCH/source.jpg'
        self._write_reservation(external, row['target_path'], 'f' * 64)

        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview['status'], 'ready', preview)
        self.assertEqual(preview['summary']['renamed_files'], 1)
        job = self.service.submit(preview['preview_id'], self.decisions, True)
        planned = job['file_plans'][row['source_path']]
        self.assertEqual(planned['target_name'], 'A_1.JPG')
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        self.assertTrue((self.root / 'projects' / planned['target_path']).is_file())

    def test_external_reservation_blocks_when_existing_policy_is_error(self):
        row = self._base_target()
        self.decisions['archive_options']['existing'] = 'error'
        self._write_reservation('OTHER/BATCH/source.jpg', row['target_path'], 'f' * 64)

        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview['status'], 'blocked', preview)
        self.assertIn('占用', preview['errors'][0])

    def test_casefolded_shared_reservations_require_exact_target_paths(self):
        original, duplicate = self._add_duplicate_source_unit()
        _, _, project = self._base_assignment()
        row = studio_target_rows(original, project)[0]
        duplicate_row = studio_target_rows(duplicate, project)[0]
        lower_target = str(PurePosixPath(row['target_path']).with_name(row['target_name'].lower()))
        self._write_reservations({
            row['source_path']: {'target_path': row['target_path'], 'blake3': row['blake3']},
            duplicate_row['source_path']: {'target_path': lower_target, 'blake3': duplicate_row['blake3']},
        })

        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview['status'], 'blocked', preview)

    def test_reserved_source_with_different_content_is_not_reused(self):
        row = self._base_target()
        self._write_reservation(row['source_path'], row['target_path'], '0' * 64)

        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview['status'], 'blocked', preview)

    def test_own_reservation_does_not_hide_conflicting_other_owner_hash(self):
        row = self._base_target()
        self._write_reservations({
            row['source_path']: {'target_path': row['target_path'], 'blake3': row['blake3']},
            'OTHER/BATCH/source.jpg': {'target_path': row['target_path'], 'blake3': 'f' * 64},
        })
        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview['status'], 'blocked', preview)
        self.assertIn('占用', preview['errors'][0])


if __name__ == '__main__':
    unittest.main()
