import copy
import json
from pathlib import Path
import tempfile
import unittest

from muli_sorter.archive_direct_journal import DirectJournal
from muli_sorter.archive_io import ArchiveError, atomic_json, directory
from muli_sorter.review import digest


class DirectJournalValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name).resolve()
        self.job_id = 'job-direct-journal-validation'
        self.root_source = '/staging/one/IMG-0001.JPG'
        self.child_source = '/staging/two/IMG-0002.JPG'

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _signature(seed):
        return [11, 100 + seed, 4096 + seed, 1000 + seed, 2000 + seed]

    @staticmethod
    def _parent(seed):
        return [21, 200 + seed]

    def _entry(self, source_path, seed, state='pending'):
        return {
            'source_path': source_path,
            'source_signature': self._signature(seed),
            'source_parent': self._parent(seed),
            'target_parent': self._parent(seed + 100),
            'state': state,
        }

    def _journal(self, entries=None):
        return {
            'identity': {
                'job_id': self.job_id,
                'request_digest': 'request-digest',
                'roots': {'staging': '/staging', 'projects': '/projects'},
                'file_plans': [self.root_source, self.child_source],
                'strategy': 'direct_move',
            },
            'entry_storage': 'per-file/v1',
            'files': entries or [
                self._entry(self.root_source, 1),
                self._entry(self.child_source, 2),
            ],
        }

    def _entry_path(self, journal, source_path):
        return 'direct-entry-' + self.job_id + '-' + digest(source_path) + '.json'

    def _assert_rejected(self, journal, side_entries, message):
        name = 'direct-move-' + self.job_id + '.json'
        with directory(self.state) as fd:
            atomic_json(fd, name, journal)
            for source_path, envelope in side_entries.items():
                atomic_json(fd, self._entry_path(journal, source_path), envelope)
            with self.assertRaisesRegex(ArchiveError, '直接移动逐文件回执'):
                DirectJournal(fd, name, journal, recovering=True)

    def test_wrong_owner_side_entry_is_rejected(self):
        journal = self._journal()
        side = copy.deepcopy(journal['files'][0])
        side['state'] = 'renaming'
        self._assert_rejected(journal, {
            self.root_source: {'owner': 'wrong-owner', 'entry': side},
        }, 'wrong owner')

    def test_changed_fixed_identity_in_side_entry_is_rejected(self):
        journal = self._journal()
        side = copy.deepcopy(journal['files'][0])
        side['state'] = 'renaming'
        side['source_parent'] = self._parent(999)
        self._assert_rejected(journal, {
            self.root_source: {'owner': digest(journal['identity']), 'entry': side},
        }, 'changed fixed identity')

    def test_removed_side_entry_without_target_signature_is_rejected(self):
        journal = self._journal()
        side = copy.deepcopy(journal['files'][0])
        side['state'] = 'removed'
        self._assert_rejected(journal, {
            self.root_source: {'owner': digest(journal['identity']), 'entry': side},
        }, 'missing target signature')

    def test_removed_main_entry_cannot_fall_back_to_child_renaming(self):
        journal = self._journal()
        journal['files'][0]['state'] = 'removed'
        side = copy.deepcopy(journal['files'][0])
        side['state'] = 'renaming'
        self._assert_rejected(journal, {
            self.root_source: {'owner': digest(journal['identity']), 'entry': side},
        }, 'removed to renaming fallback')

    def test_valid_child_entry_overlays_main_entry(self):
        journal = self._journal()
        root_side = copy.deepcopy(journal['files'][0])
        root_side['state'] = 'renaming'
        child_side = copy.deepcopy(journal['files'][1])
        child_side['state'] = 'removed'
        child_side['target_signature'] = self._signature(202)
        with directory(self.state) as fd:
            name = 'direct-move-' + self.job_id + '.json'
            atomic_json(fd, name, journal)
            atomic_json(fd, self._entry_path(journal, self.root_source), {
                'owner': digest(journal['identity']),
                'entry': root_side,
            })
            atomic_json(fd, self._entry_path(journal, self.child_source), {
                'owner': digest(journal['identity']),
                'entry': child_side,
            })
            loaded = json.loads(Path(self.state / name).read_text())
            result = DirectJournal(fd, name, loaded, recovering=True)
            self.assertTrue(result.per_file)
            self.assertEqual(loaded['files'][0], root_side)
            self.assertEqual(loaded['files'][1], child_side)


if __name__ == '__main__':
    unittest.main()
