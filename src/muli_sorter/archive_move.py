"""Journaled source cleanup after every destination in a move request verifies.

Only individual source files are removed. Ingest manifests, receipts, directories
and its database are never edited. A durable removing intent allows recovery
when the process exits after unlink but before saving its result.
"""
import os
from pathlib import PurePosixPath
from .archive_io import ArchiveError, atomic_json, directory, hash_fd, open_file, signature, subdirectory, persistent_identity, verified_target_hash
from .archive_options import options_for
from .archive_source import verify_sources
from .postcopy_receipt import PostcopyError, candidate_signature
from .time_correction_receipt import RECEIPT_NAME, ReceiptError, receipt_signature
from .queue_source import validate_snapshot
from .review import digest
from .staging_coordination import staging_guard, batch_guard, BATCH_ID


def require_ingest_idle(service, batch_ids=None):
    snapshot = service.runtime()
    if service.production:
        validate_snapshot(snapshot)
    if not isinstance(snapshot,dict) or not isinstance(snapshot.get('batches'),list):
        raise ArchiveError('无法确认备份任务状态，暂不清理中转')
    if batch_ids is not None:
        selected = set(batch_ids)
        rows = [r for r in snapshot['batches'] if r.get('batch_id') in selected]
        if (len(rows) != len(selected) or {r.get('batch_id') for r in rows} != selected or
                any(r.get('state') != 'COMPLETED' for r in rows)):
            raise ArchiveError('所选批次或增量引用批次尚未完成，暂不移动')
        return
    # Unknown or in-progress states never authorize source removal.
    if any(row.get('state') not in ('COMPLETED','FAILED','CANCELLED','INTERRUPTED') for row in snapshot['batches']):
        raise ArchiveError('备份任务尚未结束，移动清理已暂停；待备份结束后继续本任务')


def request_batches(request, evidence=()):
    """Bind protection to actual sources and all receipt-bound control batches."""
    plans = request.get('file_plans')
    if not isinstance(plans, dict) or not plans:
        raise ArchiveError('移动任务缺少精确来源范围')
    batches = set()
    for path in plans:
        parts = path.split('/')
        if len(parts) < 3 or parts[1] != 'SOURCE_DATA' or any(p in ('', '.', '..') for p in parts):
            raise ArchiveError('移动来源不是有效批次内的素材路径')
        batches.add(parts[0])
    for item in request.get('source_evidence', {}).values():
        batches.update(item)
    for item in evidence:
        batches.update(item)
    for unit in request.get('model', {}).get('units', []):
        if any(f.get('source_path') in plans for f in unit.get('files', [])):
            batches.update(p['batch_id'] for p in unit.get('provenance', []))
    if any(not isinstance(b, str) or not BATCH_ID.fullmatch(b) for b in batches):
        raise ArchiveError('移动批次保护范围无效')
    return sorted(batches)


def checked(fd, name, row, expected, *, target=False):
    handle = open_file(fd,name)
    try:
        actual = signature(handle)
        if (actual[:4] != expected[:4] if target else actual != expected) or os.fstat(handle).st_nlink != 1:
            raise ArchiveError('移动清理前文件身份或链接关系发生变化：'+name)
        if actual[2] != row['size_bytes']:
            raise ArchiveError('移动清理前文件内容校验不通过：'+name)
        if target:
            verified_target_hash(handle, row['blake3'], expected_signature=expected)
        elif hash_fd(handle) != row['blake3']:
            raise ArchiveError('移动清理前文件内容校验不通过：'+name)
        current = open_file(fd,name)
        try:
            if signature(current) != signature(handle):
                raise ArchiveError('移动清理前路径被替换：'+name)
        finally:
            os.close(current)
    finally:
        os.close(handle)


