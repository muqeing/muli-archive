"""Discovery caching must not become media authorization or retain old models."""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from test_postcopy_receipt import PostcopyFixture
from muli_sorter import queue_evidence as evidence
from muli_sorter.discovery_queue import DiscoveryQueue
from muli_sorter.intake import EvidenceError
from muli_sorter.queue_evidence_cache import DiscoveryEvidenceCache


class EvidenceCacheTests(PostcopyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.cache = DiscoveryEvidenceCache(self.root/'staging', allow_examples=True)
        self.snapshot = self.runtime_snapshot()
        self.row = self.snapshot['batches'][0]

    def parts(self):
        return evidence.signal_parts(self.root/'staging', [self.batch_id], self.snapshot,
                                     allow_examples=True, cache=self.cache)

    def test_cold_pass_validates_once_and_matches_original_token(self):
        expected = evidence.signal_parts(self.root/'staging', [self.batch_id], self.snapshot,
                                         allow_examples=True)
        with patch.object(evidence, 'validate_record', wraps=evidence.validate_record) as checked:
            candidate = evidence.size_verified_candidate(self.root/'staging', self.row,
                allow_examples=True, cache=self.cache)
            self.assertIsNotNone(candidate)
            self.assertEqual(self.parts(), expected)
            self.assertEqual(checked.call_count, 1)
        entry = self.cache.entries[self.batch_id]
        self.assertEqual(set(entry['candidate']), {'receipt_signature'})
        self.assertLess(len(json.dumps(entry)), 2048)

    def test_warm_discovery_reads_no_manifest_or_receipt_body(self):
        expected = self.parts()
        with patch.object(evidence, 'validate_record', side_effect=AssertionError('full reread')), \
             patch.object(evidence, 'read_bytes', side_effect=AssertionError('receipt reread')), \
             patch('muli_sorter.postcopy_receipt._read_file', side_effect=AssertionError('postcopy reread')):
            for _ in range(5):
                self.assertIsNotNone(self.cache.candidate(self.row))
                self.assertEqual(self.parts(), expected)

    def test_same_size_rewrite_restored_mtime_is_not_trusted(self):
        self.parts()
        p = self.batch/'ingest_manifest.json'; before=p.stat(); raw=p.read_bytes()
        changed=raw.replace(b'COPY_SIZE_VERIFIED', b'X' * len(b'COPY_SIZE_VERIFIED'), 1)
        self.assertEqual(len(changed), len(raw));p.write_bytes(changed)
        os.utime(p, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertIsNone(self.cache.candidate(self.row))
        with self.assertRaises(EvidenceError):self.parts()

    def test_optional_corrections_and_renewal_publication_invalidate(self):
        cases=[(self.batch/'time_correction_receipt.json',{}),
               (self.batch/'photo_time_correction.json',{'status':'prepared'}),
               (self.postcopy/(self.batch_id+'.v2.json'),{})]
        for path, value in cases:
            with self.subTest(path=path.name):
                expected=self.parts();path.write_text(json.dumps(value))
                self.assertIsNone(self.cache.candidate(self.row))
                with self.assertRaises(EvidenceError):self.parts()
                path.unlink()
                self.assertEqual(self.parts(), expected)

    def test_missing_receipt_does_not_poison_later_completion(self):
        self.parts();p=self.postcopy/(self.batch_id+'.json');raw=p.read_bytes();p.unlink()
        self.assertIsNone(self.cache.candidate(self.row));self.assertNotIn(self.batch_id,self.cache.entries)
        p.write_bytes(raw)
        self.assertIsNotNone(self.cache.candidate(self.row))

    def test_warm_symlink_or_directory_is_rejected(self):
        self.parts();p=self.batch/'ingest_manifest.json';saved=p.with_suffix('.saved');p.rename(saved)
        p.symlink_to(saved)
        self.assertIsNone(self.cache.candidate(self.row))
        with self.assertRaises((EvidenceError,OSError)):self.parts()
        p.unlink();p.mkdir()
        self.assertIsNone(self.cache.candidate(self.row))

    def test_replaced_receipt_root_invalidates_even_with_same_file_inode(self):
        self.parts();old=self.cache.fingerprint(self.row);saved=self.postcopy.with_name('saved-receipts')
        self.postcopy.rename(saved);self.postcopy.mkdir()
        os.link(saved/(self.batch_id+'.json'),self.postcopy/(self.batch_id+'.json'))
        self.assertNotEqual(self.cache.fingerprint(self.row),old)
        self.assertIsNotNone(self.cache.candidate(self.row))  # Fresh full proof, never old hint.

    def test_revision_uid_completion_and_lease_boundaries(self):
        original=self.parts();before=self.cache.fingerprint(self.row)
        self.row['archive_handoff']={'status':'partial','expires_at':'unknown'}
        self.assertEqual(self.cache.fingerprint(self.row),before);self.assertEqual(self.parts(),original)
        for field,value in [('revision',2),('batch_uid','changed'),('completed_at','changed')]:
            with self.subTest(field=field):
                old=self.row[field];self.row[field]=value
                self.assertNotEqual(self.cache.fingerprint(self.row),before)
                self.row[field]=old
        self.row['state']='FINALIZING'
        self.assertIsNone(self.cache.candidate(self.row))
        with self.assertRaises(EvidenceError):self.parts()

    def test_candidate_cannot_be_mutated_through_return_value(self):
        expected=self.cache.candidate(self.row);value=self.cache.candidate(self.row)
        value['receipt_signature'][0][0]='poisoned'
        self.assertEqual(self.cache.candidate(self.row),expected)

    def test_cache_scope_cannot_cross_roots_or_example_mode(self):
        self.parts()
        for root,examples in [(self.root/'other-staging',True),(self.root/'staging',False)]:
            with self.subTest(root=root,examples=examples):
                with self.assertRaises(EvidenceError):
                    evidence.signal_parts(root,[self.batch_id],self.snapshot,
                                          allow_examples=examples,cache=self.cache)
                with self.assertRaises(EvidenceError):
                    evidence.size_verified_candidate(root,self.row,
                                                     allow_examples=examples,cache=self.cache)

    def test_warm_change_between_fences_is_rejected(self):
        self.parts();before=self.cache.fingerprint(self.row)
        with patch.object(self.cache,'fingerprint',side_effect=[before,'changed']):
            self.assertIsNone(self.cache.candidate(self.row))
        self.parts();before=self.cache.fingerprint(self.row)
        with patch.object(self.cache,'fingerprint',side_effect=[before,'changed']):
            with self.assertRaises(EvidenceError):self.parts()
        self.assertNotIn(self.batch_id,self.cache.entries)

    def test_lru_bound_and_prune_keep_only_compact_hints(self):
        cache=DiscoveryEvidenceCache(self.root/'staging',maximum_entries=2)
        for i in range(1000):cache._put(str(i),'fingerprint',None,'digest')
        self.assertEqual(list(cache.entries),['998','999'])
        cache.prune({'999'});self.assertEqual(list(cache.entries),['999'])
        cache.prune(set());self.assertFalse(cache.entries)

    def test_queue_warm_rounds_skip_full_validation_and_leave_artifacts_exact(self):
        provider=lambda:{**self.runtime_snapshot(),'complete_listing':True}
        with DiscoveryQueue(self.root/'staging',self.root/'projects',self.root/'queue',allow_examples=True) as queue:
            queue.tick(provider);before=deepcopy(queue.jobs()[0])
            with patch.object(evidence,'validate_record',side_effect=AssertionError('old manifest read')):
                for _ in range(3):queue.discover(provider)
            after=queue.jobs()[0]
            self.assertEqual(after['state'],'awaiting_confirmation')
            self.assertEqual(after['artifact'],before['artifact']);self.assertEqual(after['token'],before['token'])

    def test_build_and_acceptance_do_not_use_discovery_cache_as_authorization(self):
        self.parts()
        with patch.object(evidence,'validate_record',wraps=evidence.validate_record) as checked:
            evidence.build_job(self.root/'staging',self.batch_id,self.snapshot,[],allow_examples=True)
            self.assertGreaterEqual(checked.call_count,3)

    def test_ten_to_ten_thousand_retired_summaries_never_open_old_evidence(self):
        from muli_sorter.discovery_queue import RULES
        from muli_sorter.review import digest
        provider=lambda:{**self.runtime_snapshot(),'complete_listing':True}
        with DiscoveryQueue(self.root/'staging',self.root/'projects',self.root/'queue',
                            allow_examples=True,enable_lifecycle=True) as queue:
            queue.tick(provider)
            current=deepcopy(self.row);rows=[]
            fingerprint=queue.evidence_cache.fingerprint
            def only_active(row):
                self.assertEqual(row['batch_id'],current['batch_id'])
                return fingerprint(row)
            # The existing source snapshot caps ALL rows at 10,000. Phase one
            # avoids old evidence I/O; it does not claim incremental SQL yet.
            for total in (10,100,1000,10000):
                for i in range(len(rows),total-1):
                    row={**current,'batch_id':f'BATCH_20260927_{i:06}', 'batch_uid':f'retired-{i}'}
                    rows.append(row);jid=digest([row['batch_uid'],row['revision'],RULES])
                    queue.db.execute("INSERT INTO jobs(id,batch_id,uid,revision,state,first_seen) VALUES(?,?,?,?,'closed',0)",
                                     (jid,row['batch_id'],row['batch_uid'],row['revision']))
                queue.db.commit()
                def snapshot():return {**provider(),'batches':[current,*rows]}
                with patch.object(queue.evidence_cache,'fingerprint',side_effect=only_active), \
                     patch.object(evidence,'validate_record',side_effect=AssertionError('old body read')):
                    _,status=queue.discover(snapshot)
                self.assertEqual(len(status['jobs']),1)
                self.assertEqual(status['jobs'][0]['state'],'awaiting_confirmation')
