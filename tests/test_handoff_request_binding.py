from copy import deepcopy
import json
import os
from pathlib import Path
import random
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from muli_sorter.handoff_request_binding import matches
from muli_sorter.review import canonical,digest


class RequestBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name).resolve()/'request.json'
        self.request={'job_id':'a'*64,'roots':{'source_identity':[123,456],'target_identity':[123,789]},
                      'file_plans':{'input':{'target_path':'项目/照片.JPG','blake3':'b'*64}},
                      'model':{'units':[{'name':'中\\文"{}[],','nested':[True,None,3]}]}}

    def check(self,request=None):
        original=self.request if request is None else request
        return matches(self.path,request_digest=digest(original),job_id=original['job_id'],
                       roots=original['roots'],file_plans=original['file_plans'])

    def save(self,value=None):
        self.path.write_bytes(canonical(self.request if value is None else value))

    def test_canonical_request_matches_without_modifying_input(self):
        self.save();before=self.path.read_bytes();self.assertTrue(self.check())
        self.assertEqual(before,self.path.read_bytes())

    def test_every_small_chunk_boundary_preserves_escapes_and_unicode(self):
        self.save()
        for length in range(1,65):
            with self.subTest(length=length),patch('muli_sorter.handoff_request_binding.CHUNK',length):
                self.assertTrue(self.check())

    def test_nested_models_and_escaped_punctuation_fuzz(self):
        generator=random.Random(17)
        for index in range(100):
            value=deepcopy(self.request)
            value['model']={'label':''.join(generator.choice('中😀"\\{}[],:n\n') for _ in range(40)),
                            'units':[{'name':str(index),'flags':[None,False,index]}]}
            self.save(value);self.assertTrue(self.check(value))

    def test_wrong_job_roots_or_file_plan_rejected(self):
        self.save()
        for field in ('job_id','roots','file_plans'):
            with self.subTest(field=field):
                changed=deepcopy(self.request);changed[field]='changed'
                self.assertFalse(matches(self.path,request_digest=digest(self.request),job_id=changed['job_id'],
                                         roots=changed['roots'],file_plans=changed['file_plans']))

    def test_skipped_historical_model_bytes_remain_bound_by_full_hash(self):
        changed=deepcopy(self.request);changed['model']['units'][0]['name']='tampered'
        self.save(changed);self.assertFalse(self.check())

    def test_noncanonical_whitespace_and_trailing_data_rejected(self):
        for raw in (json.dumps(self.request,ensure_ascii=False,indent=2).encode(),
                    canonical(self.request)+b'\n',canonical(self.request)+b'{}'):
            self.path.write_bytes(raw);self.assertFalse(self.check())

    def test_missing_duplicate_and_truncated_fields_rejected(self):
        raw=canonical(self.request)
        values=[raw[:-1],raw+b'x',b'{"job_id":"'+b'a'*64+b'","job_id":"'+b'a'*64+b'"}']
        values.append(canonical({k:v for k,v in self.request.items() if k!='roots'}))
        for value in values:
            self.path.write_bytes(value);self.assertFalse(self.check())

    def test_oversized_requests_rejected_before_reading(self):
        self.save()
        with patch('muli_sorter.handoff_request_binding.MAX_BYTES',8),patch(
                'muli_sorter.handoff_request_binding.os.read',side_effect=AssertionError('must not read')):
            self.assertFalse(self.check())

    def test_historical_payload_is_not_retained_in_memory(self):
        value=deepcopy(self.request);value['model']={'historical':'x'*(8*1024*1024)}
        self.save(value);expected=digest(value)
        tracemalloc.start()
        try:
            self.assertTrue(matches(self.path,request_digest=expected,job_id=value['job_id'],
                                    roots=value['roots'],file_plans=value['file_plans']))
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertLess(peak,2*1024*1024)

    def test_changed_fd_during_read_rejected(self):
        self.save();read=os.read;touched=[]
        def change(fd,size):
            raw=read(fd,size)
            if not touched:
                self.path.write_bytes(canonical(self.request)+b' ');touched.append(True)
            return raw
        with patch('muli_sorter.handoff_request_binding.os.read',side_effect=change):
            self.assertFalse(self.check())

    def test_symlink_and_multiple_links_rejected(self):
        target=self.path.with_name('original.json');target.write_bytes(canonical(self.request))
        self.path.symlink_to(target)
        with self.assertRaises(OSError):self.check()
        self.path.unlink();os.link(target,self.path);self.assertFalse(self.check())


if __name__=='__main__':unittest.main()
