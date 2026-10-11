"""Classifier role checks; archive proof checks run in the archive image."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from blake3 import blake3
from test_intake import fixture, write
from muli_sorter.intake import validate_record, resolve_files, EvidenceError
from muli_sorter.matching import group_files, project_index
from muli_sorter.queue_evidence import signal_parts
from muli_sorter.photo_clock_receipt import NAME, SCHEMA
from muli_sorter.review import build_review_model

class PhotoClockClassifierTests(unittest.TestCase):
    def setUp(self):
            t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
            self.root = Path(t.name).resolve(); self.staging = self.root/'staging'; self.staging.mkdir()
            self.m = fixture(self.staging); self.bid = self.m['batch']['batch_id']
            self.batch = self.staging/self.bid; self.file = self.batch/'SOURCE_DATA/A.ARW'
            self.old = '2026-09-29T04:11:36+08:00'; self.new = '2026-09-28T16:11:36+08:00'
            a = (datetime.fromisoformat(self.old).strftime('%Y:%m:%d %H:%M:%S')+'\0').encode()
            self.before = b'HEADER'+b'x'*94+a*3+b'RAW-PIXELS'*50
            self.file.write_bytes(self.before)
            f = self.m['files'][0]; h = blake3(self.before).hexdigest()
            f.update(size_bytes=len(self.before),copy_status='size_verified',hash_match=None,error=None)
            f['hash'].update(source=h,destination=None,existing_destination=None)
            f['metadata']['capture_time'].update(normalized=self.old,wall_time=self.old[:-6],confidence='timezone_aware')
            self.m['batch']['result']='COPY_SIZE_VERIFIED'
            self.m['summary'].update(selected_bytes=len(self.before),verified_file_count=0,size_verified_file_count=1,
                                    copied_file_count=1,copied_bytes=len(self.before),awaiting_hash_file_count=0)
            write(self.staging,self.m); self.raw=(self.batch/'ingest_manifest.json').read_bytes()
            self.proofs=self.root/'postcopy';self.proofs.mkdir()
            self.env=patch.dict(os.environ,{'MULI_POSTCOPY_RECEIPTS':str(self.proofs)});self.env.start();self.addCleanup(self.env.stop)
            now=datetime.now(timezone.utc).isoformat();self.path=self.bid+'/SOURCE_DATA/A.ARW'
            self.proof={'schema':'postcopy-verification/1','status':'completed','verified_at':now,
                        'batch_id':self.bid,'batch_uid':'uid1','revision':1,'manifest_id':'uid1-r1','source_id':'cam1',
                        'original_manifest_blake3':blake3(self.raw).hexdigest(),
                        'original_ingest_complete_blake3':blake3((self.batch/'ingest_complete.json').read_bytes()).hexdigest(),
                        'files':[{'file_id':'f1','relative_path':'A.ARW','resolved_path':self.path,
                                  'size_bytes':len(self.before),'blake3':h,'source_signature':self.sig()}]}
            self.proof_raw=json.dumps(self.proof).encode();(self.proofs/(self.bid+'.json')).write_bytes(self.proof_raw)
            self.projects=self.root/'projects';(self.projects/'2026/9月/20260928_00862_合成').mkdir(parents=True)
            self.rows=[{**self.m['batch'],'revision':1}]

    def sig(self):
            s=self.file.stat();return dict(dev=s.st_dev,ino=s.st_ino,size=s.st_size,mtime_ns=s.st_mtime_ns,ctime_ns=s.st_ctime_ns)

    def provider(self):
            return dict(generated_at=datetime.now(timezone.utc).isoformat(),complete_listing=True,batches=self.rows)

    def unit(self):
            m=validate_record(self.staging,self.bid)
            projects=project_index(self.projects)
            groups=group_files(resolve_files(self.staging,m,{self.bid:m}),projects,[],m['manifest_id'])
            report=dict(mode='read_only_preview',example_data=False,generated_at=datetime.now(timezone.utc).isoformat(),
                        projects=projects,batches=[dict(batch_id=self.bid,status='verified',manifest_id=m['manifest_id'],groups=groups)])
            return build_review_model(report)['units'][0]

    def correct(self):
            old=(datetime.fromisoformat(self.old).strftime('%Y:%m:%d %H:%M:%S')+'\0').encode()
            new=(datetime.fromisoformat(self.new).strftime('%Y:%m:%d %H:%M:%S')+'\0').encode()
            data=bytearray(self.before);patches=[]
            for n,tag in enumerate(['IFD0:ModifyDate','ExifIFD:DateTimeOriginal','ExifIFD:CreateDate']):
                at=100+20*n;data[at:at+20]=new
                patches.append(dict(tag=tag,offset=at,before_hex=old.hex(),after_hex=new.hex()))
            self.file.write_bytes(data);now=datetime.now(timezone.utc).isoformat()
            self.receipt=dict(schema=SCHEMA,status='completed',operator_confirmed=True,batch_id=self.bid,batch_uid='uid1',
                              revision=1,manifest_id='uid1-r1',original_manifest_blake3=blake3(self.raw).hexdigest(),
                              original_postcopy_blake3=blake3(self.proof_raw).hexdigest(),correction_id='synthetic-photo-minus12h',
                              offset_seconds=-43200,completed_at=now,
                              files=[dict(file_id='f1',relative_path='A.ARW',size_bytes=len(data),original_blake3=blake3(self.before).hexdigest(),
                                          corrected_blake3=blake3(data).hexdigest(),original_capture_time=self.old,corrected_capture_time=self.new,
                                          original_source_signature=self.proof['files'][0]['source_signature'],corrected_source_signature=self.sig(),
                                          patches=patches,readback_verified_at=now)])
            self.publish()

    def publish(self):
            (self.batch/NAME).write_text(json.dumps(self.receipt))

    def test_only_corrected_batch_signal_changes(self):
            other=fixture(self.staging,'BATCH_20260928_000002','uid2');self.rows.append({**other['batch'],'revision':1})
            before=signal_parts(self.staging,[self.bid,other['batch']['batch_id']],self.provider())
            self.correct();after=signal_parts(self.staging,[self.bid,other['batch']['batch_id']],self.provider())
            self.assertNotEqual(before[self.bid],after[self.bid]);self.assertEqual(before[other['batch']['batch_id']],after[other['batch']['batch_id']])

    def test_invalid_receipts_never_admit(self):
            self.correct();good=deepcopy(self.receipt)
            mutations=[lambda r:r.update(status='prepared'),lambda r:r.update(offset_seconds=-46800),
                       lambda r:r.update(original_postcopy_blake3='0'*64),lambda r:r.update(batch_uid='other'),
                       lambda r:r['files'].append(deepcopy(r['files'][0])),
                       lambda r:r['files'][0]['patches'][0].update(tag='MakerNote'),
                       lambda r:r['files'][0]['patches'][1].update(offset=100),
                       lambda r:r['files'][0]['corrected_source_signature'].update(ino=123456)]
            for mutate in mutations:
                with self.subTest():
                    self.receipt=deepcopy(good);mutate(self.receipt);self.publish()
                    with self.assertRaises(EvidenceError):validate_record(self.staging,self.bid)

    def test_missing_independent_verification_still_blocks(self):
            self.correct();(self.proofs/(self.bid+'.json')).unlink()
            with self.assertRaises(EvidenceError):validate_record(self.staging,self.bid)

    def test_classification_uses_corrected_time_hash_and_date_candidate(self):
        self.correct(); u = self.unit()
        self.assertEqual(u['capture_time'], self.new)
        self.assertTrue(u['candidate_project_ids'])
        self.assertTrue(u['timezone_trusted'])
        self.assertEqual(u['files'][0]['blake3'], blake3(self.file.read_bytes()).hexdigest())
        self.assertEqual((self.batch/'ingest_manifest.json').read_bytes(), self.raw)
        self.assertEqual((self.proofs/(self.bid+'.json')).read_bytes(), self.proof_raw)
