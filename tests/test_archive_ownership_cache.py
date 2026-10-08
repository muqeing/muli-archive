import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from muli_sorter.archive import recover_index_full
from muli_sorter.archive_ownership_cache import NAME, recover_full, read_identity
from muli_sorter.archive_io import directory, atomic_json, ArchiveError
from muli_sorter.review import digest

class OwnershipCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name).resolve()
        self.ctx=directory(self.path);self.fd=self.ctx.__enter__()
    def tearDown(self):
        self.ctx.__exit__(None,None,None);self.tmp.cleanup()
    def intent(self,uid,**extra):
        identity={'unit_id':uid,'files':[],**extra};jid=digest(identity)
        atomic_json(self.fd,'job-'+jid+'.json',{'identity':identity,'state':'in_progress'})
        return self.path/('job-'+jid+'.json')
    def run_cache(self):
        report=[];r=recover_full(self.fd,progress=lambda **v:report.append(v));return r,report[-1]
    def test_hot_opens_no_intents_new_and_changed_only_read(self):
        a=self.intent('a');self.intent('b');r,_=self.run_cache()
        self.assertEqual(r,recover_index_full(self.fd))
        with patch('muli_sorter.archive_ownership_cache.read_identity',wraps=read_identity) as reads:
            self.assertEqual(self.run_cache()[1]['parsed_records'],0);self.assertEqual(reads.call_count,0)
            self.intent('c');self.assertEqual(self.run_cache()[1]['parsed_records'],1)
            d=json.loads(a.read_text());d['state']='completed';a.write_text(json.dumps(d))
            r,stats=self.run_cache();self.assertEqual(stats['parsed_records'],1)
            self.assertEqual(stats['reused_records'],2);self.assertEqual(reads.call_count,2)
        self.assertEqual(r,recover_index_full(self.fd))
    def test_same_size_mtime_tamper_is_detected_by_ctime(self):
        a=self.intent('a');self.run_cache();st=a.stat();a.write_text(a.read_text().replace('"a"','"b"'));os.utime(a,ns=(st.st_atime_ns,st.st_mtime_ns))
        with self.assertRaises(ArchiveError):self.run_cache()
    def test_duplicate_owner_rejected(self):
        self.intent('a');self.run_cache();self.intent('a',project='another')
        with self.assertRaises(ArchiveError):self.run_cache()
    def test_bad_cache_and_wrong_root_trigger_full_read(self):
        self.intent('a');self.run_cache();p=self.path/NAME;p.write_text('{bad')
        self.assertEqual(self.run_cache()[1]['parsed_records'],1)
        data=json.loads(p.read_text());data['binding']=[0,1];data['digest']=digest({k:v for k,v in data.items() if k!='digest'});p.write_text(json.dumps(data))
        self.assertEqual(self.run_cache()[1]['parsed_records'],1)
    def test_deleted_index_recovery_and_missing_intent_preserves_reservation(self):
        a=self.intent('a');self.intent('b');expected,_=self.run_cache();(self.path/'archive-index.json').unlink()
        self.assertEqual(recover_index_full(self.fd),expected)
        a.unlink();r,stats=self.run_cache();self.assertEqual(r,expected);self.assertEqual(stats['reason'],'record_removed_full_read');self.assertEqual(stats['parsed_records'],1)
    def test_symlink_and_hardlink_rejected_even_on_cache_hit(self):
        a=self.intent('a');self.run_cache();q=self.path/'other';a.rename(q);a.symlink_to(q)
        with self.assertRaises((OSError,ArchiveError)):self.run_cache()
        a.unlink();q.rename(a);os.link(a,q)
        with self.assertRaises(ArchiveError):self.run_cache()
    def test_change_or_new_intent_during_check_rejected(self):
        a=self.intent('a');self.run_cache()
        def mutate(**v):
            if v.get('checked_records')==1:a.write_text(a.read_text()+' ')
        with self.assertRaises(ArchiveError):recover_full(self.fd,progress=mutate)
        self.assertEqual(self.run_cache()[1]['parsed_records'],1)
    def test_cache_write_failure_does_not_skip_validation(self):
        self.intent('a')
        from muli_sorter.archive_ownership_cache import atomic_json as actual
        def fail(fd,name,value):
            if name==NAME:raise OSError('cache write failed')
            return actual(fd,name,value)
        with patch('muli_sorter.archive_ownership_cache.atomic_json',fail):
            r,s=self.run_cache();self.assertEqual(len(r),1);self.assertFalse(s['cache_saved'])
        self.assertEqual(self.run_cache()[1]['parsed_records'],1)
    def test_existing_index_conflict_not_hidden_by_warm_cache(self):
        self.intent('a');self.run_cache();atomic_json(self.fd,'archive-index.json',{'a':'0'*64})
        with self.assertRaises(ArchiveError):self.run_cache()
    def test_empty_and_malformed_intent(self):
        self.assertEqual(self.run_cache()[0],{})
        (self.path/('job-'+'0'*64+'.json')).write_text('{}')
        with self.assertRaises((ArchiveError,KeyError)):self.run_cache()
