"""Opt-in, whole-request same-mount move with durable per-file rename intent.

Reuse a bound full-read receipt only while the complete source identity stays
unchanged; otherwise read the source fully. Rename keeps the inode. Recovery
rehashes destinations and never guesses ownership from a name or content.
"""
from copy import deepcopy
import os
from pathlib import PurePosixPath
import time

from .archive_diagnostics import diagnostic_context
from .archive import exists, now, record, recover_index
from .archive_io import (ArchiveError, atomic_json, directory, hash_fd, open_file,
                         persistent_identity, signature, subdirectory)
from .archive_layout import STANDARD_FOLDERS
from .archive_move import require_ingest_idle, request_batches
from .archive_options import options_for
from .archive_rename_io import STRATEGY, mount_id, rename_noreplace, view_roots
from .archive_source import verify_sources, _attributes_touched_only
from .archive_direct_journal import DirectJournal, ENTRY_STORAGE
from .archive_direct_identical import require_identical_target
from .archive_target_metadata import MetadataProofCache, check_named_target
from .manual_folders import _create, _inspect
from .manual_projects import fold
from .review import digest
from .staging_coordination import staging_guard, batch_guard


def _checked(fd, name, row, expected=None, *, moved=False, content=False, verified_source=None, progress=None):
    handle = open_file(fd, name)
    try:
        actual = signature(handle)
        if (actual[2] != row['size_bytes'] or os.fstat(handle).st_nlink != 1 or
                (expected is not None and (actual[:4] != expected[:4] if moved else actual != expected))):
            raise ArchiveError('直接移动文件身份、大小或链接发生变化：' + name)
        if verified_source is not None:
            info = os.fstat(handle)
            current = {'dev':info.st_dev, 'ino':info.st_ino, 'size':info.st_size,
                       'mtime_ns':info.st_mtime_ns, 'ctime_ns':info.st_ctime_ns}
            if current != verified_source:
                # Before creating a rename intent, an attribute-only change may
                # be proved by reading this source once. The returned fresh
                # identity then binds the journal; later mutation checks stay
                # strict and never adopt a new identity after the intent.
                if not content or not _attributes_touched_only(current, verified_source):
                    exc = ArchiveError('复用校验前来源文件身份改变：' + name)
                    exc.archive_location = {'changed_signature_fields': [
                        {'field':key, 'expected':verified_source.get(key), 'actual':value}
                        for key, value in current.items() if verified_source.get(key) != value]}
                    raise exc
                if progress:
                    progress(0)
                if hash_fd(handle, **({'progress':progress} if progress else {})) != row['blake3']:
                    raise ArchiveError('来源文件属性变化且内容摘要与成功清单不一致：' + name)
        elif content and hash_fd(handle, **({'progress':progress} if progress else {})) != row['blake3']:
            raise ArchiveError('直接移动内容与成功清单不一致：' + name)
        again = open_file(fd, name)
        try:
            final = signature(handle)
            if final != actual or signature(again) != final or os.fstat(handle).st_nlink != 1:
                raise ArchiveError('直接移动路径在核对时被替换：' + name)
        finally:
            os.close(again)
        return final
    finally:
        os.close(handle)


def _evidence(service, selected, cache=None):
    cache = {} if cache is None else cache
    result = {}
    for item in selected:
        with diagnostic_context(unit_id=item['unit']['unit_id'], project_id=item['project']['project_id']):
            unit = item['unit']
            result[unit['unit_id']] = verify_sources(
                service.staging, unit, service.runtime, production=service.production,
                reviewed_metadata=True, cache=cache, check_media=False,
                companion_parent=unit.get('_companion_parent'))
    return result


def _folders(service, job, prepared, targets):
    for row in prepared['projects']:
        path = row['relative_path']
        if row['action'] == 'create' and path not in job['folders']:
            job['folders'][path] = _create(targets, path)
            service._save(job)
        if _inspect(targets, path) != (job['target_identities'][path] or job['folders'].get(path)):
            raise ArchiveError('直接移动项目目录身份不一致')
        for child in STANDARD_FOLDERS:
            _inspect(targets, path + '/' + child)
            with subdirectory(targets, path + '/' + child, create=True):
                pass