def finish_move(service, job, request):
    if not service.production:
        with staging_guard(service.staging,create=True):
            pass
    # A production lock must already be initialized by the compatible Ingest.
    # Never invent cooperation by creating it in the production archive worker.
    job['phase'] = '等待内容校验或备份操作结束，再核对并清理中转'
    service._save(job)
    from .archive import record
    evidence = []
    with directory(service.state/'units') as receipts:
        for outcome in job['outcomes']:
            receipt = record(receipts, outcome['receipt'])
            if not receipt or receipt.get('status') != 'completed':
                raise ArchiveError('缺少目标完成回执，不能开始移动清理')
            evidence.append(receipt['source_evidence'])
    batches = request_batches(request, evidence)
    with batch_guard(service.staging, batches, exclusive=True, wait=True, cancel=service.stop_event) as scoped:
        return _finish_move_locked(service,job,request,batches if scoped else None)


def _finish_move_locked(service, job, request, batch_ids=None):
    from .archive import record, now
    if not service.enabled or not service.move_enabled or options_for(request['decisions'])['mode']!='move':
        raise ArchiveError('未启用本任务的移动清理权限')
    require_ingest_idle(service, batch_ids)
    service._check_roots()
    if request.get('file_plans') != job.get('file_plans'):
        raise ArchiveError('移动目标计划与已提交范围不一致')
    model = request['model']
    approved_proxy_ids, links = service._proxy_authorization(model, request['decisions'])
    units = {}
    for unit in model['units']:
        clean = {k:v for k,v in unit.items() if k != '_proxy_archive_authorized'}
        if unit['unit_id'] in approved_proxy_ids:
            clean['_proxy_archive_authorized'] = True
        units[unit['unit_id']] = clean
    rows, evidence = [], {}
    with directory(service.state/'units') as receipts:
        for outcome in job['outcomes']:
            if outcome['status']!='completed':
                raise ArchiveError('目标尚未全部完成，不能开始清理中转')
            receipt = record(receipts,outcome['receipt'])
            if not receipt or receipt['unit_id']!=outcome['unit_id'] or receipt['status']!='completed':
                raise ArchiveError('缺少已校验目标的归档回执')
            uid = receipt['unit_id']
            if uid not in units or len(receipt['files'])!=len(units[uid]['files']):
                raise ArchiveError('归档回执不属于本次素材')
            for row in receipt['files']:
                expected = request['file_plans'].get(row['source_path'])
                if not expected or any(row.get(k)!=v for k,v in expected.items()):
                    raise ArchiveError('移动文件与已提交目标计划不一致')
                rows.append(row)
            evidence[uid] = receipt['source_evidence']
    if set(r['source_path'] for r in rows) != set(request['file_plans']):
        raise ArchiveError('移动清理范围未闭合')
    # Exact same source may occur only once. Refuse ambiguous cleanup ownership.
    if len(rows)!=len(request['file_plans']):
        raise ArchiveError('移动清理包含重复来源')
    with directory(service.state/'requests') as state, directory(service.staging) as sources, directory(service.projects) as targets:
        name = 'move-'+job['job_id']+'.json'
        journal = record(state,name)
        identity = {'job_id':job['job_id'],'request_digest':digest(request),'roots':service.identity,'files':rows}
        if journal is None:
            journal = {'identity':identity,'files':[{'source_path':r['source_path'],'state':'pending'} for r in rows]}
            atomic_json(state,name,journal)
        if journal['identity'] != identity or len(journal['files'])!=len(rows):
            raise ArchiveError('移动清理记录与任务范围不一致')

        def target_check(row):
            with subdirectory(targets,str(PurePosixPath(row['target_path']).parent)) as dest:
                checked(dest,row['target_name'],row,row['target_signature'],target=True)

        def evidence_check():
            cache = {}
            for uid, expected in evidence.items():
                actual = verify_sources(service.staging,units[uid],service.runtime,production=service.production,
                                        reviewed_metadata=True,cache=cache,check_media=False,
                                        companion_parent=units.get(links.get(uid)))
                if actual != expected:
                    raise ArchiveError('移动清理前成功清单或批次状态发生变化')

        evidence_check()
        manifests = {}
        for batch_id in {bid for value in evidence.values() for bid in value}:
            with subdirectory(sources,batch_id) as batch:
                for file_name in ('ingest_complete.json','ingest_manifest.json','ingest_manifest.md'):
                    handle = open_file(batch,file_name)
                    try:
                        manifests[(batch_id,file_name)] = signature(handle)
                    finally:
                        os.close(handle)
            try:
                # The correction receipt is optional, but its absence is an
                # evidence value too: appearing or changing after confirmation
                # must invalidate the move and source cleanup.
                manifests[(batch_id, RECEIPT_NAME)] = receipt_signature(service.staging, batch_id)
            except ReceiptError as exc:
                raise ArchiveError(str(exc)) from exc
            postcopy = next((source.get(batch_id, {}).get('postcopy')
                             for source in evidence.values() if batch_id in source), None)
            if postcopy is not None:
                manifests[(batch_id, 'postcopy_receipt')] = postcopy['receipt_signature']

        def manifests_unchanged():
            # Bounded metadata checks between removals; do not reparse every
            # selected unit's entire batch for every single source file.
            for (batch_id,file_name), expected in manifests.items():
                if file_name == RECEIPT_NAME:
                    try:
                        actual = receipt_signature(service.staging, batch_id)
                    except ReceiptError as exc:
                        raise ArchiveError(str(exc)) from exc
                    if actual != expected:
                        raise ArchiveError('清理期间时间校正回执发生变化')
                    continue
                if file_name == 'postcopy_receipt':
                    try:
                        actual = candidate_signature(service.staging, batch_id)
                    except PostcopyError as exc:
                        raise ArchiveError(str(exc)) from exc
                    if actual != expected:
                        raise ArchiveError('清理期间独立校验回执发生变化')
                    continue
                with subdirectory(sources,batch_id) as batch:
                    handle = open_file(batch,file_name)
                    try:
                        if signature(handle)!=expected:
                            raise ArchiveError('清理期间成功清单发生变化')
                    finally:
                        os.close(handle)
        # Verify all companions and all units before removing the first source.
        for row in rows:
            target_check(row)
        for row, entry in zip(rows,journal['files']):
            if entry['source_path']!=row['source_path'] or entry['state'] not in ('pending','removing','removed'):
                raise ArchiveError('移动清理状态记录无效')
            if service.stop_event.is_set():
                raise ArchiveError('服务停止，移动进度已保留')
            service._check_roots()
            target_check(row)
            with subdirectory(sources,str(PurePosixPath(row['source_path']).parent)) as src:
                source_name = PurePosixPath(row['source_path']).name
                parent_id = persistent_identity(src)
                if entry.get('parent_identity') and entry['parent_identity']!=parent_id:
                    raise ArchiveError('中转源目录身份发生变化')
                try:
                    checked(src,source_name,row,row['source_signature'])
                except FileNotFoundError:
                    if entry['state'] not in ('removing','removed'):
                        raise ArchiveError('未开始移动的源文件已缺失，需核对：'+source_name)
                else:
                    if entry['state']=='removed':
                        raise ArchiveError('已清理的源路径重新出现文件，保留供核对')
                    entry.update(state='removing',parent_identity=parent_id)
                    atomic_json(state,name,journal)
                    service.checkpoint('move_intent',row)
                    # Recheck after the checkpoint immediately before unlink.
                    manifests_unchanged()
                    target_check(row)
                    checked(src,source_name,row,row['source_signature'])
                    os.unlink(source_name,dir_fd=src)
                    os.fsync(src)
                    service.checkpoint('source_removed',row)
                entry['state']='removed'
                atomic_json(state,name,journal)
                job['summary']['removed_sources']=sum(e['state']=='removed' for e in journal['files'])
                job['phase']='已校验并清理中转：'+source_name
                service._save(job)
        journal['completed_at']=now()
        atomic_json(state,name,journal)
        job.update(status='completed',phase='移动归档完成，目标已校验，中转源文件已清理',completed_at=now(),errors=[])
        service._save(job)
        return job
