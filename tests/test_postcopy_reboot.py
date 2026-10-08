"""Durable identity, explicit full-read legacy renewal, and fail-closed admission."""
from copy import deepcopy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from blake3 import blake3
from test_postcopy_receipt import PostcopyFixture
from muli_sorter.archive_source import verify_sources
from muli_sorter.intake import validate_record
from muli_sorter.postcopy_receipt import source_signature, _read
from muli_sorter.postcopy_service import Verifier
from muli_sorter.staging_coordination import staging_guard


class RebootReceiptTests(PostcopyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)})
        self.env.start(); self.addCleanup(self.env.stop)
        with staging_guard(self.root/'staging', create=True):
            pass
        self.verifier = Verifier(self.root/'staging', self.postcopy,
            lambda: {**self.runtime_snapshot(), 'complete_listing': True}, allow_examples=True)
        self.original = self.postcopy/(self.batch_id+'.json')
        self.renewal = self.postcopy/(self.batch_id+'.v2.json')

    def v2(self):
        receipt = deepcopy(self.receipt); receipt['schema'] = 'postcopy-verification/2'
        for row in receipt['files']:
            fd = os.open(self.root/'staging'/row['resolved_path'], os.O_RDONLY)
            try: row['source_signature'] = source_signature(fd, receipt['schema'])
            finally: os.close(fd)
        self._write_receipt(receipt)
        return receipt

    def source(self):
        return self.root/'staging'/self.receipt['files'][0]['resolved_path']

    def test_v2_reboot_device_change_does_not_require_content_reread(self):
        self.v2()
        original = os.fstat; inode = self.source().stat().st_ino
        def remounted(fd):
            info = original(fd)
            if info.st_ino != inode: return info
            return SimpleNamespace(**{
                **{key:getattr(info,key) for key in dir(info) if key.startswith('st_')},
                'st_dev': info.st_dev + 2})
        with patch('muli_sorter.postcopy_receipt.os.fstat', side_effect=remounted):
            proofs = {}
            verify_sources(self.root/'staging', self.model['units'][0], self.runtime_snapshot,
                           validated_signatures=proofs)
            self.assertEqual(proofs[str(self.source().relative_to(self.root/'staging'))]['dev'],
                             self.source().stat().st_dev + 2)

    def test_v2_different_filesystem_with_identical_other_fields_is_rejected(self):
        self.v2()
        from muli_sorter.postcopy_receipt import filesystem_id
        def different(fd):
            return filesystem_id(fd) + 1
        with patch('muli_sorter.postcopy_receipt.filesystem_id', side_effect=different):
            with self.assertRaisesRegex(ValueError, '签名不一致'):
                verify_sources(self.root/'staging', self.model['units'][0], self.runtime_snapshot)

    def test_v2_same_size_rewrite_restoring_mtime_is_rejected(self):
        self.v2(); source=self.source(); before=source.stat()
        source.write_bytes(b'x'*before.st_size)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, '签名不一致'):
            verify_sources(self.root/'staging', self.model['units'][0], self.runtime_snapshot)

    def test_legacy_remount_is_explained_and_never_silently_accepted(self):
        changed=deepcopy(self.receipt)
        for row in changed['files']: row['source_signature']['dev'] += 2
        self._write_receipt(changed)
        with self.assertRaisesRegex(ValueError, 'NAS 重启.*一次独立续验'):
            verify_sources(self.root/'staging', self.model['units'][0], self.runtime_snapshot)

    def test_explicit_renewal_reads_every_byte_preserves_old_and_grants_v2(self):
        before=self.original.read_bytes(); digest=blake3(before).hexdigest(); chunks=[]
        renewed=self.verifier.verify(self.batch_id, renew_legacy_digest=digest,
            checkpoint=lambda event,row: chunks.append(row['resolved_path']) if event=='hash_chunk' else None)
        self.assertEqual(before, self.original.read_bytes())
        self.assertEqual(set(chunks), {row['resolved_path'] for row in self.receipt['files']})
        self.assertEqual(renewed['previous_receipt_blake3'],digest)
        self.assertEqual(validate_record(self.root/'staging',self.batch_id,allow_examples=True)
                         ['_postcopy_evidence']['schema'],'postcopy-verification/2')
        verify_sources(self.root/'staging',self.model['units'][0],self.runtime_snapshot)
        with self.assertRaisesRegex(ValueError,'不覆盖'):
            self.verifier.verify(self.batch_id, renew_legacy_digest=digest)

    def test_bad_content_does_not_publish_renewal(self):
        before=self.original.read_bytes();source=self.source()
        source.write_bytes(b'x'*source.stat().st_size)
        with self.assertRaisesRegex(ValueError,'内容或身份'):
            self.verifier.verify(self.batch_id, renew_legacy_digest=blake3(before).hexdigest())
        self.assertFalse(self.renewal.exists());self.assertEqual(before,self.original.read_bytes())

    def test_missing_archived_source_does_not_forge_complete_renewal(self):
        digest=blake3(self.original.read_bytes()).hexdigest();self.source().unlink()
        with self.assertRaises(FileNotFoundError):self.verifier.verify(self.batch_id,renew_legacy_digest=digest)
        self.assertFalse(self.renewal.exists())

    def test_wrong_original_digest_fails_before_content_read(self):
        with patch('muli_sorter.postcopy_service.attrs', side_effect=AssertionError('media read')):
            with self.assertRaisesRegex(ValueError,'绑定明确'):
                self.verifier.verify(self.batch_id,renew_legacy_digest='0'*64)
        self.assertFalse(self.renewal.exists())

    def test_original_change_invalidates_renewal_and_warm_cache(self):
        digest=blake3(self.original.read_bytes()).hexdigest()
        self.verifier.verify(self.batch_id,renew_legacy_digest=digest)
        cache={}
        _read(self.root/'staging',self.batch_id,required=True,cache=cache)
        self.original.write_bytes(self.original.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'保留的原始回执不一致'):
            _read(self.root/'staging',self.batch_id,required=True,cache=cache)
        with self.assertRaisesRegex(ValueError,'保留的原始回执不一致'):
            validate_record(self.root/'staging',self.batch_id,allow_examples=True)

    def test_orphan_symlink_and_zero_filesystem_are_rejected(self):
        self.v2();bad=json.loads(self.original.read_bytes())
        bad['files'][0]['source_signature']['fsid']=0;self._write_receipt(bad)
        with self.assertRaisesRegex(ValueError,'文件系统身份'):
            validate_record(self.root/'staging',self.batch_id,allow_examples=True)
        self.renewal.symlink_to(self.original)
        with self.assertRaises(ValueError):_read(self.root/'staging',self.batch_id,required=True)

    def test_filesystem_identity_unavailable_never_falls_back_to_inode(self):
        fd=os.open(self.source(),os.O_RDONLY)
        try:
            for fsid in (None, 0, 2**64):
                with patch('muli_sorter.postcopy_receipt.os.fstatvfs',return_value=SimpleNamespace(f_fsid=fsid)):
                    with self.assertRaisesRegex(ValueError,'稳定标识'):
                        source_signature(fd,'postcopy-verification/2')
        finally:os.close(fd)


if __name__ == '__main__':unittest.main()
