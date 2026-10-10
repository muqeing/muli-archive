import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from blake3 import blake3
from muli_sorter.archive_direct_move import _checked
from muli_sorter.archive_io import ArchiveError, hash_fd


class SourceAttributeMoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'synthetic.mp4'
        self.data = b'original bytes'*100
        self.path.write_bytes(self.data)
        os.chmod(self.path, 0o600)
        self.fd = os.open(self.tmp.name, os.O_RDONLY|os.O_DIRECTORY)
        self.proof = self.current()
        self.row = {'size_bytes':len(self.data), 'blake3':blake3(self.data).hexdigest()}

    def tearDown(self):
        os.close(self.fd)
        self.tmp.cleanup()

    def current(self):
        s=self.path.stat()
        return {k:getattr(s,'st_'+k) for k in ('dev','ino','size','mtime_ns','ctime_ns')}

    def touched(self):
        os.chmod(self.path, 0o640)
        self.assertNotEqual(self.current()['ctime_ns'], self.proof['ctime_ns'])

    def checked(self, **kwargs):
        return _checked(self.fd,self.path.name,self.row,content=True,
                        verified_source=self.proof,**kwargs)

    def test_unchanged_proof_does_not_read_content(self):
        with patch('muli_sorter.archive_direct_move.hash_fd',side_effect=AssertionError('extra read')):
            self.checked()

    def test_ctime_only_reads_one_file_and_returns_fresh_identity(self):
        self.touched();progress=[]
        with patch('muli_sorter.archive_direct_move.hash_fd',wraps=hash_fd) as reads:
            result=self.checked(progress=progress.append)
        self.assertEqual(reads.call_count,1)
        self.assertEqual(sum(progress),len(self.data))
        self.assertEqual(result[4],self.current()['ctime_ns'])
        self.assertNotEqual(result[4],self.proof['ctime_ns'])

    def test_same_size_edit_with_restored_mtime_is_rejected(self):
        old=self.path.stat();self.path.write_bytes(b'X'*len(self.data))
        os.utime(self.path,ns=(old.st_atime_ns,old.st_mtime_ns))
        with self.assertRaisesRegex(ArchiveError,'内容摘要'):
            self.checked()

    def test_mtime_change_is_not_adopted_even_with_same_content(self):
        old=self.path.stat();os.utime(self.path,ns=(old.st_atime_ns,old.st_mtime_ns+1000000))
        with patch('muli_sorter.archive_direct_move.hash_fd',side_effect=AssertionError('extra read')):
            with self.assertRaises(ArchiveError) as failed:self.checked()
        self.assertIn('mtime_ns',[r['field'] for r in failed.exception.archive_location['changed_signature_fields']])

    def test_replaced_inode_with_same_bytes_and_mtime_is_rejected(self):
        old=self.path.stat();other=self.path.with_name('replacement');other.write_bytes(self.data)
        os.utime(other,ns=(old.st_atime_ns,old.st_mtime_ns));os.replace(other,self.path)
        with self.assertRaisesRegex(ArchiveError,'来源文件身份'):self.checked()

    def test_second_attribute_change_during_hash_is_rejected(self):
        self.touched()
        def read(fd,**kwargs):
            def progress(n):os.chmod(self.path,0o600)
            return hash_fd(fd,progress=progress)
        with patch('muli_sorter.archive_direct_move.hash_fd',side_effect=read):
            with self.assertRaisesRegex(ArchiveError,'计算摘要'):self.checked()

    def test_path_replaced_during_hash_is_rejected(self):
        self.touched()
        def read(fd,**kwargs):
            result=hash_fd(fd,**kwargs)
            other=self.path.with_name('replacement');other.write_bytes(self.data);os.replace(other,self.path)
            return result
        with patch('muli_sorter.archive_direct_move.hash_fd',side_effect=read):
            with self.assertRaisesRegex(ArchiveError,'路径在核对'):self.checked()

    def test_extra_hardlink_is_rejected_without_content_read(self):
        os.link(self.path,self.path.with_name('extra'))
        with patch('muli_sorter.archive_direct_move.hash_fd',side_effect=AssertionError('extra read')):
            with self.assertRaises(ArchiveError):self.checked()

    def test_recorded_intent_identity_stays_strict_after_attribute_change(self):
        self.touched();fresh=self.checked();os.chmod(self.path,0o600)
        with self.assertRaisesRegex(ArchiveError,'身份、大小或链接'):
            _checked(self.fd,self.path.name,self.row,expected=fresh,content=True)