def _confirmed_parents(job, request, targets):
    """Check each selected destination directory once, without opening media."""
    expected = request['target_parent_identities']
    paths = {str(PurePosixPath(r['target_path']).parent) for r in request['file_plans'].values()}
    if set(expected) != paths:
        raise ArchiveError('确认计划缺少目标目录身份')
    owned = job.get('execution_parent_identities', {})
    for path, identity in expected.items():
        if _inspect(targets, path) != owned.get(path, identity):
            raise ArchiveError('确认后的目标子目录已变化：' + path)


def _target_listing(targets, plans):
    rows = {}
    for row in plans.values():
        with diagnostic_context(source_path=row['source_path'], name=row.get('name'), target_path=row['target_path']):
            rows.setdefault(str(PurePosixPath(row['target_path']).parent), []).append(row)
    result = {}
    for path, items in rows.items():
        with subdirectory(targets, path) as fd:
            before = signature(fd)
            aliases = {}
            for name in os.listdir(fd):
                aliases.setdefault(fold(name), []).append(name)
            for row in items:
                with diagnostic_context(source_path=row['source_path'], name=row.get('name'), target_path=row['target_path']):
                    for name in (row['target_name'], row['temp']):
                        if any(existing != name for existing in aliases.get(fold(name), ())):
                            raise ArchiveError('目标文件名称存在冲突：' + name)
            if signature(fd) != before:
                raise ArchiveError('读取目标目录期间内容变化：' + path)
            result[path] = (persistent_identity(fd), before)
    return result


def run_direct_move(service, job, request):
    if (not service.enabled or not service.move_enabled or
            options_for(request['decisions'])['mode'] != 'move' or
            request.get('execution_strategy') != STRATEGY or job.get('execution_strategy') != STRATEGY):
        raise ArchiveError('本任务没有同卷直接移动授权')
    if not service.production:
        with staging_guard(service.staging, create=True):
            pass
    job.update(status='running', phase='等待素材使用结束，准备同卷直接移动')
    service._save(job)
    batches = request_batches(request)
    with batch_guard(service.staging, batches, exclusive=True, wait=True, cancel=service.stop_event) as scoped:
        require_ingest_idle(service, batches if scoped else None)
        job['phase'] = '核对归档记录与待移动范围'
        service._save(job)
        with view_roots(service) as (sources, targets), directory(service.state/'requests') as requests, directory(service.state/'units') as receipts:
            return _run_locked(service, job, request, sources, targets, requests, receipts)


