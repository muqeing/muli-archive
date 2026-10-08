"""Independent, incremental archive -> Ingest metadata feedback worker.

Never changes a job, receipt, manifest, media file or Ingest database. Existing
jobs are excluded by an explicit activation time. A lost event is recovered
from the already durable completed job. The archive worker never waits here.
"""
import os
from pathlib import Path
import threading
import time
from blake3 import blake3
from . import handoff_protocol as wire
from .archive_io import signature
from .archive_target_metadata import MetadataProofCache, check_target
from .intake import _open, validate_record, resolve_files, read_bytes
from .order_feed_io import atomic_json, read_json
from .review import digest


class FeedbackDeferred(Exception):
    pass


REVOCATIONS_NAME = 'handoff-revocations.json'
REVOCATION_LIMIT = 500

# Why a previously published confirmation was withdrawn. The copy service only
# sees "needs_review"; these explanations stay local so the operator can tell an
# expired credential from a receipt that is merely older than the batch version.
REVOCATION_REASONS = {
    'job_not_eligible': '任务状态或完成时间不再符合回填条件',
    'invalid_job_binding': '任务的存储方式与请求记录不一致',
    'invalid_file_plan': '任务的逐文件计划与清单不一致',
    'archive_record_changed': '归档清单或回执在生成反馈后发生变化',
    'manifest_mode_mismatch': '清单的演示/生产属性不一致',
    'no_manifest_coverage': '任务的来源清单已无法对应',
    'records_changed_during_feedback': '生成反馈期间归档记录发生变化',
    'source_manifest_changed': '来源清单发生变化',
    'receipt_identity_mismatch': '独立校验回执的身份与当前来源不一致',
    'unsupported_archive_receipt': '归档回执版本不受支持',
    'target_identity_changed': '目标文件身份发生变化',
    'move_not_closed': '移动任务的文件明细未闭合',
    'job_scope_not_closed': '任务的来源范围未闭合',
    'move_source_still_exists': '移动后来源仍然存在',
    'direct_move_inode_changed': '同卷移动后的文件身份发生变化',
    'target_project_mismatch': '目标项目归属与任务记录不一致',
    'handoff_directory_replaced': '交接目录被替换',
}


def revocation_reason(exc):
    """Short operator-facing explanation for a withdrawn confirmation."""
    message = ' '.join(str(exc).split())[:60]
    return REVOCATION_REASONS.get(str(exc), '其它原因（' + message + '）')


