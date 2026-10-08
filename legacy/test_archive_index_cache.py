import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from muli_sorter.archive import recover_index
from muli_sorter.archive_index_cache import recover_index_cached, NAME
from muli_sorter.archive_io import directory, atomic_json, ArchiveError
from muli_sorter.review import digest


class IndexCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name).resolve()
        self.context = directory(self.path)
        self.fd = self.context.__enter__()
        self.roots = {'test': 'synthetic'}

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.tmp.cleanup()

    def intent(self, uid, **extra):
        identity = {'unit_id': uid, 'files': [], **extra}
        jid = digest(identity)
        atomic_json(self.fd, 'job-'+jid+'.json', {'identity': identity, 'state': 'in_progress'})
        return jid

    def cached(self):
        stats = []
        result = recover_index_cached(self.fd, self.roots, progress=lambda **v: stats.append(v))
        return result, stats[-1] if stats else {}

    def test_unchanged_intents_reused_new_intent_only_parsed_and_result_equal(self):
        self.intent('one');self.intent('two')
        result, first = self.cached()
        self.assertEqual(first['parsed_records'], 2)
        self.assertEqual(result, recover_index(self.fd))
        _, second = self.cached();self.assertEqual(second['parsed_records'],0)
        self.intent('three')
        result, added = self.cached()
        self.assertEqual(added['parsed_records'],1)
        self.assertEqual(added['reused_records'],2)
        self.assertEqual(result, recover_index(self.fd))

    def test_same_size_in_place_change_ctime_rechecks_and_conflict_is_not_hidden(self):
        jid = self.intent('one');self.cached()
        p=self.path/('job-'+jid+'.json');before=p.stat()
        p.write_text(p.read_text().replace('one','two'))
        os.utime(p,ns=(before.st_atime_ns,before.st_mtime_ns))
        with self.assertRaises(ArchiveError):self.cached()

    def test_duplicate_uid_or_index_conflict_still_fails_with_cache(self):
        self.intent('one');self.cached();self.intent('one', project='other')
        with self.assertRaises(ArchiveError):self.cached()
        with self.assertRaises(ArchiveError):recover_index(self.fd)

    def test_missing_index_rebuilt_from_intents_and_deletion_matches_legacy(self):
        jid=self.intent('one');self.cached()
        (self.path/'archive-index.json').unlink()
        result,_=self.cached();self.assertEqual(result,{'one':jid})
        (self.path/('job-'+jid+'.json')).unlink()
        self.assertEqual(self.cached()[0], recover_index(self.fd))

    def test_bad_cache_and_changed_binding_force_full_parse(self):
        self.intent('one');self.cached()
        (self.path/NAME).write_text('{broken')
        self.assertEqual(self.cached()[1]['parsed_records'],1)
        self.roots={'changed':'identity'}
        self.assertEqual(self.cached()[1]['parsed_records'],1)

    def test_symlink_or_hardlink_cannot_reuse_cache(self):
        jid=self.intent('one');self.cached();p=self.path/('job-'+jid+'.json')
        q=self.path/'elsewhere';p.rename(q);p.symlink_to(q)
        with self.assertRaises((ArchiveError,OSError)):self.cached()
        p.unlink();q.rename(p);os.link(p,self.path/'extra-link')
        with self.assertRaises(ArchiveError):self.cached()

    def test_changed_during_validation_never_saves_partial_cache(self):
        jid=self.intent('one');p=self.path/('job-'+jid+'.json')
        def mutate(**_):p.write_text(p.read_text()+' ')
        with self.assertRaises(ArchiveError):recover_index_cached(self.fd,self.roots,progress=mutate)
        self.assertFalse((self.path/NAME).exists())

    def test_cache_write_failure_does_not_hide_completed_validation(self):
        jid=self.intent('one')
        from muli_sorter.archive_index_cache import atomic_json as actual
        def fail(fd,name,value):
            if name==NAME:raise OSError('cache disk failure')
            actual(fd,name,value)
        with patch('muli_sorter.archive_index_cache.atomic_json',fail):
            self.assertEqual(self.cached()[0],{'one':jid})
