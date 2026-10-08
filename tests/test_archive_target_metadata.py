import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from blake3 import blake3
from muli_sorter.archive_io import ArchiveError, signature, hash_fd, verified_target_hash
from test_archive_jobs import Fixture

class TargetMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'media'
        self.data=b'original media'*200000;self.path.write_bytes(self.data)
        self.fd=os.open(self.path,os.O_RDONLY);self.expected=blake3(self.data).hexdigest()
    def tearDown(self):os.close(self.fd);self.tmp.cleanup()
    def mutate_on_read(self,callback,repeated=False):
        read=os.read;count=[0]
        def injected(fd,size):
            if fd==self.fd and os.lseek(fd,0,os.SEEK_CUR)==0 and (repeated or not count[0]):
                count[0]+=1;callback(count[0])
            return read(fd,size)
        return patch('muli_sorter.archive_io.os.read',side_effect=injected)
    def test_target_ctime_only_during_hash_gets_one_complete_stable_reread(self):
        before=signature(self.fd)
        with self.mutate_on_read(lambda n:os.chmod(self.path,0o640)):
            result=verified_target_hash(self.fd,self.expected)
        self.assertEqual(result,signature(self.fd));self.assertEqual(before[:4],result[:4]);self.assertNotEqual(before[4],result[4])
    def test_original_source_hash_still_rejects_ctime_only(self):
        with self.mutate_on_read(lambda n:os.chmod(self.path,0o640)),self.assertRaises(ArchiveError):hash_fd(self.fd)
    def test_second_metadata_mutation_fails_closed(self):
        with self.mutate_on_read(lambda n:os.chmod(self.path,0o640 if n%2 else 0o600),repeated=True),self.assertRaises(ArchiveError):verified_target_hash(self.fd,self.expected)
    def test_same_size_content_change_with_restored_mtime_never_passes(self):
        before=self.path.stat()
        def changed(n):
            self.path.write_bytes(b'X'*len(self.data));os.utime(self.path,ns=(before.st_atime_ns,before.st_mtime_ns))
        with self.mutate_on_read(changed),self.assertRaises(ArchiveError):verified_target_hash(self.fd,self.expected)
    def test_mtime_change_does_not_retry(self):
        before=self.path.stat()
        with self.mutate_on_read(lambda n:os.utime(self.path,ns=(before.st_atime_ns,before.st_mtime_ns+10))),self.assertRaises(ArchiveError):verified_target_hash(self.fd,self.expected)
    def test_identity_replacement_is_not_accepted_by_expected_signature(self):
        before=signature(self.fd);self.path.unlink();self.path.write_bytes(self.data)
        other=os.open(self.path,os.O_RDONLY)
        try:
            with self.assertRaises(ArchiveError):verified_target_hash(other,self.expected,expected_signature=before)
        finally:os.close(other)
    def test_extra_hardlink_is_rejected(self):
        os.link(self.path,self.path.with_name('extra'))
        with self.assertRaises(ArchiveError):verified_target_hash(self.fd,self.expected)

class MoveMetadataTests(Fixture,unittest.TestCase):
    def setUp(self):
        super().setUp();self.service.move_enabled=True
        self.decisions['archive_options']={'mode':'move','existing':'skip_identical'}
    def test_target_ctime_only_after_intent_requires_full_hash_and_allows_move(self):
        def change(phase,row):
            if phase=='move_intent':os.chmod(self.root/'projects'/row['target_path'],0o640)
        self.service.checkpoint=change
        done=self.service.run_job(self.submit()['job_id'])
        self.assertEqual(done['status'],'completed',done);self.assertEqual(done['summary']['removed_sources'],4)
    def test_source_ctime_only_after_intent_is_preserved(self):
        def change(phase,row):
            if phase=='move_intent':os.chmod(self.root/'staging'/row['source_path'],0o640)
        self.service.checkpoint=change
        done=self.service.run_job(self.submit()['job_id'])
        self.assertEqual(done['status'],'failed',done);self.assertEqual(done['summary']['removed_sources'],0)

class PublicationMetadataTests(Fixture,unittest.TestCase):
    def inject(self,original,*,required_links=None,known_inode=None):
        read=os.read;changed=[0]
        def verify(fd,digest,**kwargs):
            if ((required_links is not None and os.fstat(fd).st_nlink!=required_links) or
                    (known_inode is not None and os.fstat(fd).st_ino!=known_inode)):
                return original(fd,digest,**kwargs)
            def injected(handle,size):
                if handle==fd and not changed[0]:
                    changed[0]+=1;os.fchmod(fd,0o640)
                return read(handle,size)
            with patch('muli_sorter.archive_io.os.read',side_effect=injected):return original(fd,digest,**kwargs)
        return verify,changed
    def test_prepublication_temp_ctime_change_during_read_is_fully_reverified(self):
        import muli_sorter.archive as a
        inject,changed=self.inject(a.verified_target_hash)
        with patch.object(a,'verified_target_hash',side_effect=inject):done=self.service.run_job(self.submit()['job_id'])
        self.assertEqual(done['status'],'completed',done);self.assertEqual(changed[0],1)
    def test_final_verification_two_name_owned_copy_ctime_change(self):
        import muli_sorter.archive_copy as a
        inject,changed=self.inject(a.verified_target_hash,required_links=2)
        with patch.object(a,'verified_target_hash',side_effect=inject):done=self.service.run_job(self.submit()['job_id'])
        self.assertEqual(done['status'],'completed',done);self.assertEqual(changed[0],1)
        self.assertTrue(all(p.stat().st_nlink==1 for p in (self.root/'projects').rglob('*') if p.is_file()))
    def test_skip_identical_final_read_ctime_change_keeps_both_copies(self):
        import muli_sorter.archive_copy as a
        from muli_sorter.archive_layout import studio_target_rows
        s=next(s for s in self.decisions['segments'] if s['decision']=='confirmed');u=next(u for u in self.model['units'] if u['unit_id']==s['unit_ids'][0]);p=next(p for p in self.model['projects'] if p['project_id']==s['project_id']);row=studio_target_rows(u,p)[0]
        source=self.root/'staging'/row['source_path'];target=self.root/'projects'/row['target_path'];target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(source.read_bytes());inode=target.stat().st_ino
        self.decisions['archive_options']={'mode':'copy','existing':'skip_identical'}
        job=self.submit();inject,changed=self.inject(a.verified_target_hash,known_inode=inode)
        with patch.object(a,'verified_target_hash',side_effect=inject):done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done);self.assertEqual(done['summary']['skipped_files'],1);self.assertEqual(changed[0],1);self.assertEqual(target.stat().st_ino,inode);self.assertTrue(source.exists())