def _run_locked(service, job, request, sources, targets, requests, receipts):
    last_progress = 0.0
    target_metadata_proofs = MetadataProofCache()

    def progress(force=False):
        nonlocal last_progress
        timestamp = time.monotonic()
        if force or timestamp - last_progress >= 1:
            service._save(job)
            last_progress = timestamp

    name = 'direct-move-' + job['job_id'] + '.json'
    journal = record(requests, name)
    owner = {'job_id': job['job_id'], 'request_digest': digest(request),
             'roots': service.identity, 'file_plans': request['file_plans'], 'strategy': STRATEGY}
    from .archive_ownership_cache import (clear_many, job_progress,
                                          reserve_many, write_ready)
    recovering = journal is not None
    if journal is None:
        job['phase'] = '核对直接移动来源和目标'
        service._save(job)
        if request.get('admission_policy') == 'confirmed-plan/v1':
            # The confirmed request already contains the frozen target plan.
            # Rebind its selected units, without re-planning directories or
            # rehashing targets. File opens and batch guards below are the
            # mutation boundary, including stale-source and occupied-target checks.
            _, selected, _, _ = service._compile(request['model'], request['decisions'])
            for item in selected:
                item['rows'] = [request['file_plans'][r['source_path']] for r in item['rows']]
            if {r['source_path']:r for item in selected for r in item['rows']} != request['file_plans']:
                raise ArchiveError('已确认计划文件范围不闭合')
            prepared = {'selected':selected, 'projects':job['projects'],
                        'job_id':service._planned_id(selected,request['decisions']),
                        'execution_strategy':request['execution_strategy'],
                        'file_plans':request['file_plans'],
                        'evidence':_evidence(service, selected),
                        'validated_signatures':request.get('validated_signatures',{})}
        else:
            # Existing requests keep their original execution contract.
            prepared = service._prepare(request['model'], request['decisions'], accepted=job)
        if (prepared['job_id'] != job['job_id'] or prepared.get('execution_strategy') != STRATEGY or
                prepared['file_plans'] != request['file_plans'] or
                prepared['evidence'] != request['source_evidence']):
            raise ArchiveError('直接移动条件或确认范围发生变化，请核对')
        selected = prepared['selected']
        scope = {item['unit']['unit_id']:digest(_unit_identity(item)) for item in selected}
        index = recover_index(receipts, scope, progress=job_progress(service, job),
                              require_ready=service.production)
        policy = request.get('verification_policy')
        if policy not in (None, 'postcopy-signature-or-full-read/v1'):
            raise ArchiveError('直接移动内容核对策略不受支持')
        proofs = prepared['validated_signatures'] if policy else {}
        if policy and proofs != request.get('validated_signatures'):
            raise ArchiveError('直接移动独立校验回执身份改变')
        # Check ownership before creating directories or touching any media.
        for item in selected:
            identity = _unit_identity(item)
            uid, jid = item['unit']['unit_id'], digest(identity)
            if uid in index or record(receipts, 'job-' + jid + '.json') is not None:
                raise ArchiveError('该素材已有归档执行记录，不能改用直接移动')
        confirmed = request.get('admission_policy') == 'confirmed-plan/v1'
        if confirmed:
            _confirmed_parents(job, request, targets)
        _folders(service, job, prepared, targets)
        listings = _target_listing(targets, request['file_plans']) if confirmed else {}
        if confirmed:
            job['execution_parent_identities'] = {p:value[0] for p,value in listings.items()}
            service._save(job)
        entries = []
        planned_targets = {}
        job['verification'] = {'checked_files':0, 'total_files':len(request['file_plans']),
                               'checked_bytes':0, 'total_bytes':sum(r['size_bytes'] for r in request['file_plans'].values()),
                               'reused_files':0, 'hashed_files':0}
        for item in selected:
            for row in item['rows']:
                with diagnostic_context(source_path=row['source_path'], name=row.get('name'), target_path=row['target_path']):
                    if service.stop_event.is_set():
                        raise ArchiveError('服务停止，尚未移动素材')
                    with subdirectory(sources, str(PurePosixPath(row['source_path']).parent)) as src, subdirectory(targets, str(PurePosixPath(row['target_path']).parent), create=True) as dst:
                        parent = str(PurePosixPath(row['target_path']).parent)
                        if confirmed and persistent_identity(dst) != listings[parent][0]:
                            raise ArchiveError('执行前目标子目录被替换：' + parent)
                        if mount_id(src) != mount_id(dst):
                            raise ArchiveError('执行前挂载发生变化，保留来源')
                        if exists(dst, row['temp']):
                            raise ArchiveError('直接移动临时文件被占用')
                        proof = proofs.get(row['source_path'])
                        job['phase'] = ('核对已校验文件身份：' if proof else '直接移动前完整核对内容：') + PurePosixPath(row['source_path']).name
                        verified_bytes = job['verification']['checked_bytes']
                        source_hashed = False
                        def reading(amount):
                            nonlocal source_hashed
                            if service.stop_event.is_set():
                                raise ArchiveError('服务停止，尚未移动素材')
                            if proof and not source_hashed:
                                job['phase'] = '来源属性变化，重新核对本文件内容：' + PurePosixPath(row['source_path']).name
                            source_hashed = True
                            job['verification']['checked_bytes'] += amount
                            progress()
                        progress()
                        sig = _checked(src, PurePosixPath(row['source_path']).name, row, content=True, verified_source=proof, progress=reading)
                        prior = planned_targets.get(row['target_path'])
                        target_preexisting = exists(dst, row['target_name'])
                        target_signature = None
                        if target_preexisting:
                            if options_for(request['decisions'])['existing'] != 'skip_identical':
                                raise ArchiveError('直接移动目标已存在，禁止覆盖：' + row['target_name'])
                            _, target_signature = require_identical_target(
                                dst, row, cache=getattr(service, 'target_digests', None))
                            method = 'skip_identical'
                        elif prior is not None:
                            if (prior['blake3'] != row['blake3'] or
                                    prior['size_bytes'] != row['size_bytes']):
                                raise ArchiveError('直接移动同批目标冲突：' + row['target_name'])
                            # The first row owns the new target; later rows are
                            # duplicate sources and are unlinked only after that
                            # target has been fully verified.
                            method = 'skip_identical'
                        else:
                            method = 'rename'
                        entry = {'source_path':row['source_path'], 'state':'pending',
                                 'method':method, 'target_digest':row['blake3'],
                                 'target_preexisting':target_preexisting,
                                 'source_signature':sig, 'source_parent':persistent_identity(src),
                                 'target_parent':persistent_identity(dst)}
                        if target_signature is not None:
                            entry['target_signature'] = target_signature
                        entries.append(entry)
                        planned_targets[row['target_path']] = row
                        job['verification']['checked_files'] += 1
                        job['verification']['checked_bytes'] = verified_bytes + row['size_bytes']
                        job['verification']['reused_files' if proof and not source_hashed else 'hashed_files'] += 1
        progress(force=True)
        if _evidence(service, selected) != request['source_evidence']:
            raise ArchiveError('直接移动前成功清单发生变化')
        for path, (_, expected) in listings.items():
            with subdirectory(targets, path) as fd:
                if signature(fd) != expected:
                    raise ArchiveError('准备移动期间目标目录变化：' + path)
        journal = {'identity':owner, 'selected':selected, 'files':entries, 'created_at':now(),
                   'entry_storage':ENTRY_STORAGE}
        atomic_json(requests, name, journal)
        service.checkpoint('direct_move_prepared', journal)
    if journal.get('identity') != owner:
        raise ArchiveError('直接移动日志与原确认任务不一致')
    journal_io = DirectJournal(requests, name, journal, recovering=recovering)
    selected = journal['selected']
    # Journals written by the original all-rename implementation have no
    # method field.  They are safely interpreted as rename entries and retain
    # their original identity binding.
    for entry in journal['files']:
        entry.setdefault('method', 'rename')
        entry.setdefault('target_preexisting', False)
    if recovering:
        scope = {item['unit']['unit_id']:digest(_unit_identity(item)) for item in selected}
        index = recover_index(receipts, scope, progress=job_progress(service, job),
                              require_ready=service.production,
                              allow_pending_without_job=scope)
    # Rebuild the selection without requiring sources already moved to exist.
    _, compiled, _, _ = service._compile(request['model'], request['decisions'])
    if len(compiled) != len(selected):
        raise ArchiveError('直接移动日志单元范围改变')
    for expected, item in zip(compiled, selected):
        rows = [request['file_plans'][r['source_path']] for r in expected['rows']]
        if item != {**expected, 'rows':rows}:
            raise ArchiveError('直接移动日志项目或文件计划改变')
    rows = [r for item in selected for r in item['rows']]
    if (len(rows) != len(request['file_plans']) or len(journal['files']) != len(rows) or
            {r['source_path']:r for r in rows} != request['file_plans']):
        raise ArchiveError('直接移动范围未闭合')

    evidence_cache = {}
    item_by_source = {r['source_path']:item for item in selected for r in item['rows']}

    def guards(row=None):
        if service.stop_event.is_set():
            raise ArchiveError('服务停止，直接移动进度已记录')
        # Reopen the alias roots to detect replacement of the common view.
        # view_roots also reopens and validates the registered source/target
        # roots; doing that separately repeats the identical path traversal.
        with view_roots(service):
            pass
        for path, original in job['target_identities'].items():
            if _inspect(targets, path) != (original or job['folders'].get(path)):
                raise ArchiveError('移动期间项目目录发生变化')
        subset = [item_by_source[row['source_path']]] if row else selected
        expected = {i['unit']['unit_id']:request['source_evidence'][i['unit']['unit_id']] for i in subset}
        if _evidence(service, subset, evidence_cache) != expected:
            raise ArchiveError('移动期间成功清单或批次状态发生变化')

    guards()
    def checked_target(dst, row, expected):
        started = False
        def reading(amount):
            nonlocal started
            if service.stop_event.is_set():
                raise ArchiveError('服务停止，目标属性核对尚未完成')
            job['phase'] = '目标属性变化，核对该文件内容：' + row['target_name']
            counts = job.setdefault('verification', {})
            if not started:
                counts['hashed_files'] = counts.get('hashed_files', 0) + 1
                counts['metadata_rechecked_files'] = counts.get('metadata_rechecked_files', 0) + 1
                started = True
            counts['metadata_read_bytes'] = counts.get('metadata_read_bytes', 0) + amount
            progress()
        return check_named_target(dst, row, expected, cache=target_metadata_proofs,
                                  progress=reading)

    def target_readback(dst, row, entry):
        expected = entry.get('target_signature')
        if entry.get('method') == 'rename':
            # A recorded target may acquire NAS attributes after rename. A
            # ctime-only difference requires content proof, not a blind rebind.
            # An interrupted rename without a target signature still needs the
            # original conservative full read against its source inode.
            actual = (checked_target(dst, row, expected)
                      if expected is not None else
                      _checked(dst, row['target_name'], row, entry['source_signature'],
                               moved=True, content=True))
        else:
            # Existing/duplicate targets are bound by both digest and name
            # signature; the service cache may reuse a complete read.
            _, actual = require_identical_target(
                dst, row, expected_signature=expected,
                cache=getattr(service, 'target_digests', None), metadata_compatible=True)
        # Existing targets' original signatures are fixed by DirectJournal.
        # Keep that binding; final receipts carry the freshly verified value.
        if not entry.get('target_preexisting'):
            entry['target_signature'] = actual
        return actual

    def check_entry_paths(row, entry, src, dst):
        if (entry.get('source_path') != row['source_path'] or
                entry.get('method') not in ('rename', 'skip_identical') or
                entry.get('state') not in ('pending', 'renaming', 'removed')):
            raise ArchiveError('直接移动混合日志条目无效')
        _parents(src, dst, entry)
        source_name = PurePosixPath(row['source_path']).name
        source_exists = exists(src, source_name)
        target_exists = exists(dst, row['target_name'])
        if entry['state'] == 'removed':
            if source_exists:
                raise ArchiveError('直接移动来源重新出现')
            if not target_exists:
                raise ArchiveError('直接移动已完成目标缺失')
            target_readback(dst, row, entry)
            return source_exists, target_exists
        if entry['method'] == 'rename':
            if source_exists:
                if target_exists:
                    raise ArchiveError('直接移动目标已占用，拒绝覆盖')
                _checked(src, source_name, row, entry['source_signature'], content=recovering)
            elif entry['state'] == 'pending':
                raise ArchiveError('尚未开始直接移动的来源缺失')
            else:
                if not target_exists:
                    raise ArchiveError('直接移动目标缺失，不能采用已移出来源')
                target_readback(dst, row, entry)
                entry['state'] = 'removed'
                journal_io.save_entry(entry)
        else:
            if not target_exists:
                raise ArchiveError('直接移动相同目标缺失')
            target_readback(dst, row, entry)
            if source_exists:
                _checked(src, source_name, row, entry['source_signature'], content=recovering)
            elif entry['state'] == 'pending':
                raise ArchiveError('尚未开始清理的重复来源缺失')
        return source_exists, target_exists

    # Validate every journal entry before mutating anything. Rename entries
    # are checked first so same-batch duplicate sources can observe the newly
    # created target during the cleanup barrier.
    for row, entry in zip(rows, journal['files']):
        with diagnostic_context(unit_id=item_by_source[row['source_path']]['unit']['unit_id'], source_path=row['source_path'], name=row['name'], target_path=row['target_path']):
            if entry.get('method') != 'rename':
                continue
            with subdirectory(sources, str(PurePosixPath(row['source_path']).parent)) as src, subdirectory(targets, str(PurePosixPath(row['target_path']).parent)) as dst:
                check_entry_paths(row, entry, src, dst)
    journal_io.save()

    direct_count = sum(e.get('method') == 'rename' for e in journal['files'])
    skipped_count = sum(e.get('method') == 'skip_identical' for e in journal['files'])
    moved_count = sum(e.get('method') == 'rename' and e['state'] == 'removed' for e in journal['files'])
    count = sum(e['state'] == 'removed' for e in journal['files'])
    # Move new targets while their sources still have their bound identity.
    for row, entry in zip(rows, journal['files']):
        with diagnostic_context(unit_id=item_by_source[row['source_path']]['unit']['unit_id'], source_path=row['source_path'], name=row['name'], target_path=row['target_path']):
            if entry['method'] != 'rename' or entry['state'] == 'removed':
                continue
            guards(row)
            with subdirectory(sources, str(PurePosixPath(row['source_path']).parent)) as src, subdirectory(targets, str(PurePosixPath(row['target_path']).parent)) as dst:
                _parents(src, dst, entry)
                source_name = PurePosixPath(row['source_path']).name
                _checked(src, source_name, row, entry['source_signature'])
                entry['state'] = 'renaming'
                journal_io.save_entry(entry)  # intent durable BEFORE rename
                service.checkpoint('direct_move_intent', row)
                guards(row)
                with subdirectory(sources, str(PurePosixPath(row['source_path']).parent)) as fresh_src, subdirectory(targets, str(PurePosixPath(row['target_path']).parent)) as fresh_dst:
                    _parents(fresh_src, fresh_dst, entry)
                    _checked(fresh_src, source_name, row, entry['source_signature'])
                    rename_noreplace(fresh_src, source_name, fresh_dst, row['target_name'])
                    os.fsync(fresh_src); os.fsync(fresh_dst)
                    service.checkpoint('direct_move_renamed', row)
                    with subdirectory(sources, str(PurePosixPath(row['source_path']).parent)) as final_src, subdirectory(targets, str(PurePosixPath(row['target_path']).parent)) as final_dst:
                        _parents(final_src, final_dst, entry)
                        if exists(final_src, source_name):
                            raise ArchiveError('移动后来源重新出现，保留供核对')
                        entry['target_signature'] = _checked(final_dst, row['target_name'], row, entry['source_signature'], moved=True)
                entry['state'] = 'removed'
                journal_io.save_entry(entry)
                count += 1
            moved_count += 1
            job['summary'].update(direct_moved_files=moved_count, skipped_files=skipped_count,
                                  removed_sources=count)
            job['phase'] = '已直接移动：' + str(count) + '/' + str(len(rows))
            progress()

    # Barrier: every target, including existing identical targets and targets
    # created by the rename phase, must be verified before any duplicate
    # source is unlinked.
    for row, entry in zip(rows, journal['files']):
        with diagnostic_context(unit_id=item_by_source[row['source_path']]['unit']['unit_id'], source_path=row['source_path'], name=row['name'], target_path=row['target_path']):
            with subdirectory(sources, str(PurePosixPath(row['source_path']).parent)) as src, subdirectory(targets, str(PurePosixPath(row['target_path']).parent)) as dst:
                _parents(src, dst, entry)
                target_readback(dst, row, entry)
                if entry['method'] == 'skip_identical' and entry['state'] != 'removed':
                    source_name = PurePosixPath(row['source_path']).name
                    if not exists(src, source_name):
                        if entry['state'] == 'pending':
                            raise ArchiveError('尚未开始清理的重复来源缺失')
                        # The source may have been unlinked before a crash, with
                        # the durable removed transition still pending.
                        entry['state'] = 'removed'
                    else:
                        _checked(src, source_name, row, entry['source_signature'], content=recovering)
                if entry['state'] != 'pending':
                    journal_io.save_entry(entry)
    journal_io.save()

    # All destinations are ready. The unlink intent uses the same durable
    # ``renaming`` state; method distinguishes it from an interrupted rename.
    for row, entry in zip(rows, journal['files']):
        with diagnostic_context(unit_id=item_by_source[row['source_path']]['unit']['unit_id'], source_path=row['source_path'], name=row['name'], target_path=row['target_path']):
            if entry['method'] != 'skip_identical' or entry['state'] == 'removed':
                continue
            guards(row)
            with subdirectory(sources, str(PurePosixPath(row['source_path']).parent)) as src, subdirectory(targets, str(PurePosixPath(row['target_path']).parent)) as dst:
                _parents(src, dst, entry)
                source_name = PurePosixPath(row['source_path']).name
                if not exists(src, source_name):
                    if entry['state'] == 'pending':
                        raise ArchiveError('尚未开始清理的重复来源缺失')
                    entry['state'] = 'removed'
                    journal_io.save_entry(entry)
                    count += 1
                    continue
                entry['state'] = 'renaming'
                journal_io.save_entry(entry)
                service.checkpoint('direct_move_intent', row)
                guards(row)
                # Reopen both path parents after the durable intent. Held FDs do
                # not prove that the reviewed directory names still point to the
                # same objects.
                with subdirectory(sources, str(PurePosixPath(row['source_path']).parent)) as fresh_src, subdirectory(targets, str(PurePosixPath(row['target_path']).parent)) as fresh_dst:
                    _parents(fresh_src, fresh_dst, entry)
                    target_readback(fresh_dst, row, entry)
                    _checked(fresh_src, source_name, row, entry['source_signature'])
                    os.unlink(source_name, dir_fd=fresh_src)
                    os.fsync(fresh_src)
                service.checkpoint('direct_move_removed', row)
                entry['state'] = 'removed'
                journal_io.save_entry(entry)
                count += 1
            job['summary'].update(direct_moved_files=moved_count, skipped_files=skipped_count,
                                  removed_sources=count)
            job['phase'] = '已直接移动：' + str(count) + '/' + str(len(rows))
            progress()
    progress(force=True)
    guards()
    # Final metadata/identity readback (the same inode, not a newly written copy).
    final_rows = []
    for row, entry in zip(rows, journal['files']):
        with diagnostic_context(unit_id=item_by_source[row['source_path']]['unit']['unit_id'], source_path=row['source_path'], name=row['name'], target_path=row['target_path']):
            with subdirectory(sources, str(PurePosixPath(row['source_path']).parent)) as src, subdirectory(targets, str(PurePosixPath(row['target_path']).parent)) as dst:
                _parents(src, dst, entry)
                if exists(src, PurePosixPath(row['source_path']).name):
                    raise ArchiveError('直接移动来源重新出现')
                target_sig = checked_target(dst, row, entry['target_signature'])
                if not entry.get('target_preexisting') and target_sig != entry['target_signature']:
                    entry['target_signature'] = target_sig
                    journal_io.save_entry(entry)
                final = {**row, 'source_signature':entry['source_signature'],
                         'target_signature':target_sig, 'published':True,
                         'transfer_method':('skip_identical' if entry['method'] == 'skip_identical'
                                            else 'same_volume_rename')}
                if entry['method'] == 'skip_identical':
                    final['identical_target_blake3'] = row['blake3']
                final_rows.append(final)
    journal['completed_at'] = now()
    journal_io.save()
    # Publish the established MOVE history contract only after all files verify.
    move = {'identity':{'job_id':job['job_id'], 'request_digest':digest(request),
                        'roots':service.identity, 'files':final_rows},
            'files':[{'source_path':r['source_path'], 'state':'removed'} for r in final_rows],
            'completed_at':journal['completed_at'], 'strategy':STRATEGY}
    atomic_json(requests, 'move-' + job['job_id'] + '.json', move)
    by_source = {r['source_path']:r for r in final_rows}
    outcomes = []
    ownership_jobs = {item['unit']['unit_id']:digest(_unit_identity(item)) for item in selected
                      if item['unit']['unit_id'] not in index}
    reserve_many(receipts, ownership_jobs, index)
    decision_id = digest(request['decisions'])
    for item in selected:
        identity = _unit_identity(item)
        unit, project = item['unit'], item['project']
        uid, jid = unit['unit_id'], digest(identity)
        if uid in index and index[uid] != jid:
            raise ArchiveError('直接移动归属索引发生变化')
        old = record(receipts, 'job-' + jid + '.json')
        if old and (old['identity'] != identity or old.get('direct_move_owner') != job['job_id']):
            raise ArchiveError('直接移动单元记录被其他任务占用')
        unit_rows = [by_source[r['source_path']] for r in item['rows']]
        atomic_json(receipts, 'job-' + jid + '.json', {'identity':identity, 'state':'completed',
                    'direct_move_owner':job['job_id'], 'files':unit_rows, 'updated_at':now()})
        index[uid] = jid
        receipt = {'schema_version':'archive/0.4' if service.production else 'synthetic-archive/0.3',
                   'example_data':not service.production, 'real_media_write_authorized':service.production,
                   'job_id':jid, 'unit_id':uid, 'project':project, 'report_id':request['model']['report_id'],
                   'decision_id':decision_id, 'status':'completed', 'completed_at':now(),
                   'file_count':len(unit_rows), 'files':unit_rows,
                   'source_evidence':request['source_evidence'][uid], 'storage':'same_volume_move',
                   'provenance':unit['provenance']}
        atomic_json(receipts, 'receipt-' + jid + '.json', receipt)
        service.checkpoint('direct_move_receipt', receipt)
        outcomes.append({'unit_id':uid, 'status':'completed', 'files':len(unit_rows),
                         'receipt':'receipt-'+jid+'.json', 'bytes':sum(r['size_bytes'] for r in unit_rows),
                         'direct_moved_files':sum(r.get('transfer_method') == 'same_volume_rename' for r in unit_rows),
                         'written_bytes':0,
                         'skipped_files':sum(r.get('transfer_method') == 'skip_identical' for r in unit_rows),
                         'removed_sources':len(unit_rows)})
    # Per-unit intents above are already durable; recover_index reconstructs
    # this derived index if a crash occurs before this single final write.
    atomic_json(receipts, 'archive-index.json', index)
    write_ready(receipts, index)
    clear_many(receipts, ownership_jobs)
    job['summary'].update(completed_units=len(outcomes), completed_files=len(rows),
                          processed_bytes=sum(r['size_bytes'] for r in rows),
                          direct_moved_files=moved_count, copy_files=0, skipped_files=skipped_count,
                          removed_sources=len(rows))
    job.update(status='completed', phase='同卷直接移动完成，文件身份与来源移出已核对',
               cleanup_started=True, direct_move_recovery_required=False, completed_at=now(), outcomes=outcomes, errors=[],issues=[])
    service._save(job)
    return job


def _parents(src, dst, entry):
    if (persistent_identity(src) != entry['source_parent'] or
            persistent_identity(dst) != entry['target_parent'] or mount_id(src) != mount_id(dst)):
        raise ArchiveError('直接移动父目录或挂载身份发生变化')


def _unit_identity(item):
    return {'unit_id':item['unit']['unit_id'], 'project_id':item['project']['project_id'],
            'project_path':item['project']['path'], 'files':deepcopy(item['rows'])}
