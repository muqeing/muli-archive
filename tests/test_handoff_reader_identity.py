"""A reader device change is not a change to immutable source proof bytes."""
from copy import deepcopy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import nullcontext

from muli_sorter.handoff_source_evidence import FIELDS, MAX_PROOFS, matches
from muli_sorter.review import digest


class ReaderProofTests(unittest.TestCase):
    def fixture(self, schema='postcopy-verification/2'):
        postcopy = dict(schema=schema, status='completed', verified_at='2026-10-01T00:00:00+00:00',
            batch_id='BATCH_20261001_000001', batch_uid='sample', revision=1,
            manifest_id='sample-r1', source_id='card', original_manifest_blake3='a'*64,
            original_ingest_complete_blake3='b'*64, receipt_blake3='c'*64,
            receipt_root={'path':'/receipts','dev':10,'ino':20},
            receipt_signature=[[10,20],[10,30,100,200,300],'c'*64],
            files={'source':{'blake3':'d'*64,'source_signature':{'fsid':123,'ino':40}}})
        old = {'manifest_id':'sample-r1','files':[{'hash':{'source':'d'*64}}],
               '_postcopy_evidence':postcopy}
        evidence = {'manifest_id':old['manifest_id'],'content_digest':digest(old),
                    'postcopy':{key:deepcopy(postcopy[key]) for key in FIELDS}}
        current = deepcopy(old)
        current['_postcopy_evidence']['receipt_root']['dev'] = 11
        current['_postcopy_evidence']['receipt_signature'][0][0] = 11
        current['_postcopy_evidence']['receipt_signature'][1][0] = 11
        return old, current, evidence

    def test_exact_original_evidence_remains_supported(self):
        old, _, evidence = self.fixture()
        self.assertTrue(matches(old,evidence))
        evidence.pop('postcopy')
        self.assertTrue(matches(old,evidence))

    def test_only_reader_device_shift_supported_for_both_receipt_schemas(self):
        for schema in ('postcopy-verification/1','postcopy-verification/2'):
            with self.subTest(schema=schema):
                old,current,evidence=self.fixture(schema)
                before=deepcopy((old,current,evidence))
                self.assertTrue(matches(current,evidence))
                self.assertEqual((old,current,evidence),before)

    def test_all_other_receipt_metadata_changes_are_rejected(self):
        for key in FIELDS-{'receipt_root','receipt_signature'}:
            with self.subTest(key=key):
                _,current,evidence=self.fixture()
                current['_postcopy_evidence'][key]='changed'
                self.assertFalse(matches(current,evidence))

    def test_reader_inode_size_mtime_ctime_hash_and_path_changes_rejected(self):
        paths=[('receipt_root','ino'),('receipt_root','path'),
               ('receipt_signature',0,1),('receipt_signature',1,1),
               ('receipt_signature',1,2),('receipt_signature',1,3),
               ('receipt_signature',1,4),('receipt_signature',2)]
        for path in paths:
            with self.subTest(path=path):
                _,current,evidence=self.fixture();row=current['_postcopy_evidence']
                for key in path[:-1]:row=row[key]
                key=path[-1];row[key]=row[key]+1 if type(row[key]) is int else row[key]+'x'
                self.assertFalse(matches(current,evidence))

    def test_embedded_source_identity_or_digest_never_normalized(self):
        for key,value in [('blake3','e'*64),('source_signature',{'fsid':999,'ino':40})]:
            with self.subTest(key=key):
                _,current,evidence=self.fixture();current['_postcopy_evidence']['files']['source'][key]=value
                self.assertFalse(matches(current,evidence))

    def test_raw_manifest_and_amendment_changes_still_change_full_digest(self):
        for field in ('files','_photo_amendment','_amendment','future_field'):
            with self.subTest(field=field):
                _,current,evidence=self.fixture();current[field]=['different']
                self.assertFalse(matches(current,evidence))

    def test_revision_digest_missing_projection_or_unknown_schema_rejected(self):
        for mode in ('revision','digest','missing','extra','schema'):
            with self.subTest(mode=mode):
                _,current,evidence=self.fixture()
                if mode=='revision':evidence['manifest_id']='sample-r2'
                if mode=='digest':evidence['content_digest']='0'*64
                if mode=='missing':evidence.pop('postcopy')
                if mode=='extra':evidence['postcopy']['extra']=True
                if mode=='schema':
                    evidence['postcopy']['schema']='unknown';current['_postcopy_evidence']['schema']='unknown'
                self.assertFalse(matches(current,evidence))

    def test_device_mapping_must_be_coherent_and_integer(self):
        for field in ('root','file','boolean'):
            with self.subTest(field=field):
                _,current,evidence=self.fixture();p=current['_postcopy_evidence']
                if field=='root':p['receipt_signature'][0][0]=12
                if field=='file':p['receipt_signature'][1][0]=12
                if field=='boolean':p['receipt_root']['dev']=True
                self.assertFalse(matches(current,evidence))

    def test_many_units_reuse_one_projected_manifest_hash(self):
        _,current,evidence=self.fixture();cache={};calls=[]
        def observed(value):
            calls.append(value)
            return digest(value)
        current_digest=digest(current)
        with patch('muli_sorter.handoff_source_evidence.digest',side_effect=observed):
            for _ in range(1000):
                self.assertTrue(matches(current,evidence,current_digest=current_digest,cache=cache))
        self.assertEqual(sum('files' in row for row in calls),1)
        self.assertEqual(len(cache),1)
        current['files'][0]['hash']['source']='e'*64
        self.assertFalse(matches(current,evidence,cache=cache))

    def test_build_proof_cache_is_bounded(self):
        cache={}
        for index in range(MAX_PROOFS+2):
            old,current,evidence=self.fixture()
            old['serial']=index;current['serial']=index;evidence['content_digest']=digest(old)
            self.assertTrue(matches(current,evidence,cache=cache))
        self.assertEqual(len(cache),MAX_PROOFS)


