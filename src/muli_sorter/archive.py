"""Independent-copy core used by the synthetic runner and confirmed job service."""
from contextlib import ExitStack
from datetime import datetime, timezone
import os
import re
from pathlib import Path, PurePosixPath
import unicodedata
from .archive_copy import stage_file, verify_final
from .archive_targets import same_content
from .archive_fixture import MARKER
from .archive_io import ArchiveError, atomic_json, directory, exclusive_lock, hash_fd, open_file, signature, subdirectory, verified_target_hash
from .archive_source import verify_sources
from .intake import decode
from .review import digest, validate_decisions

CATEGORIES = {'photo':'1相机原素材','video':'2视频素材/未指定机位','audio':'3录音素材'}


def now():
    return datetime.now(timezone.utc).isoformat()


def record(fd, name, default=None):
    try:
        file = open_file(fd, name)
    except FileNotFoundError:
        return default
    with os.fdopen(file, 'rb') as stream:
        raw = stream.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024:
        raise ArchiveError('状态记录过大')
    return decode(raw)


def exists(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def target_rows(unit, project):
    if unit['kind'] not in CATEGORIES:
        raise ArchiveError('代理或辅助文件不能单独归档')
    result, seen = [], set()
    for file in unit['files']:
        target = '/'.join((project['path'], CATEGORIES[unit['kind']], '独立归档', unit['unit_id'], file['name']))
        folded = unicodedata.normalize('NFC', target).casefold()
        if folded in seen:
            raise ArchiveError('目标文件名冲突')
        seen.add(folded)
        result.append({**file, 'target_path':target, 'target_name':PurePosixPath(target).name,
                       'temp':'.muli-'+digest([target, file['blake3']])[:24]+'.partial'})
    return result


def _unit(root, source_fd, projects_fd, state_fd, model, decision, unit, project, index, runtime, checkpoint, chunk_size,
          *, source_verifier=None, production=False, rows_factory=target_rows, skip_identical=False):
    verify = source_verifier or (lambda u: verify_sources(root/'staging', u, runtime))
    evidence = verify(unit)
    identity = {'unit_id':unit['unit_id'],'project_id':project['project_id'],'project_path':project['path'],'files':rows_factory(unit,project)}
    jid = digest(identity)
    if unit['unit_id'] in index and index[unit['unit_id']] != jid:
        raise ArchiveError('该单元已有其他项目或目标计划，停止重复归档')
    # The project must already exist. Only subdirectories within it may be created.
    with subdirectory(projects_fd, project['path']):
        pass
    filename = 'job-'+jid+'.json'
    job = record(state_fd, filename)
    if job is None:
        for row in identity['files']:
            with subdirectory(projects_fd,str(PurePosixPath(row['target_path']).parent),create=True) as dest:
                if exists(dest,row['temp']):
                    raise ArchiveError('目标或临时文件已经存在且不属于已登记任务，禁止覆盖')
                if exists(dest,row['target_name']) and not (skip_identical and same_content(dest,row['target_name'],row)):
                    raise ArchiveError('目标已存在且内容不一致，禁止覆盖')
        # Reserve the ownership before writing the intent. A crash after this
        # point is recoverable from the small pending journal and cannot be
        # mistaken for an unowned unit on the next submission.
        from .archive_ownership_cache import clear_intent, reserve_intent, write_ready
        reserve_intent(state_fd, unit['unit_id'], jid, index)
        job = {'identity':identity,'report_id':model['report_id'],'decision_id':digest(decision),'example_data':not production,
               'created_at':now(),'state':'in_progress','files':[dict(row) for row in identity['files']]}
        atomic_json(state_fd,filename,job)
        checkpoint('intent_recorded',job)
        index[unit['unit_id']] = jid
        atomic_json(state_fd,'archive-index.json',index)
        write_ready(state_fd, index)
        clear_intent(state_fd, unit['unit_id'], jid)
    elif job.get('identity') != identity or unit['unit_id'] not in index:
        raise ArchiveError('任务记录与索引不一致，保留现场供核对')
    rows = job['files']
    if len(rows) != len(identity['files']) or any(any(row.get(k)!=expected[k] for k in expected) for row,expected in zip(rows,identity['files'])):
        raise ArchiveError('任务文件记录被改动')
    stats = {'written_bytes':0,'resumed_bytes':0,'reused_files':0,'skipped_files':0}

    def persist():
        job['updated_at'] = now()
        atomic_json(state_fd,filename,job)

    def parents(row):
        stack = ExitStack()
        try:
            src = stack.enter_context(subdirectory(source_fd,str(PurePosixPath(row['source_path']).parent)))
            dst = stack.enter_context(subdirectory(projects_fd,str(PurePosixPath(row['target_path']).parent)))
            return stack, src, dst
        except BaseException:
            stack.close()
            raise

    def finalize_existing(row, src, dst):
        if not row.get('source_signature'):
            if not skip_identical:
                raise ArchiveError('目标未登记，禁止覆盖')
            source = open_file(src,PurePosixPath(row['source_path']).name)
            try:
                before = signature(source)
                if before[2] != row['size_bytes'] or hash_fd(source) != row['blake3']:
                    raise ArchiveError('源内容与成功清单不一致')
                row['source_signature'] = before
                # Persisted only after both actual files pass verify_final.
                row['skipped_existing'] = True
            finally:
                os.close(source)
        result = verify_final(src,PurePosixPath(row['source_path']).name,dst,row)
        if exists(dst,row['temp']):
            temp = open_file(dst,row['temp'])
            try:
                if signature(temp)[:2] != result[:2] or result[:2] != row.get('temp_identity'):
                    raise ArchiveError('正式文件与临时文件关系不符')
            finally:
                os.close(temp)
            os.unlink(row['temp'],dir_fd=dst)
            os.fsync(dst)
        final_stat = os.stat(row['target_name'],dir_fd=dst,follow_symlinks=False)
        if final_stat.st_nlink != 1:
            raise ArchiveError('目标存在额外硬链接，不能验证独立副本边界')
        row['published'] = True
        target_fd = open_file(dst, row['target_name'])
        try:
            row['target_signature'] = signature(target_fd)
        finally:
            os.close(target_fd)
        persist()

    try:
        job['state'] = 'in_progress'
        job.pop('error',None)
        persist()
        # All companion files are staged and verified before the first publication.
        for row in rows:
            stack, src, dst = parents(row)
            with stack:
                if exists(dst,row['target_name']):
                    finalize_existing(row,src,dst)
                    stats['reused_files'] += 1
                    stats['skipped_files'] += 1
                elif row.get('published'):
                    raise ArchiveError('先前已发布文件缺失，停止自动重建')
                else:
                    free = os.fstatvfs(dst)
                    if free.f_bavail * free.f_frsize < row['size_bytes']:
                        raise ArchiveError('目标可用空间不足')
                    def progress(key,size):
                        stats[key] += size
                    stage_file(src,PurePosixPath(row['source_path']).name,dst,row,persist,checkpoint,chunk_size,progress=progress)
        checkpoint('unit_staged',job)
        for row in rows:
            if verify(unit) != evidence:
                raise ArchiveError('执行期间成功清单发生变化')
            stack, src, dst = parents(row)
            with stack:
                if not exists(dst,row['target_name']):
                    temp = open_file(dst,row['temp'])
                    source = open_file(src,PurePosixPath(row['source_path']).name)
                    try:
                        if signature(temp)[:2] != row.get('temp_identity') or os.fstat(temp).st_size != row['size_bytes'] or signature(source) != row.get('source_signature') or hash_fd(source) != row['blake3']:
                            raise ArchiveError('发布前内容证据不一致')
                        verified_target_hash(temp, row['blake3'])
                        if signature(source) != row.get('source_signature'):
                            raise ArchiveError('发布前内容证据不一致')
                    finally:
                        os.close(temp)
                        os.close(source)
                    # Link is only a no-overwrite publish of the NEW copy, never the source.
                    os.link(row['temp'],row['target_name'],src_dir_fd=dst,dst_dir_fd=dst,follow_symlinks=False)
                    os.fsync(dst)
                    checkpoint('published',row)
                finalize_existing(row,src,dst)
        if verify(unit) != evidence:
            raise ArchiveError('生成回执前来源清单发生变化')
        for row in rows:
            stack, src, dst = parents(row)
            with stack:
                verify_final(src,PurePosixPath(row['source_path']).name,dst,row)
        receipt = {'schema_version':'archive/0.4' if production else 'synthetic-archive/0.3',
                   'example_data':not production,'real_media_write_authorized':production,
                   'job_id':jid,'unit_id':unit['unit_id'],'project':project,'report_id':model['report_id'],'decision_id':digest(decision),
                   'status':'completed','completed_at':now(),'file_count':len(rows),'files':rows,'source_evidence':evidence,
                   'storage':'independent_copy','provenance':unit['provenance']}
        atomic_json(state_fd,'receipt-'+jid+'.json',receipt)
        checkpoint('receipt',receipt)
        job['state'] = 'completed'
        persist()
        return {'unit_id':unit['unit_id'],'status':'completed','files':len(rows),'receipt':'receipt-'+jid+'.json',**stats}
    except Exception as exc:
        exc.archive_stats = stats
        job['state'] = 'incomplete'
        job['error'] = str(exc)
        persist()
        raise


def recover_index_full(state):
    """Rebuild the unit ownership ledger from durable copy intents after a crash."""
    index = record(state, 'archive-index.json', {})
    old_index = dict(index)
    names = os.listdir(state)
    if len(names) > 100_000:
        raise ArchiveError('状态目录过大，停止自动恢复')
    for name in names:
        if not re.fullmatch(r'job-[0-9a-f]{64}\.json', name):
            continue
        job = record(state, name)
        identity = job['identity']
        jid = digest(identity)
        uid = identity['unit_id']
        if name != 'job-' + jid + '.json' or (uid in index and index[uid] != jid):
            raise ArchiveError('已登记任务互相冲突或被修改')
        index[uid] = jid
    if index != old_index:
        atomic_json(state, 'archive-index.json', index)
    from .archive_ownership_cache import write_ready
    write_ready(state, index)
    return index


def recover_index(state, selected, *, progress=None, require_ready=True,
                  allow_pending_without_job=None):
    from .archive_ownership_cache import recover
    return recover(state, selected, progress=progress, require_ready=require_ready,
                   allow_pending_without_job=allow_pending_without_job)


def run_synthetic(root, model, decisions, *, runtime_provider=None, checkpoint=None, chunk_size=1024*1024):
    """No production CLI: both model and every manifest must be synthetic."""
    if model.get('example_data') is not True:
        raise ArchiveError('当前仅支持合成实验，不接受真实素材确认模型')
    validate_decisions(model,decisions)
    root = Path(root).absolute()
    if str(root).startswith('/Volumes/'):
        raise ArchiveError('演示入口不接受挂载卷路径')
    checkpoint = checkpoint or (lambda *args:None)
    if not isinstance(chunk_size,int) or not 1 <= chunk_size <= 8*1024*1024:
        raise ArchiveError('复制块大小无效')
    with ExitStack() as stack:
        root_fd = stack.enter_context(directory(root))
        if record(root_fd,'SYNTHETIC_ONLY.json') != MARKER:
            raise ArchiveError('缺少独立合成实验标记')
        stack.enter_context(exclusive_lock(root_fd))
        source = stack.enter_context(subdirectory(root_fd,'staging'))
        projects = stack.enter_context(subdirectory(root_fd,'projects'))
        state = stack.enter_context(subdirectory(root_fd,'state'))
        runtime = runtime_provider or (lambda:record(root_fd,'runtime-state.json'))
        units = {u['unit_id']:u for u in model['units']}
        project_map = {p['project_id']:p for p in model['projects']}
        scope = {}
        for segment in decisions['segments']:
            if segment['decision'] != 'confirmed':
                continue
            for uid in segment['unit_ids']:
                unit, project = units[uid], project_map[segment['project_id']]
                if unit['kind'] not in CATEGORIES:
                    # _unit reports unsupported proxy/auxiliary units in its
                    # normal per-unit error path; scope preparation must not
                    # turn that expected outcome into a top-level failure.
                    continue
                identity = {'unit_id':uid,'project_id':project['project_id'],'project_path':project['path'],
                            'files':target_rows(unit,project)}
                scope[uid] = digest(identity)
        index = recover_index(state, scope, require_ready=False)
        outcomes = []
        for segment in decisions['segments']:
            for uid in segment['unit_ids']:
                if segment['decision'] != 'confirmed':
                    outcomes.append({'unit_id':uid,'status':segment['decision'],'files':len(units[uid]['files'])})
                    continue
                try:
                    outcomes.append(_unit(root,source,projects,state,model,decisions,units[uid],project_map[segment['project_id']],index,runtime,checkpoint,chunk_size))
                except (ArchiveError,OSError,ValueError,KeyError,TypeError) as exc:
                    outcomes.append({'unit_id':uid,'status':'incomplete','error':str(exc),**getattr(exc,'archive_stats',{})})
        completed = [o for o in outcomes if o['status']=='completed']
        report = {'mode':'synthetic_archive','example_data':True,'real_media_write_authorized':False,'generated_at':now(),
                  'status':'completed' if len(completed)==len(outcomes) else 'partial', 'outcomes':outcomes,
                  'summary':{'completed_units':len(completed),'completed_files':sum(o['files'] for o in completed),
                             'incomplete_units':sum(o['status']=='incomplete' for o in outcomes),
                             'pending_units':sum(o['status']=='pending' for o in outcomes),'deferred_units':sum(o['status']=='deferred' for o in outcomes),
                             'written_bytes':sum(o.get('written_bytes',0) for o in outcomes),'resumed_bytes':sum(o.get('resumed_bytes',0) for o in outcomes),
                             'reused_files':sum(o.get('reused_files',0) for o in outcomes)}}
        atomic_json(state,'latest-run.json',report)
        return report
