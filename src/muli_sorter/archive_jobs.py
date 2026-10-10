"""Durable, single-worker archive jobs accepted from an explicitly confirmed preview."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import gzip
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import time

from .archive_diagnostics import diagnostic_context, diagnostic_issues
from .archive import _unit, record, recover_index
from .archive_io import ArchiveError, atomic_json, directory, exclusive_lock, subdirectory, persistent_identity
from .archive_source import verify_sources
from .archive_options import options_for
from .archive_targets import plan_rows, same_content
from .archive_move import finish_move
from .archive_history import ArchiveHistory
from .archive_companions import companion_rows
from .archive_checks import PreflightChecks, TargetDigestCache
from .archive_job_catalog import JobCatalog
from .material_triage import _clip, companion_links, standard_dji_proxy_file, support_reason
from .material_triage import build_material_state
from .discarded_view import project as project_discarded
from .archive_layout import LAYOUT_VERSION, STANDARD_FOLDERS, studio_target_rows
from .intake import relative
from .manual_folders import _create, _inspect
from .manual_projects import fold
from .matching import project_index
from .order_feed_io import read_json, atomic_json as save_json
from .review import compile_plan, digest, validate_model


JOB_ID = re.compile(r'[0-9a-f]{64}')
TERMINAL = {'completed', 'partial', 'failed'}


class MaterialReports:
    """Keep the four opened-page identities without retaining four object trees.

    These are private, in-memory JSON snapshots, not source or archive evidence.
    Lookups restore every field; callers still perform the usual live checks.
    The owning ArchiveJobs mutex serializes all accesses.
    """

    def __init__(self):
        self._rows = {}

    def __setitem__(self, key, model):
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode='wb', compresslevel=1, mtime=0) as stream:
            encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'))
            for chunk in encoder.iterencode(model):
                stream.write(chunk.encode('utf-8'))
        self._rows[key] = buffer.getvalue()

    def get(self, key, default=None):
        raw = self._rows.get(key)
        return default if raw is None else json.loads(gzip.decompress(raw))

    def __len__(self):
        return len(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def pop(self, key):
        # Eviction does not reconstruct a large snapshot that is being discarded.
        self._rows.pop(key)


def now():
    return datetime.now(timezone.utc).isoformat()


def copy_project(project):
    # A manually proposed directory obtains a directory-catalog ID on the next scan.
    # Copy ownership is tied to its path, not that temporary UI ID.
    result = deepcopy(project)
    result['project_id'] = 'dir-' + sha256(project['path'].encode()).hexdigest()[:16]
    result['name'] = result['name'].removeprefix('【待新建】').removeprefix('【待建目录】')
    return result


class ArchiveJobs:
    def __init__(self, staging, projects, state, model_provider, runtime_provider, *,
                 production=False, enabled=False, project_label=None, checkpoint=None, move_enabled=False, direct_move_view=None):
        self.staging, self.projects, self.state = [Path(p).absolute() for p in (staging, projects, state)]
        if any(self.state == p or self.state.is_relative_to(p) or p.is_relative_to(self.state)
               for p in (self.staging, self.projects)):
            raise ArchiveError('归档任务状态必须与素材和项目目录分开')
        self.provider, self.runtime = model_provider, runtime_provider
        self.production, self.enabled = production, enabled
        self.move_enabled = move_enabled
        self.direct_move_view = deepcopy(direct_move_view)
        self.label = project_label or str(self.projects)
        self.checkpoint = checkpoint or (lambda *args: None)
        self.stack = ExitStack()
        self.mutex = threading.RLock()
        self.previews = {}
        self.preflight_lock = threading.Lock()
        self.job_catalog = JobCatalog(self.state / 'requests')
        self.checks = PreflightChecks(self.preflight)
        self.material_reports = MaterialReports()
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.thread = None
        self.worker_busy = threading.Event()
        try:
            # Only this independent service directory may be initialized at startup.
            self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.state_fd = self.stack.enter_context(directory(self.state))
            self.stack.enter_context(exclusive_lock(self.state_fd))
            for name in ('requests', 'units'):
                with subdirectory(self.state_fd, name, create=True):
                    pass
            with directory(self.staging) as source, directory(self.projects) as target:
                identity = {'staging':str(self.staging),'projects':str(self.projects),
                            'source_identity':self._identity(source),'target_identity':self._identity(target),
                            'production':production,'layout_version':LAYOUT_VERSION,
                            'identity_scheme':'fsid-inode/v1'}
            old = record(self.state_fd, 'service-identity.json')
            if old is not None and old != identity:
                raise ArchiveError('任务目录的归档规则、素材来源、项目根目录或运行模式已改变，不能套用旧任务')
            atomic_json(self.state_fd, 'service-identity.json', identity)
            self.identity = identity
            from .archive_tickets import PersistentTickets
            self.previews = PersistentTickets(self.state/'confirmation-tickets', self.identity)
            self.target_digests = TargetDigestCache(disk_path=self.state/'target-digests-v1.sqlite3', ttl=30*24*3600)
            self.stack.callback(self.target_digests.close)
            self.history = ArchiveHistory(self.state, identity, self.label, self.production)
            from .archive_source_preparation import SourcePreparation
            self.source_preparation = SourcePreparation(self)
        except BaseException:
            self.stack.close()
            raise

    @staticmethod
    def _identity(fd):
        return persistent_identity(fd)

    def _check_roots(self):
        with directory(self.staging) as source, directory(self.projects) as target:
            if (self._identity(source) != self.identity['source_identity'] or
                    self._identity(target) != self.identity['target_identity']):
                raise ArchiveError('素材或项目根目录已改变')

    def close(self):
        self.stop_event.set()
        self.wake.set()
        self.checks.close()
        if self.thread is not None:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise ArchiveError('归档线程尚未停止，不能提前释放任务锁')
        self.source_preparation.close()
        self.stack.close()

    def model(self):
        model = self.provider()
        validate_model(model)
        if model.get('example_data') is not (not self.production):
            raise ArchiveError('确认模型与服务的真实/演示模式不一致')
        return model

    def current_report_id(self):
        getter = getattr(self.provider, 'current_report_id', None)
        return getter() if getter is not None else self.model()['report_id']

    @staticmethod
    def _selection_signature(selected):
        return digest(sorted(selected, key=lambda row: row['unit']['unit_id']))

    def material_state(self, model=None, *, report_id=None):
        # Keep opened-page identity stable across order-index refreshes. This
        # projection rechecks paths; it does not modify the stored review model.
        with self.mutex:
            if model is not None:
                self.material_reports[model['report_id']] = model
                while len(self.material_reports) > 4:
                    self.material_reports.pop(next(iter(self.material_reports)))
            else:
                model = self.material_reports.get(report_id)
            if model is None:
                raise ArchiveError('该页面快照已过期，请保存草稿后重新打开页面')
        return project_discarded(model, self.staging)

    def _job_path(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ArchiveError('归档任务编号无效')
        return self.state / 'requests' / ('job-' + job_id + '.json')

    def get(self, job_id):
        return read_json(self._job_path(job_id))

    def _save(self, job):
        job['updated_at'] = now()
        save_json(self._job_path(job['job_id']), job)

    def list_jobs(self, *, summary=False):
        rows = self.job_catalog.snapshot()[:20]
        return rows if summary else [self.get(row['job_id']) for row in rows]

    def _proxy_authorization(self, model, decisions, compiled=None):
        """Validate opt-in proxy parents and return the exact link projection."""
        options = options_for(decisions)
        approved = tuple(options.get('proxy_only_unit_ids', ()))
        units = {u['unit_id']: u for u in model['units']}
        if not approved:
            return set(), companion_links(model)[0]
        if compiled is None:
            compiled = compile_plan(model, {k:v for k,v in decisions.items() if k != 'archive_options'})
        confirmed = {uid for assignment in compiled['assignments'] for uid in assignment['unit_ids']}
        for uid in approved:
            unit = units.get(uid)
            if unit is None:
                raise ArchiveError('显式代理授权包含未知素材单元')
            if uid not in confirmed:
                raise ArchiveError('显式代理授权只能选择已确认归属的素材单元')
            if standard_dji_proxy_file(unit) is None:
                raise ArchiveError('显式代理授权只能选择标准单一 DJI LRF proxy_only 单元')
        # A proxy parent is usable only when no same-manifest MP4 or second
        # approved proxy can claim the same clip.
        for uid in approved:
            proxy = units[uid]
            key = _clip(standard_dji_proxy_file(proxy), True)
            manifests = {ref['manifest_id'] for ref in proxy.get('provenance', ())}
            for other in units.values():
                if other['unit_id'] == uid:
                    continue
                other_keys = {_clip(f) for f in other.get('files', ())} - {None}
                other_manifests = {ref['manifest_id'] for ref in other.get('provenance', ())}
                if (key in other_keys and manifests & other_manifests and
                        other.get('kind') == 'video'):
                    raise ArchiveError('同清单同片段已有 MP4，不能把 LRF 代理作为唯一主文件')
                if (other['unit_id'] in approved and key == _clip(standard_dji_proxy_file(other), True)
                        and manifests & other_manifests):
                    raise ArchiveError('同清单同片段存在多个已批准 LRF 主代理')
        return set(approved), companion_links(model, approved_proxy_ids=approved)[0]

    def _compile(self, model, decisions):
        if not isinstance(decisions, dict):
            raise ArchiveError('归档计划格式无效')
        options = options_for(decisions)
        if options['mode']=='move' and not self.move_enabled:
            raise ArchiveError('服务尚未启用移动权限，请先启用后再选择移动')
        compiled = compile_plan(model, {k:v for k,v in decisions.items() if k!='archive_options'})
        if not compiled['assignments']:
            raise ArchiveError('请至少确认一个拍摄段，再提交归档')
        units = {u['unit_id']:u for u in model['units']}
        authorized_proxy_ids, links = self._proxy_authorization(model, decisions, compiled)
        assignments_by_unit = {uid:a for a in compiled['assignments'] for uid in a['unit_ids']}
        archived = None
        selected, projects, targets = [], {}, set()
        for assignment in compiled['assignments']:
            with diagnostic_context(segment_id=assignment.get('segment_id'), project_id=assignment['project']['project_id']):
                project = copy_project(assignment['project'])
                parts = relative(project['path'])
                if len(parts) != 3 or not re.fullmatch(r'20\d{2}', parts[0]) or not re.fullmatch(r'(?:0?[1-9]|1[0-2])月', parts[1]):
                    raise ArchiveError('项目必须位于已核定的年份、月份目录下')
                projects[project['path']] = project
                candidates = None
                for uid in assignment['unit_ids']:
                    with diagnostic_context(unit_id=uid):
                        raw_unit = units[uid]
                        unit = {k:v for k,v in raw_unit.items() if k != '_proxy_archive_authorized'}
                        parent_id = links.get(uid)
                        if parent_id:
                            parent = {k:v for k,v in units[parent_id].items() if k != '_proxy_archive_authorized'}
                            if parent_id in authorized_proxy_ids:
                                parent['_proxy_archive_authorized'] = True
                            parent_assignment = assignments_by_unit.get(parent_id)
                            prior = None
                            if parent_assignment:
                                if parent_assignment['project']['path'] != project['path']:
                                    raise ArchiveError('附属文件与主视频必须归属同一项目')
                            else:
                                if parent_id in authorized_proxy_ids:
                                    raise ArchiveError('已批准 LRF 代理与附属文件必须在本次确认计划中共同选择')
                                if archived is None:
                                    needed_parents = {links[u] for u in assignments_by_unit if u in links and links[u] not in assignments_by_unit}
                                    archived = {r['unit_id']:r for r in self.history.for_units(model, needed_parents)['archived_units'] if r['in_current_model']}
                                prior = archived.get(parent_id)
                                if prior is None:
                                    raise ArchiveError('请先确认主视频归属，或核对其归档记录后补齐附属文件')
                            rows = companion_rows(unit, parent, project, archived=prior, label=self.label)
                            unit = {**unit, '_companion_parent':parent}
                        else:
                            if uid in authorized_proxy_ids:
                                unit['_proxy_archive_authorized'] = True
                            if (unit['kind'] not in ('photo','video','audio','proxy_only') or
                                    (unit['kind'] == 'proxy_only' and uid not in authorized_proxy_ids) or
                                    any(PurePosixPath(f['name']).name.startswith('._') for f in unit['files']) or
                                    all(support_reason(f) for f in unit['files'])):
                                raise ArchiveError('设备附属文件无需选择项目；关系未确认的素材请在异常区域核对')
                            rows = studio_target_rows(unit,project)
                        if not parent_id and project.get('evidence_source') != 'manual_no_order':
                            # Candidate membership is shared by the reviewed segment.
                            if candidates is None:
                                candidates = {pid for u in assignment['unit_ids']
                                              for pid in units[u].get('candidate_project_ids', [])}
                            if assignment['project']['project_id'] not in candidates:
                                raise ArchiveError('所选项目不属于该拍摄段候选，请重新选择')
                        for row in rows:
                            key = fold(row['target_path'])
                            if key in targets and options['existing']=='error':
                                raise ArchiveError('同一项目存在同名素材，需核对后再归档：'+row['target_name'])
                            targets.add(key)
                        selected.append({'unit':unit,'project':project,'rows':rows})
        selected.sort(key=lambda item: bool(item['unit'].get('_companion_parent')))
        key = sorted([{'unit_id':r['unit']['unit_id'],'path':r['project']['path'],'files':r['rows']} for r in selected], key=lambda r:r['unit_id'])
        job_id = digest({'storage':'independent_copy','targets':key,'roots':self.identity})
        return compiled, selected, projects, job_id

    def _planned_id(self, selected, decisions):
        key = sorted([{'unit_id':r['unit']['unit_id'],'path':r['project']['path'],'files':r['rows']} for r in selected], key=lambda r:r['unit_id'])
        policy = options_for(decisions)
        value = {'storage':'independent_copy','targets':key,'roots':self.identity}
        if policy != {'mode':'copy','existing':'error'}:
            value['archive_options'] = policy
        return digest(value)

    def _verify(self, unit, cache, validated_signatures=None, runtime_provider=None):
        return self.source_preparation.verify(unit, cache, validated_signatures,
                                              runtime_provider=runtime_provider)

    def _prepare(self, model, decisions, *, accepted=None, progress=None):
        if progress:
            progress(phase='核对项目和素材范围')
        self._check_roots()
        compiled, selected, projects, job_id = self._compile(model, decisions)
        compilation_signature = self._selection_signature(selected)
        options = options_for(decisions)
        if accepted is None and options['mode'] == 'move':
            # Fail before any large target reads, not after accepting a job.
            # Prior copied media must not become a source-removal operation
            # through a generic repeat submission.
            with directory(self.state/'units') as existing_units:
                for item in selected:
                    with diagnostic_context(unit_id=item['unit']['unit_id'], project_id=item['project']['project_id']):
                        identity={'unit_id':item['unit']['unit_id'],'project_id':item['project']['project_id'],
                                  'project_path':item['project']['path'],'files':item['rows']}
                        if record(existing_units, 'job-'+digest(identity)+'.json') is not None:
                            raise ArchiveError('所选素材已有归档执行记录；复制后保留的来源不能通过重复提交移动清理。请先核对已有结果，本次尚未读取目标内容或提交任务。')
        self.target_digests.reserve(sum(len(i['rows']) for i in selected) * 2)
        if progress:
            progress(total_files=sum(len(i['rows']) for i in selected), checked_files=0)
        from .archive_target_evidence import SelectedTargetEvidence
        target_evidence = SelectedTargetEvidence(self, selected, self.target_digests)
        with directory(self.projects) as target:
            counts = plan_rows(target,selected,options,record(self.state_fd,'reservations.json',{}),
                               (accepted or {}).get('file_plans'), cache=target_evidence, progress=progress)
        job_id = self._planned_id(selected,decisions)
        from .archive_rename_io import eligible, STRATEGY
        # Accepted old jobs retain their original execution policy.
        direct = (accepted is None or accepted.get('execution_strategy') == STRATEGY) and eligible(self, selected, options, cache=target_evidence)
        # New production MOVE requests must not silently become copy+cleanup.
        # Accepted legacy requests retain their immutable execution strategy.
        if (accepted is None and options['mode'] == 'move' and not direct
                and (self.production or self.direct_move_view)):
            raise ArchiveError('本次无法同卷直接移动（入口、挂载或目标条件不满足）；未自动改成复制，请核对提示后另行选择复制方式')
        cache, evidence, folders, project_rows = {}, {}, {}, []
        validated_signatures = {}
        runtime_window = [0.0, None]
        def selected_runtime():
            current = time.monotonic()
            if runtime_window[1] is None or current - runtime_window[0] >= 1:
                runtime_window[:] = [current, self.runtime()]
            return runtime_window[1]
        required = 0
        prior = accepted or (self.get(job_id) if self._job_path(job_id).exists() else None)
        prior_folders = (prior or {}).get('folders', {})
        current_orders = {}
        for project in project_index(self.projects):
            if project.get('order_id'):
                current_orders.setdefault(project['order_id'], []).append(project['path'])
        with directory(self.projects) as fd, directory(self.state/'units') as units_fd:
            reservations = record(self.state_fd, 'reservations.json', {})
            for path, project in projects.items():
                with diagnostic_context(project_id=project['project_id'], target_path=path):
                    if progress:
                        progress(phase='核对项目目录', current_file='')
                    found = _inspect(fd, path)
                    captured = (accepted or {}).get('target_identities', {}).get(path)
                    if captured is not None and found != captured:
                        raise ArchiveError('已确认的项目目录身份已变化：' + path)
                    if found is None and project.get('order_id') and any(p != path for p in current_orders.get(project['order_id'], [])):
                        raise ArchiveError('同一订单出现其他目录，停止重复创建：' + path)
                    if found is None:
                        if path in prior_folders:
                            raise ArchiveError('本任务已创建的项目目录消失：' + path)
                        if project.get('folder_action') not in ('create_manual_after_confirmed_assignment','create_after_confirmed_assignment'):
                            raise ArchiveError('原有项目目录已消失，请重新核对：' + path)
                        if project.get('folder_action') == 'create_after_confirmed_assignment':
                            live = self.model()
                            current = next((p for p in live['projects'] if p['path']==path),None)
                            if not current or current.get('order_id') != project.get('order_id') or not current.get('order_evidence'):
                                raise ArchiveError('缺少当前有效订单信息，暂不能创建订单目录')
                        action = 'create'
                    else:
                        is_new = project.get('folder_action') in ('create_manual_after_confirmed_assignment','create_after_confirmed_assignment')
                        if is_new and prior_folders.get(path) != found:
                            raise ArchiveError('待建目录已被占用，请刷新候选后核对：' + path)
                        action = 'existing'
                    folders[path] = found
                    missing = [name for name in STANDARD_FOLDERS if _inspect(fd,path+'/'+name) is None]
                    subset = [r for r in selected if r['project']['path']==path]
                    destinations = {}
                    for item in subset:
                        with diagnostic_context(unit_id=item['unit']['unit_id'], project_id=item['project']['project_id']):
                            for file in item['rows']:
                                category = str(PurePosixPath(file['target_path']).parent.relative_to(path))
                                destinations[category] = destinations.get(category,0)+1
                    project_rows.append({'name':project['name'],'path':self.label.rstrip('/')+'/'+path,'relative_path':path,
                                         'action':action,'units':len(subset),'files':sum(len(r['rows']) for r in subset),
                                         'bytes':sum(f['size_bytes'] for r in subset for f in r['rows']),
                                         'create_subfolders':missing,'destinations':destinations})
            verified_files = 0
            directory_listings = {}
            parent_identities = {}
            for item in selected:
                with diagnostic_context(unit_id=item['unit']['unit_id'], project_id=item['project']['project_id']):
                    unit = item['unit']
                    if progress:
                        progress(phase='第2/2阶段：核对所选来源与目标身份', checked_files=verified_files,
                                 current_file=PurePosixPath(item['rows'][0]['name']).name)
                    evidence[unit['unit_id']] = self._verify(unit, cache, validated_signatures, runtime_provider=selected_runtime)
                    identity = {'unit_id':unit['unit_id'],'project_id':item['project']['project_id'],
                                'project_path':item['project']['path'],'files':item['rows']}
                    old = record(units_fd, 'job-'+digest(identity)+'.json')
                    for row in item['rows']:
                        with diagnostic_context(source_path=row['source_path'], name=row['name'], target_path=row['target_path']):
                            remaining = row['size_bytes']
                            reservation = reservations.get(row['source_path'])
                            expected = {'target_path':row['target_path'],'blake3':row['blake3']}
                            if reservation is not None and reservation != expected:
                                raise ArchiveError('素材已经提交过其他归档归属，请先核对已有结果')
                            parent = str(PurePosixPath(row['target_path']).parent)
                            if parent not in parent_identities:
                                parent_identities[parent] = _inspect(fd, parent)
                            if parent_identities[parent] is not None:
                                with subdirectory(fd,parent) as target_fd:
                                    if parent not in directory_listings:
                                        info = os.fstat(target_fd)
                                        listing = {}
                                        for name in os.listdir(target_fd):
                                            listing.setdefault(fold(name), []).append(name)
                                        directory_listings[parent] = (listing, (info.st_dev,info.st_ino,info.st_mtime_ns,info.st_ctime_ns))
                                    listing = directory_listings[parent][0]
                                    for name in (row['target_name'], row['temp']):
                                        # Check aliases without treating regular files as directories.
                                        aliases = [n for n in listing.get(fold(name), ()) if n != name]
                                        if aliases:
                                            raise ArchiveError('目标文件名称存在冲突：'+row['target_name'])
                                        try:
                                            info = os.stat(name,dir_fd=target_fd,follow_symlinks=False)
                                        except FileNotFoundError:
                                            continue
                                        if name==row['target_name'] and options['existing']=='skip_identical':
                                            if not same_content(target_fd,name,row,cache=self.target_digests,
                                                    progress=(lambda count: progress(bytes_delta=count)) if progress else None):
                                                raise ArchiveError('预定目标内容发生变化，请重新检查：'+name)
                                            remaining = 0
                                            continue
                                        if old is None or old.get('identity') != identity:
                                            raise ArchiveError('目标存在未登记文件，禁止覆盖：'+row['target_name'])
                                        if not stat.S_ISREG(info.st_mode):
                                            raise ArchiveError('归档目标或临时文件类型发生变化')
                                        if info.st_size > row['size_bytes']:
                                            raise ArchiveError('已登记目标大小超出原素材')
                                        remaining = min(remaining, row['size_bytes'] - info.st_size)
                            required += remaining
                            verified_files += 1
                            if progress:
                                progress(checked_files=verified_files)
            total = sum(f['size_bytes'] for r in selected for f in r['rows'])
            for parent, expected in parent_identities.items():
                if _inspect(fd, parent) != expected:
                    raise ArchiveError('核对期间目标路径发生变化，请重新检查')
            # This listing is local to one preparation, never a durable proof.
            # Fail if another process changed any inspected directory meanwhile.
            for parent, (_, expected) in directory_listings.items():
                with subdirectory(fd,parent) as target_fd:
                    info = os.fstat(target_fd)
                    if (info.st_dev,info.st_ino,info.st_mtime_ns,info.st_ctime_ns) != expected:
                        raise ArchiveError('核对期间目标目录发生变化，请重新检查')
            free = os.fstatvfs(fd)
            if not direct and free.f_bavail * free.f_frsize < required:
                raise ArchiveError('项目磁盘可用空间不足，暂不能提交')
        # Recheck selected batches and explicit referenced batches once, fresh,
        # after all file work. A periodic snapshot is never final authority.
        from .intake import runtime_check
        final_runtime = self.runtime()
        for key, manifest in cache.items():
            if isinstance(key, tuple) and key and key[0] == 'record-base':
                runtime_check(final_runtime, manifest)
        direct_count = counts['copy_files'] if direct else 0
        if direct:
            counts['copy_files'] = 0
        summary={'segments':len(compiled['assignments']),'units':len(selected),'files':sum(len(r['rows']) for r in selected),
                 'bytes':total,**counts,'direct_move_files':direct_count,'pending_units':compiled['summary']['pending_units'],'deferred_units':compiled['summary']['deferred_units']}
        return {'compiled':compiled,'selected':selected,'projects':project_rows,'target_identities':folders,
                'compilation_signature':compilation_signature,
                'evidence':evidence,'validated_signatures':validated_signatures,'summary':summary,'job_id':job_id,
                'target_parent_identities':parent_identities,
                'file_plans':{r['source_path']:r for i in selected for r in i['rows']},'archive_options':options,
                'execution_strategy':STRATEGY if direct else 'copy_then_cleanup'}

    def preflight(self, decisions, *, progress=None):
        # Old pages using the synchronous route are serialized too. Async callers
        # can abandon obsolete work while waiting without adding more hash readers.
        while not self.preflight_lock.acquire(timeout=0.1):
            if progress:
                progress(phase='等待前一次检查结束')
        try:
            return self._preflight(decisions, progress=progress)
        finally:
            self.preflight_lock.release()

    def _preflight(self, decisions, *, progress=None):
        model = None
        try:
            if progress:
                progress(phase='读取当前素材清单')
            if not self.enabled:
                raise ArchiveError('服务尚未启用归档提交')
            if self.thread is not None and not self.thread.is_alive():
                raise ArchiveError('归档后台已停止，请先检查服务状态')
            model = self.model()
            prepared = self._prepare(model, decisions, progress=progress)
            if progress:
                progress(phase='整理检查结果', current_file='')
            from .archive_request_scope import scoped_request_inputs
            scoped_model, scoped_decisions, scope_binding = scoped_request_inputs(model, decisions, prepared)
            _, scoped_selected, _, _ = self._compile(scoped_model, scoped_decisions)
            if self._selection_signature(scoped_selected) != prepared['compilation_signature']:
                raise ArchiveError('本次归档范围收集结果不一致，未创建任务')
            seal = digest({'decisions':decisions,'report_id':model['report_id'],'roots':self.identity,
                           'targets':prepared['target_identities'],'parents':prepared['target_parent_identities'],
                           'files':prepared['file_plans'],'evidence':prepared['evidence']})
            with self.mutex:
                # Persist only the selected executable plan. Memory eviction or
                # restart must not invalidate a still-current confirmation.
                prepared = {k:prepared[k] for k in (
                    'projects','target_identities','target_parent_identities','compilation_signature',
                    'evidence','validated_signatures','summary','job_id','file_plans',
                    'archive_options','execution_strategy')}
                self.previews[seal] = {'model':scoped_model,'decisions':deepcopy(decisions),
                                      'scoped_decisions':scoped_decisions,'scope_binding':scope_binding,
                                      'origin_report_id':model['report_id'],
                                      'prepared':prepared,'expires_at':time.time()+1200}
            return self.confirmation(seal)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {'status':'blocked','errors':[str(exc)],'projects':[],
                    'issues':diagnostic_issues(exc, model, decisions)}

    def confirmation(self, preview_id):
        """Restore one unexpired confirmation without reopening a review/model."""
        with self.mutex:
            ticket = self.previews.get(preview_id)
            if not self.enabled or not ticket or ticket['expires_at'] < time.time():
                raise ArchiveError('本次确认记录不存在或已过期，请手动重新检查')
            plan = ticket['prepared']
            return deepcopy({'status':'ready','preview_id':preview_id,'expires_at':ticket['expires_at'],
                             'summary':plan['summary'],'projects':plan['projects'],
                             'archive_options':plan['archive_options'],
                             'execution_strategy':plan['execution_strategy'],
                             'file_actions':[{'source_name':PurePosixPath(r['name']).name,'target_path':r['target_path']}
                                             for r in plan['file_plans'].values() if r['target_name']!=PurePosixPath(r['name']).name],
                             'errors':[]})

    def submit(self, preview_id, decisions, confirmed, *, progress=None):
        with self.mutex:
            if not self.enabled or confirmed is not True:
                raise ArchiveError('必须在页面核对本次范围并明确提交')
            ticket = self.previews.get(preview_id)
            if not ticket or ticket['expires_at'] < time.time() or ticket['decisions'] != decisions:
                raise ArchiveError('提交范围已变化或检查已过期，请重新检查')
            if progress:
                progress(phase='接收已确认计划')
            origin_report_id = ticket.get('origin_report_id', ticket['model']['report_id'])
            scoped_decisions = ticket.get('scoped_decisions', decisions)
            # Accept the exact reviewed plan; no model reload, media read,
            # directory walk, or second preflight in the submission path.
            # Execution still checks paths, batch locks and actual file identity.
            fresh = ticket['prepared']
            job_id = fresh['job_id']
            if self._job_path(job_id).exists():
                return self.get(job_id)
            request = {'model':ticket['model'],'decisions':deepcopy(scoped_decisions),'job_id':job_id,'roots':self.identity,'file_plans':fresh['file_plans']}
            request['admission_policy'] = 'confirmed-plan/v1'
            if 'scope_binding' in ticket:
                request['scope_binding'] = ticket['scope_binding']

            from .archive_rename_io import STRATEGY
            if fresh['execution_strategy'] == STRATEGY:
                request.update(execution_strategy=STRATEGY, source_evidence=fresh['evidence'],
                               target_parent_identities=fresh['target_parent_identities'],
                               verification_policy='postcopy-signature-or-full-read/v1',
                               validated_signatures=fresh['validated_signatures'])

            if progress:
                progress(phase='保存本次归档任务', current_file='')
            reservations = record(self.state_fd, 'reservations.json', {})
            for row in fresh['file_plans'].values():
                with diagnostic_context(source_path=row['source_path'], name=row.get('name'), target_path=row['target_path']):
                    prior = reservations.get(row['source_path'])
                    reservation = {'target_path':row['target_path'],'blake3':row['blake3']}
                    if prior is not None and prior != reservation:
                        raise ArchiveError('本次素材已被另一份归档计划占用，请保留原任务并核对')
                    reservations[row['source_path']] = reservation
            save_json(self.state/'requests'/('request-'+job_id+'.json'),request)
            atomic_json(self.state_fd, 'reservations.json', reservations)
            s=fresh['summary']
            job={'job_id':job_id,'status':'queued','phase':'等待归档','created_at':now(),'report_id':origin_report_id,
                 'example_data':not self.production,'confirmed_at':now(),
                 'storage':'verified_move' if fresh['archive_options']['mode']=='move' else 'independent_copy',
                 'archive_options':fresh['archive_options'],'file_plans':fresh['file_plans'],
                 'execution_strategy':fresh['execution_strategy'],
                 'request_digest':digest(request),'target_identities':fresh['target_identities'],'folders':{},
                 'summary':{'total_units':s['units'],'total_files':s['files'],'total_bytes':s['bytes'],
                            'completed_units':0,'completed_files':0,'processed_bytes':0,
                            'planned_direct_move_files':s.get('direct_move_files',0),'direct_moved_files':0,
                            'skipped_files':0,'copy_files':0,'renamed_files':s['renamed_files'],'removed_sources':0},
                 'projects':fresh['projects'],'outcomes':[],'errors':[]}
            self._save(job)
            self.wake.set()
            return job

    def retry(self, job_id, confirmed):
        with self.mutex:
            if not self.enabled or confirmed is not True:
                raise ArchiveError('请明确选择继续未完成任务')
            job=self.get(job_id)
            if job['status'] not in ('partial','failed'):
                return job
            job.update(status='queued',phase='等待继续归档',errors=[],issues=[])
            self._save(job)
            self.wake.set()
            return job

    def run_job(self, job_id):
        self.worker_busy.set()
        try:
            return self._run_job(job_id)
        finally:
            self.worker_busy.clear()

    def _run_job(self, job_id):
        job=self.get(job_id)
        if not self.enabled or job['status'] == 'completed':
            return job
        model, decisions = None, None
        try:
            request=read_json(self.state/'requests'/('request-'+job_id+'.json'))
            if digest(request) != job['request_digest'] or request['roots'] != self.identity:
                raise ArchiveError('已提交任务的范围或根目录记录发生变化')
            model,decisions=request['model'],request['decisions']
            from .archive_rename_io import STRATEGY
            if request.get('execution_strategy') == STRATEGY:
                from .archive_direct_move import run_direct_move
                return run_direct_move(self, job, request)
            if job.get('cleanup_started'):
                return finish_move(self,job,request)
            from .staging_coordination import staging_guard, batch_guard
            from .archive_move import request_batches
            if not self.production:
                with staging_guard(self.staging, create=True):
                    pass
            job['phase'] = '等待素材使用结束，准备复制归档'
            self._save(job)
            with batch_guard(self.staging, request_batches(request), wait=True, cancel=self.stop_event):
                self._run_copy_phase(job, request)
            # Release the read locks before reacquiring exclusive cleanup locks.
            # finish_move rechecks every bound source/target and completion record.
            if job.get('cleanup_started') and job['status'] == 'running':
                return finish_move(self,job,request)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            job.update(status='failed',phase='归档暂停，需要核对',errors=[str(exc)],
                       issues=diagnostic_issues(exc, model, decisions))
            if job.get('execution_strategy') == 'same_volume_rename/v1':
                job.update(direct_move_recovery_required=True,
                           phase='同卷直接移动已暂停；已移入文件保留，继续前将按记录回读')
            self._save(job)
        return job

    def _run_copy_phase(self, job, request):
        model, decisions, job_id = request['model'], request['decisions'], job['job_id']
        job.update(status='running', phase='核对本次归档范围与已选文件')
        self._save(job)
        prepared=self._prepare(model,decisions,accepted=job)
        if prepared['job_id'] != job_id:
            raise ArchiveError('归档任务身份不一致')
        job.update(status='running',phase='创建并核对项目目录',errors=[],issues=[])
        self._save(job)
        with directory(self.staging) as source, directory(self.projects) as projects, directory(self.state/'units') as units_fd:
            for row in prepared['projects']:
                path=row['relative_path']
                if row['action']=='create':
                    identity=_create(projects,path)
                    if _inspect(projects,path) != identity:
                        raise ArchiveError('新建目录回读未通过')
                    job['folders'][path]=identity
                    self._save(job)
                if _inspect(projects,path) != (job['target_identities'][path] or job['folders'].get(path)):
                    raise ArchiveError('项目目录与提交时身份不一致')
                for child in STANDARD_FOLDERS:
                    _inspect(projects,path+'/'+child)
                    with subdirectory(projects,path+'/'+child,create=True):
                        pass
            from .archive_ownership_cache import job_progress
            scope = {}
            for item in prepared['selected']:
                identity = {'unit_id':item['unit']['unit_id'],
                            'project_id':item['project']['project_id'],
                            'project_path':item['project']['path'],
                            'files':item['rows']}
                scope[item['unit']['unit_id']] = digest(identity)
            index=recover_index(units_fd, scope, progress=job_progress(self, job),
                                require_ready=self.production)
            cache={}
            outcomes=[]
            for item in prepared['selected']:
                if self.stop_event.is_set():
                    raise ArchiveError('归档服务正在停止，任务保留供下次继续')
                unit=item['unit']
                job['phase']='复制并校验素材'
                self._save(job)
                last_progress=[0.0]
                def checkpoint(phase,row):
                    if self.stop_event.is_set():
                        raise ArchiveError('归档服务正在停止，已保留复制进度')
                    self.checkpoint(phase,row)
                    if phase=='copy_chunk' and time.monotonic()-last_progress[0] > 0.5:
                        job['phase']='复制并校验：'+PurePosixPath(row['source_path']).name
                        job['current_file_bytes']=row.get('copied_bytes',0)
                        self._save(job)
                        last_progress[0]=time.monotonic()
                try:
                    parent = unit.get('_companion_parent')
                    if parent and any(i['unit']['unit_id'] == parent['unit_id'] for i in prepared['selected']):
                        if not any(o['unit_id'] == parent['unit_id'] and o['status'] == 'completed' for o in outcomes):
                            raise ArchiveError('主视频尚未成功归档，附属文件保留待处理')
                    result=_unit(None,source,projects,units_fd,model,decisions,unit,item['project'],index,self.runtime,
                                 checkpoint,4*1024*1024,source_verifier=lambda u:self._verify(u,cache),production=self.production,
                                 rows_factory=lambda u,p:item['rows'],
                                 skip_identical=prepared['archive_options']['existing']=='skip_identical')
                    result['bytes']=sum(f['size_bytes'] for f in unit['files'])
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    exc.archive_location = {'unit_id':unit['unit_id'], **getattr(exc, 'archive_location', {})}
                    result={'unit_id':unit['unit_id'],'status':'incomplete','files':len(unit['files']),'error':str(exc),
                            'issues':diagnostic_issues(exc, model, decisions)}
                outcomes.append(result)
                done=[o for o in outcomes if o['status']=='completed']
                job['outcomes']=outcomes
                job['summary'].update(completed_units=len(done),completed_files=sum(o['files'] for o in done),
                                      processed_bytes=sum(o['bytes'] for o in done),
                                      skipped_files=sum(o.get('skipped_files',0) for o in done),
                                      copy_files=sum(o['files']-o.get('skipped_files',0) for o in done))
                job['current_file_bytes']=0
                self._save(job)
            self._check_roots()
            errors=[o['error'] for o in outcomes if o['status']!='completed']
            job.update(status='partial' if errors else 'completed',phase='有素材需要核对' if errors else '归档完成，副本已校验',errors=errors)
            if not errors and options_for(decisions)['mode']=='move':
                # Durable barrier: every destination has a verified copy receipt.
                job.update(cleanup_started=True,status='running',phase='目标已校验，准备清理中转源文件')
                self._save(job)
                return job
            if not errors:
                job['completed_at']=now()
            self._save(job)
        return job

    def start(self):
        if self.thread is not None:
            return
        def work():
            while not self.stop_event.is_set():
                jobs=self.job_catalog.snapshot()
                for job in jobs:
                    if self.stop_event.is_set():
                        break
                    if self.enabled and job['status'] in ('queued','running'):
                        self.run_job(job['job_id'])
                self.wake.wait(2)
                self.wake.clear()
        self.thread=threading.Thread(target=work,name='archive-worker',daemon=True)
        self.thread.start()
