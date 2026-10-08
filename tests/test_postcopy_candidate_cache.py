"""Cached evidence still reopens current paths; changed receipts are never trusted."""
import json
import os
import unittest
from unittest.mock import patch
from test_postcopy_receipt import PostcopyFixture
from muli_sorter.archive_source import verify_sources
from muli_sorter.postcopy_receipt import _candidate_signature as candidate_signature, PostcopyError


class CandidateCacheTests(PostcopyFixture, unittest.TestCase):
    def signature(self, cache):
        return candidate_signature(self.root/'staging', self.batch_id,
                                   manifest=self.manifest, cache=cache)

    def test_same_pass_reuses_receipt_bytes_but_reopens_file(self):
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS':str(self.postcopy)}):
            cache={};expected=self.signature(cache)
            with patch('muli_sorter.postcopy_receipt.os.read', side_effect=AssertionError('repeat full read')):
                with patch('muli_sorter.postcopy_receipt.os.open', wraps=os.open) as opened:
                    self.assertEqual(self.signature(cache),expected)
                    self.assertGreater(opened.call_count,1)
            with patch('muli_sorter.postcopy_receipt.os.read', wraps=os.read) as reads:
                self.assertEqual(self.signature({}),expected)
                self.assertGreater(reads.call_count,0)

    def test_same_size_rewrite_with_restored_mtime_invalidates_cache(self):
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS':str(self.postcopy)}):
            cache={};before=self.signature(cache);p=self.postcopy/(self.batch_id+'.json');st=p.stat()
            raw=p.read_bytes();changed=raw.replace(b'completed',b'invalidxx',1);self.assertEqual(len(raw),len(changed));p.write_bytes(changed);os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))
            self.assertNotEqual(self.signature(cache),before)
            with self.assertRaises(ValueError):
                verify_sources(self.root/'staging',self.model['units'][0],self.runtime_snapshot,cache=cache)

    def test_warm_signature_change_between_stat_checks_is_rejected(self):
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS':str(self.postcopy)}):
            cache={};self.signature(cache);p=self.postcopy/(self.batch_id+'.json');inode=p.stat().st_ino
            original=os.fstat;seen=[]
            def changing(fd):
                info=original(fd)
                if info.st_ino==inode:
                    seen.append(1)
                    if len(seen)==2:
                        p.write_bytes(p.read_bytes()+b' ')
                        return original(fd)
                return info
            with patch('muli_sorter.postcopy_receipt.os.fstat',side_effect=changing):
                with self.assertRaisesRegex(PostcopyError,'发生变化'):self.signature(cache)

    def test_public_candidate_signature_keeps_fresh_manifest_contract(self):
        from muli_sorter.postcopy_receipt import candidate_signature as public_signature
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS':str(self.postcopy)}):
            self.assertIsNotNone(public_signature(self.root/'staging',self.batch_id))
            (self.batch/'ingest_manifest.json').write_text('{}')
            self.assertIsNone(public_signature(self.root/'staging',self.batch_id))

    def test_batch_digest_is_once_but_all_media_and_runtime_checks_remain(self):
        from muli_sorter import archive_source
        cache={};manifests=[];original=archive_source.digest
        def measured(value):
            if isinstance(value,dict) and 'batch' in value and 'files' in value:
                manifests.append(1)
            return original(value)
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS':str(self.postcopy)}):
            with patch.object(archive_source,'digest',side_effect=measured), patch.object(archive_source,'media_stat',wraps=archive_source.media_stat) as media, patch.object(archive_source,'runtime_check',wraps=archive_source.runtime_check) as runtime:
                for unit in self.model['units']:
                    verify_sources(self.root/'staging',unit,self.runtime_snapshot,cache=cache)
                self.assertEqual(len(manifests),1)
                self.assertEqual(media.call_count,sum(len(u['files']) for u in self.model['units']))
                self.assertEqual(runtime.call_count,len(self.model['units']))

    def test_cached_receipt_symlink_and_missing_file_are_not_reused(self):
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS':str(self.postcopy)}):
            cache={};self.signature(cache);p=self.postcopy/(self.batch_id+'.json');saved=p.with_suffix('.saved');p.rename(saved)
            self.assertIsNone(self.signature(cache));p.symlink_to(saved)
            with self.assertRaises(PostcopyError):self.signature(cache)

    def test_cached_root_replacement_and_wrong_manifest_are_rejected(self):
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS':str(self.postcopy)}):
            cache={};before=self.signature(cache);saved=self.postcopy.with_name('saved-receipts');self.postcopy.rename(saved);self.postcopy.mkdir()
            os.link(saved/(self.batch_id+'.json'),self.postcopy/(self.batch_id+'.json'))
            self.assertNotEqual(self.signature(cache),before)
            with self.assertRaises(PostcopyError):candidate_signature(self.root/'staging',self.batch_id,manifest={'batch':{'batch_id':'different'}},cache=cache)

    def test_warm_source_pass_reuses_manifest_and_receipt_content_only(self):
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS':str(self.postcopy)}):
            cache={};unit=self.model['units'][0]
            before=verify_sources(self.root/'staging',unit,self.runtime_snapshot,cache=cache)
            with patch('muli_sorter.postcopy_receipt.os.read',side_effect=AssertionError('repeat evidence read')):
                with patch('muli_sorter.archive_source.media_stat',wraps=__import__('muli_sorter.archive_source',fromlist=['media_stat']).media_stat) as checked:
                    self.assertEqual(verify_sources(self.root/'staging',unit,self.runtime_snapshot,cache=cache),before)
                    self.assertEqual(checked.call_count,len(unit['files']))
            changed=self.runtime_snapshot();changed['batches'][0]['state']='COPYING'
            with self.assertRaises(ValueError):verify_sources(self.root/'staging',unit,lambda:changed,cache=cache)