from test_archive_jobs import Fixture
import test_postcopy_move
from muli_sorter.archive_handoff import ArchiveHandoff
from muli_sorter.archive_layout import studio_target_rows
from muli_sorter import handoff_protocol as wire
from muli_sorter.postcopy_receipt import source_signature


class ReaderFeedbackIntegrationTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.postcopy=self.root/'postcopy-verifications';self.postcopy.mkdir()
        test_postcopy_move.PostcopyMoveTests._make_size_only(self)
        self.service.close()
        self.env=patch.dict(os.environ,{'MULI_POSTCOPY_RECEIPTS':str(self.postcopy)})
        self.env.start();self.addCleanup(self.env.stop)
        self.service=self.make_service(move_enabled=True)
        self.outbox=self.root/'feedback-out';self.acks=self.root/'feedback-ack'
        self.outbox.mkdir();self.acks.mkdir()
        self.producer=ArchiveHandoff(self.service,self.outbox,self.acks,'2020-01-01T00:00:00+00:00')
        self.addCleanup(self.producer.close)

    def remount(self):
        original=os.fstat
        def shifted(fd):
            info=original(fd)
            fields={key:getattr(info,key) for key in dir(info) if key.startswith('st_')}
            fields['st_dev']=info.st_dev+17
            return SimpleNamespace(**fields)
        return patch('muli_sorter.postcopy_receipt.os.fstat',side_effect=shifted)

    def complete(self,mode='copy',v2=False,direct=False):
        if v2:
            for p in self.postcopy.glob('*.json'):
                value=json.loads(p.read_bytes());value['schema']='postcopy-verification/2'
                for row in value['files']:
                    fd=os.open(self.service.staging/row['resolved_path'],os.O_RDONLY)
                    try:row['source_signature']=source_signature(fd,value['schema'])
                    finally:os.close(fd)
                p.write_text(json.dumps(value))
        self.decisions['archive_options']={'mode':mode,'existing':'skip_identical'}
        if direct:
            self.service.direct_move_view={'root':str(self.root),'staging':'staging','projects':'projects'}
            seg=next(s for s in self.decisions['segments'] if s['decision']=='confirmed')
            unit=next(u for u in self.model['units'] if u['unit_id']==seg['unit_ids'][0])
            project=next(p for p in self.model['projects'] if p['project_id']==seg['project_id'])
            row=studio_target_rows(unit,project)[0];target=self.service.projects/row['target_path']
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((self.service.staging/row['source_path']).read_bytes())
        job=self.submit();done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        return done

    def event(self,job):
        paths=[self.service.state/'requests'/('request-'+job['job_id']+'.json')]
        paths.extend(self.service.state/'units'/r['receipt'] for r in job['outcomes'])
        before={p:p.read_bytes() for p in paths if p.exists()}
        with self.remount():event=wire.seal({**self.producer.build(job['job_id']),'sequence':1})
        wire.validate_event(event,__import__('time').time())
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        return event

    def test_copy_feedback_keeps_original_receipts_after_remount(self):
        self.assertTrue(self.event(self.complete())['batches'])

    def test_copy_then_cleanup_move_feedback_after_reader_remount(self):
        self.assertTrue(self.event(self.complete('move',True))['batches'])

    def test_mixed_same_volume_move_feedback_after_reader_remount(self):
        job=self.complete('move',True,True)
        self.assertEqual(job['summary']['skipped_files'],1)
        self.assertTrue(self.event(job)['batches'])

    def test_changed_original_request_cannot_use_compatibility(self):
        job=self.complete();p=self.service.state/'requests'/('request-'+job['job_id']+'.json')
        value=json.loads(p.read_bytes());value['roots']['target_identity'][1]+=1;p.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'invalid_job_binding'):self.event(job)

    def test_renewal_reuses_small_request_binding_but_rechecks_changed_file(self):
        from muli_sorter.archive_handoff import request_binding_matches
        job=self.complete()
        with patch('muli_sorter.archive_handoff.request_binding_matches',wraps=request_binding_matches) as checker:
            self.event(job);self.event(job);self.assertEqual(checker.call_count,1)
            p=self.service.state/'requests'/('request-'+job['job_id']+'.json')
            value=json.loads(p.read_bytes());value['model']['snapshot_at']='changed';p.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,'invalid_job_binding'):self.event(job)
            self.assertEqual(checker.call_count,2)

    def test_request_binding_cache_keeps_only_bounded_metadata(self):
        job=self.complete()
        self.producer.request_bindings={str(i):('metadata',) for i in range(256)}
        self.event(job)
        self.assertEqual(len(self.producer.request_bindings),256)
        self.assertNotIn('0',self.producer.request_bindings)
        self.assertTrue(all(type(value) is tuple for value in self.producer.request_bindings.values()))

    def test_unverified_retained_job_never_enters_compatibility(self):
        job=self.complete();job['status']='copied_unverified';self.service._save(job)
        with patch('muli_sorter.archive_handoff.source_evidence_matches',side_effect=AssertionError('must not run')):
            with self.assertRaisesRegex(ValueError,'job_not_eligible'):self.event(job)

    def test_replaced_receipt_is_not_a_device_only_change(self):
        job=self.complete();p=next(self.postcopy.glob('*.json'));new=p.with_suffix('.replacement')
        new.write_bytes(p.read_bytes());os.replace(new,p)
        with self.assertRaisesRegex(ValueError,'source_manifest_changed'):self.event(job)

    def test_missing_original_request_cannot_use_compatibility(self):
        job=self.complete();(self.service.state/'requests'/('request-'+job['job_id']+'.json')).unlink()
        with self.assertRaises((OSError,ValueError)):self.event(job)

    def test_changed_persistent_target_root_still_rejected(self):
        job=self.complete();self.service.identity['target_identity'][1]+=1
        with self.assertRaises(ValueError):self.event(job)

    def test_replaced_target_inode_still_rejected(self):
        job=self.complete();receipt=json.loads((self.service.state/'units'/job['outcomes'][0]['receipt']).read_bytes())
        target=self.service.projects/receipt['files'][0]['target_path'];s=target.stat();other=target.with_suffix('.replacement')
        other.write_bytes(target.read_bytes());os.utime(other,ns=(s.st_atime_ns,s.st_mtime_ns));os.replace(other,target)
        with self.assertRaises(ValueError):self.event(job)

    def test_tampered_target_still_fails_with_restored_mtime(self):
        job=self.complete();receipt=json.loads((self.service.state/'units'/job['outcomes'][0]['receipt']).read_bytes())
        target=self.service.projects/receipt['files'][0]['target_path'];before=target.stat();raw=target.read_bytes()
        target.write_bytes(bytes([raw[0]^1])+raw[1:]);os.utime(target,ns=(before.st_atime_ns,before.st_mtime_ns))
        with self.assertRaises(ValueError):self.event(job)

    def test_receipt_change_during_target_check_blocks_publication(self):
        job=self.complete();p=next(self.postcopy.glob('*.json'))
        from muli_sorter.archive_handoff import check_target
        touched=[]
        def changed(*args,**kwargs):
            result=check_target(*args,**kwargs)
            if not touched:p.write_bytes(p.read_bytes()+b'\n');touched.append(True)
            return result
        with patch('muli_sorter.archive_handoff.check_target',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'records_changed_during_feedback'):self.event(job)
        self.assertFalse(list(self.outbox.iterdir()))

    def test_legacy_new_source_admission_still_requires_explicit_renewal(self):
        self.complete()
        from muli_sorter.archive_source import verify_sources
        with self.remount(),self.assertRaisesRegex(ValueError,'NAS 重启.*续验'):
            verify_sources(self.service.staging,self.model['units'][0],self.runtime)

    def consume(self,event):
        """Run the maintained real consumer with a synthetic in-memory store."""
        import importlib,sys,threading
        from types import ModuleType
        from test_staging_coordination import ingest_protocol_module
        protocol=ingest_protocol_module()
        self.assertIsNotNone(protocol,'Maintained ingest checkout required for paired feedback check')
        folder=protocol.parent;self.assertTrue((folder/'archive_handoff.py').is_file())
        name='_reader_feedback_ingest'
        if name not in sys.modules:
            package=ModuleType(name);package.__path__=[str(folder)];sys.modules[name]=package
        consumer=importlib.import_module(name+'.archive_handoff')
        manifest=json.loads((self.service.staging/event['batches'][0]['binding']['batch_id']/'ingest_manifest.json').read_bytes())
        batch={**manifest['batch'],'revision':manifest['revision'],'summary':manifest['summary'],'lifecycle':{}}
        class Store:
            def __init__(self):self.rows={};self.events=[]
            def get(self,kind,key):return deepcopy(self.rows.get((kind,key)))
            def put(self,kind,key,value):self.rows[(kind,key)]=deepcopy(value)
            def all(self,kind):return [deepcopy(v) for (k,_),v in self.rows.items() if k==kind]
            def transaction(self):return nullcontext()
            def event(self,uid,value):self.events.append((uid,deepcopy(value)))
        class Engine:
            def __init__(engine):
                engine.staging=self.service.staging;engine.mode='demo';engine.lock=threading.RLock()
                engine.store=Store();engine.value=deepcopy(batch)
            def batch(engine,uid):
                self.assertEqual(uid,engine.value['batch_uid']);return deepcopy(engine.value)
            def _save(engine,value):engine.value=deepcopy(value)
        engine=Engine();worker=consumer.IngestHandoff(engine,self.outbox,self.acks)
        before={p:p.read_bytes() for p in (self.service.staging/manifest['batch']['batch_id']).glob('ingest_*')}
        ack=worker.apply(event)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        return ack,engine.value

    def test_partial_feedback_stays_partial_through_real_consumer(self):
        from muli_sorter.batch_lifecycle import confirmed_handoff
        event=self.event(self.complete('move',True));ack,batch=self.consume(event)
        self.assertEqual(ack['status'],'synced')
        self.assertEqual(batch['archive_handoff']['status'],'partial')
        self.assertEqual(batch['archive_handoff']['confirmed_file_count'],4)
        self.assertEqual(batch['archive_handoff']['missing_file_count'],1)
        self.assertFalse(confirmed_handoff(batch,__import__('time').time()))

    def test_complete_feedback_confirms_exact_scope_through_real_consumer(self):
        from muli_sorter.batch_lifecycle import confirmed_handoff
        units={u['unit_id']:u for u in self.model['units']}
        for segment in self.decisions['segments']:
            segment['decision']='confirmed';segment['project_id']=units[segment['unit_ids'][0]]['candidate_project_ids'][0]
        event=self.event(self.complete('move',True));ack,batch=self.consume(event)
        self.assertEqual(ack['status'],'synced')
        self.assertEqual(batch['archive_handoff']['status'],'confirmed')
        self.assertEqual(batch['archive_handoff']['confirmed_file_count'],5)
        self.assertEqual(batch['archive_handoff']['missing_file_count'],0)
        self.assertTrue(confirmed_handoff(batch,__import__('time').time()))


if __name__=='__main__':unittest.main()