class ArchiveHandoff:
    def __init__(self, service, outbox, acknowledgements, since, *, idle=lambda: True):
        self.service = service
        self.outbox, self.acks = Path(outbox), Path(acknowledgements)
        wire.separate_channels((self.outbox,self.acks),(service.staging,service.projects,service.state))
        self.since = wire.timestamp(since)
        self.idle = idle
        self.identities = [wire.identity(p) for p in (self.outbox, self.acks)]
        self.stop = threading.Event(); self.thread = None
        self.target_metadata_proofs = MetadataProofCache()
        self.checked = {}; self.statuses = {}; self.lock = threading.Lock()

    def roots(self):
        if [wire.identity(p) for p in (self.outbox, self.acks)] != self.identities:
            raise ValueError('handoff_directory_replaced')
        self.service._check_roots()

    def checkpoint(self):
        if self.stop.is_set() or not self.idle():
            raise FeedbackDeferred()

    def build(self, job_id, clock=None):
        clock = time.time() if clock is None else clock
        self.roots()
        job_path = self.service._job_path(job_id)
        token = wire.signature(job_path.lstat())
        job = self.service.get(job_id)
        if (job.get('status') != 'completed' or job.get('job_id') != job_id
                or job.get('errors') or job.get('example_data') is not (not self.service.production)
                or wire.timestamp(job['completed_at']) < self.since):
            raise ValueError('job_not_eligible')
        mode = job.get('archive_options', {}).get('mode', 'copy')
        if mode not in ('copy', 'move') or not wire.HEX.fullmatch(job.get('request_digest', '')):
            raise ValueError('invalid_job_binding')
        plans = job['file_plans']
        if not plans or len(plans) != job['summary']['total_files']:
            raise ValueError('invalid_file_plan')
        records = {}; content_digests = {}; raw_bindings = {}; mapping = {}; seen = set(); dependencies = {}

        def record(path):
            before = wire.signature(path.lstat())
            result = read_json(path)
            if wire.signature(path.lstat()) != before:
                raise ValueError('archive_record_changed')
            dependencies[path] = before
            return result

        def manifest(bid):
            if bid not in records:
                m = validate_record(self.service.staging, bid, allow_examples=not self.service.production)
                if m['example_data'] is not (not self.service.production):
                    raise ValueError('manifest_mode_mismatch')
                records[bid] = m
                content_digests[bid] = digest(m)
                raw_bindings[bid] = {'batch_id':bid,'batch_uid':m['batch']['batch_uid'],
                    'manifest_id':m['manifest_id'],'revision':m['revision'],
                    'manifest_blake3':blake3(read_bytes(self.service.staging,bid+'/ingest_manifest.json')).hexdigest(),
                    'completion_blake3':blake3(read_bytes(self.service.staging,bid+'/ingest_complete.json')).hexdigest()}
                for name in ('ingest_manifest.json','ingest_complete.json','ingest_manifest.md'):
                    p = self.service.staging / bid / name
                    dependencies[p] = wire.signature(p.lstat())
                for f in m['files']:
                    if f['copy_status'] == 'skipped_existing':
                        manifest(f['existing_copy']['batch_id'])
            return records[bid]

        move = None
        if mode == 'move':
            move = record(self.service.state/'requests'/('move-'+job_id+'.json'))
            if (not job.get('cleanup_started') or move['identity']['job_id'] != job_id
                    or move['identity']['request_digest'] != job['request_digest']
                    or move['identity']['roots'] != self.service.identity):
                raise ValueError('move_journal_mismatch')
            moved = {r['source_path']: r for r in move['identity']['files']}
            removed = {r['source_path'] for r in move['files'] if r['state']=='removed'}
        for outcome in job['outcomes']:
            self.checkpoint()
            name = outcome.get('receipt','')
            if outcome.get('status') != 'completed' or not name.startswith('receipt-') or not name.endswith('.json') or not wire.HEX.fullmatch(name[8:-5]):
                raise ValueError('incomplete_outcome')
            receipt = record(self.service.state/'units'/name)
            rows = receipt['files']; uid = outcome['unit_id']
            if (receipt.get('schema_version')!=('archive/0.4' if self.service.production else 'synthetic-archive/0.3')
                    or receipt.get('real_media_write_authorized') is not self.service.production
                    or receipt.get('storage') not in ('independent_copy','same_volume_move')
                    or receipt['storage']=='same_volume_move' and mode!='move'):
                raise ValueError('unsupported_archive_receipt')
            wire.timestamp(receipt['completed_at'])
            if (receipt['status'] != 'completed' or receipt['unit_id'] != uid or receipt['job_id'] != name[8:-5]
                    or receipt['file_count'] != len(rows) or outcome['files'] != len(rows)
                    or receipt['example_data'] is not (not self.service.production)):
                raise ValueError('receipt_identity_mismatch')
            for bid,evidence in receipt['source_evidence'].items():
                m = manifest(bid)
                if m['manifest_id'] != evidence['manifest_id'] or content_digests[bid] != evidence['content_digest']:
                    raise ValueError('source_manifest_changed')
            for row in rows:
                self.checkpoint()
                path = row['source_path']; plan = plans.get(path)
                if path in seen or plan is None or any(row.get(k) != v for k,v in plan.items()) or not row.get('published'):
                    raise ValueError('receipt_scope_mismatch')
                source_sig=row.get('source_signature')
                if not isinstance(source_sig,list) or len(source_sig)!=5 or any(type(v) is not int for v in source_sig) or source_sig[2]!=row['size_bytes']:
                    raise ValueError('missing_source_identity')
                if receipt['storage']=='same_volume_move':
                    method = row.get('transfer_method', 'same_volume_rename')
                    if method == 'skip_identical':
                        if (job.get('archive_options', {}).get('existing') != 'skip_identical'
                                or row.get('identical_target_blake3') != row.get('blake3')):
                            raise ValueError('identical_move_evidence_missing')
                    elif method != 'same_volume_rename' or source_sig[:3] != row['target_signature'][:3]:
                        raise ValueError('direct_move_inode_changed')
                seen.add(path)
                if not row['target_path'].startswith(receipt['project']['path']+'/'):
                    raise ValueError('target_project_mismatch')
                if mode == 'move':
                    if moved.get(path) != row or path not in removed:
                        raise ValueError('move_not_closed')
                    try:
                        f = _open(self.service.staging,path)
                    except FileNotFoundError:
                        pass
                    else:
                        os.close(f); raise ValueError('move_source_still_exists')
                f = _open(self.service.projects,row['target_path'])
                try:
                    actual = check_target(f, row, row['target_signature'],
                                          cache=self.target_metadata_proofs,
                                          progress=lambda amount: self.checkpoint())
                    again = _open(self.service.projects,row['target_path'])
                    try:
                        if (signature(f) != actual or signature(again) != actual or
                                os.fstat(f).st_nlink != 1 or os.fstat(again).st_nlink != 1):
                            raise ValueError('target_identity_changed')
                    finally:
                        os.close(again)
                finally:
                    os.close(f)
                mapping[path] = (row,receipt['project']['name'],uid,name)
        if seen != set(plans) or len(seen) != job['summary']['completed_files']:
            raise ValueError('job_scope_not_closed')
        # resolve_files deliberately validates borrowed originals. Narrow each
        # borrowed lookup through an index to avoid rescanning a whole owner
        # manifest once per skipped file.
        owner_indices={}
        for bid,m in records.items():
            index={}
            for f in m['files']:
                if f['copy_status']=='verified':
                    index.setdefault(bid+'/'+f['destination_relative_path'],[]).append(f)
            owner_indices[bid]=index
        def resolved(m):
            ordinary=[f for f in m['files'] if f['copy_status']!='skipped_existing']
            yield from resolve_files(self.service.staging,{**m,'files':ordinary},records,check_media=False)
            for f in m['files']:
                if f['copy_status']!='skipped_existing':continue
                ref=f['existing_copy'];owner=records[ref['batch_id']]
                narrowed={ref['batch_id']:{**owner,'files':owner_indices[ref['batch_id']].get(ref['staging_relative_path'],[])}}
                yield from resolve_files(self.service.staging,{**m,'files':[f]},narrowed,check_media=False)
        batches = []
        for bid,m in records.items():
            self.checkpoint()
            rows = []; originals = {f['file_id']: f for f in m['files']}
            for logical in resolved(m):
                found = mapping.get(logical['resolved_path'])
                if found is None:
                    continue
                row,project,uid,receipt_id = found
                if row['blake3'] != logical['hash']['source'] or row['size_bytes'] != logical['size_bytes']:
                    raise ValueError('manifest_file_mismatch')
                original = originals[logical['file_id']]
                rows.append({'file_id':logical['file_id'],'relative_path':logical['relative_path'],
                    'size_bytes':logical['size_bytes'],'original_blake3':original['hash']['source'],
                    'archived_blake3':row['blake3'],'physical_source_path':row['source_path'],
                    'target_path':row['target_path'],'target_signature':row['target_signature'],
                    'project_name':project,'mode':mode,'unit_id':uid,'receipt_id':receipt_id})
            if rows:
                batches.append({'binding':raw_bindings[bid],'files':rows})
        if not batches:
            raise ValueError('no_manifest_coverage')
        self.roots()
        if token != wire.signature(job_path.lstat()) or any(wire.signature(p.lstat())!=v for p,v in dependencies.items()):
            raise ValueError('records_changed_during_feedback')
        return {'schema':wire.SCHEMA,'job_id':job_id,'request_digest':job['request_digest'],
                'example_data':not self.service.production,'source_identity':self.service.identity['source_identity'],
                'target_identity':self.service.identity['target_identity'],'job_completed_at':job['completed_at'],
                'checked_at':wire.iso(clock),'expires_at':wire.iso(clock+wire.LEASE_SECONDS),
                'status':'verified','batches':batches}

    def publish(self, job_id, clock=None):
        clock = time.time() if clock is None else clock
        try:
            previous = wire.read(self.outbox,job_id+'.json')
        except FileNotFoundError:
            previous = None
        if previous is not None:
            wire.validate_event(previous,wire.timestamp(previous['checked_at']))
            if previous['job_id']!=job_id:
                raise ValueError('previous_job_mismatch')
        revoked = None
        try:
            event = self.build(job_id,clock)
        except (ValueError,OSError,KeyError,TypeError) as exc:
            if previous is None:
                raise
            # Revoke all prior machine coverage for this job. Do not retain a
            # positive acknowledgement after a destination or receipt changed.
            revoked = revocation_reason(exc)
            event = {**previous,'status':'needs_review','batches':[
                {'binding':b['binding'],'files':[]} for b in previous['batches']],
                'checked_at':wire.iso(clock),'expires_at':wire.iso(clock+wire.LEASE_SECONDS)}
        if revoked is not None:
            self.record_revocations(job_id, previous, revoked, clock)
        event['sequence'] = (previous or {}).get('sequence',0)+1
        event = wire.seal(event); wire.validate_event(event,clock)
        self.roots(); wire.write(self.outbox,job_id+'.json',event)
        wire.write(self.outbox,'status-'+job_id+'.json',{'event_id':event['event_id'],
            'status':'pending' if event['status']=='verified' else 'needs_review','batches':[],
            'expires_at':event['expires_at'],'outbox_signature':wire.signature((self.outbox/(job_id+'.json')).lstat())})
        return event

    def revocation_path(self):
        return self.service.state / REVOCATIONS_NAME

    def record_revocations(self, job_id, previous, reason, clock):
        """Remember, per batch, that a published confirmation was withdrawn."""
        try:
            stored = read_json(self.revocation_path())
        except (OSError, ValueError, KeyError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        for item in previous.get('batches') or []:
            binding = item.get('binding') if isinstance(item, dict) else None
            uid = binding.get('batch_uid') if isinstance(binding, dict) else None
            if not isinstance(uid, str) or not uid:
                continue
            stored[uid] = {'batch_id': binding.get('batch_id'), 'job_id': job_id,
                           'reason': reason, 'at': wire.iso(clock)}
        if len(stored) > REVOCATION_LIMIT:
            ordered = sorted(stored.items(), key=lambda row: str(row[1].get('at') or ''))
            stored = dict(ordered[-REVOCATION_LIMIT:])
        try:
            atomic_json(self.revocation_path(), stored)
        except OSError:
            pass  # Feedback reporting never breaks an archive job.

    def revoked_for(self, batches):
        try:
            stored = read_json(self.revocation_path())
        except (OSError, ValueError, KeyError):
            return {}
        if not isinstance(stored, dict):
            return {}
        return {row['batch_uid']: stored[row['batch_uid']]
                for row in (batches or [])
                if isinstance(row, dict) and row.get('batch_uid') in stored}

    def status(self, job_id, completed_at=None):
        # The console already has the compact job summary.  Use its completion
        # timestamp to exclude pre-activation jobs without reopening a large
        # durable job record or manufacturing a pending state for them.
        if completed_at is not None:
            try:
                if wire.timestamp(completed_at) < self.since:
                    return {'status': 'not_included', 'batches': []}
            except (ValueError, TypeError, KeyError):
                pass
        with self.lock:
            state = dict(self.statuses.get(job_id, {'status':'pending','batches':[]}))
        try:
            if not state.get('event_id'):
                restored=wire.read(self.outbox,'status-'+job_id+'.json')
                if restored['outbox_signature']==wire.signature((self.outbox/(job_id+'.json')).lstat()):
                    state=restored
            if state.get('expires_at') and wire.timestamp(state['expires_at'])<=time.time():
                return {'status':'needs_review','batches':[]}
            ack = wire.read(self.acks,job_id+'.json')
            if (ack.get('schema') == 'muli-archive-handoff-ack/1' and ack.get('job_id') == job_id
                    and ack.get('event_id') == state.get('event_id') and wire.timestamp(ack['expires_at'])>time.time()):
                result = {'status':ack['status'],'checked_at':ack['checked_at'],'batches':ack['batches']}
                revoked = self.revoked_for(ack['batches'])
                if revoked:
                    result['revoked_batches'] = revoked
                return result
        except (OSError,ValueError,KeyError,TypeError):
            pass
        return {k:v for k,v in state.items() if k in ('status','batches','checked_at')}

    def tick(self, clock=None):
        clock = time.time() if clock is None else clock
        if not self.idle():
            return
        self.roots()
        # Stat only unchanged old jobs. Parse one eligible job per tick and
        # never retain a review model / full original request in this worker.
        for p in sorted((self.service.state/'requests').glob('job-*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True):
            jid=p.name[4:-5]
            if not wire.HEX.fullmatch(jid) or p.stat().st_mtime < self.since:
                continue
            token=wire.signature(p.lstat()); old=self.checked.get(jid)
            if old and old[0]==token and clock-old[1]<300:
                continue
            self.checked[jid]=(token,clock)
            job=self.service.get(jid)
            if job.get('status')!='completed' or wire.timestamp(job.get('completed_at',wire.iso(0)))<self.since:
                continue
            del job
            try:
                event=self.publish(jid,clock)
                status={'status':'pending' if event['status']=='verified' else 'needs_review','batches':[], 'event_id':event['event_id'],'expires_at':event['expires_at']}
            except FeedbackDeferred:
                self.checked.pop(jid,None)
                return
            except (OSError,ValueError,KeyError,TypeError):
                status={'status':'needs_review','batches':[]}
                self.checked[jid]=(token,clock-240)  # Retry transient failures in 60s.
            with self.lock:
                self.statuses[jid]=status
            break

    def start(self):
        def loop():
            while not self.stop.wait(10):
                try:
                    self.tick()
                except (OSError,ValueError,KeyError,TypeError):
                    pass  # Feedback cannot terminate or change archive work.
        self.thread=threading.Thread(target=loop,name='archive-handoff',daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError('handoff_worker_still_running')
