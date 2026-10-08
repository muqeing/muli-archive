"""Revalidate completion evidence with separate synthetic and production gates."""
import os
from .archive_io import ArchiveError, signature
from .intake import _open, relative, resolve_files, runtime_check, validate_record, media_stat
from .postcopy_receipt import PostcopyError, _candidate_signature as candidate_signature
from .time_correction_receipt import ReceiptError, amendment_evidence, receipt_signature
from .matching import group_files
from .review import digest


def _attributes_touched_only(actual, expected):
    """True when only the attribute-change time drifted.

    NAS indexers, album services and permission fixes rewrite inode metadata
    without touching the bytes. The receipt still names the same inode, size and
    mtime, so the content can be proved with one read instead of asking for a new
    independent verification.
    """
    if set(actual) != set(expected) or actual.get('ctime_ns') == expected.get('ctime_ns'):
        return False
    return all(actual[key] == expected[key] for key in actual if key != 'ctime_ns')


def verify_sources(staging, unit, runtime_provider, *, production=False, reviewed_metadata=False, cache=None, check_media=True, companion_parent=None, validated_signatures=None):
    records = {}
    visiting = set()

    def load(batch_id):
        if batch_id in visiting:
            raise ArchiveError('跨批引用形成循环')
        if batch_id in records:
            return records[batch_id]
        visiting.add(batch_id)
        key = None
        base_key = None
        cached = None
        if cache is not None:
            evidence_stats = []
            for name in ('ingest_complete.json', 'ingest_manifest.json', 'ingest_manifest.md'):
                fd = _open(staging, batch_id + '/' + name)
                try:
                    evidence_stats.append(tuple(signature(fd)))
                finally:
                    os.close(fd)
            try:
                correction_stats = receipt_signature(staging, batch_id)
            except ReceiptError as exc:
                raise ArchiveError(str(exc)) from exc
            base_key = (batch_id, tuple(evidence_stats), correction_stats, production)
            cached = cache.get(('record-base', base_key))
        if cached is not None and cached['batch'].get('result') == 'COPY_VERIFIED':
            m = cached
            key = base_key
        elif cached is not None and cached['batch'].get('result') == 'COPY_SIZE_VERIFIED':
            try:
                postcopy_stats = candidate_signature(staging, batch_id, manifest=cached, cache=cache)
            except PostcopyError as exc:
                raise ArchiveError(str(exc)) from exc
            if postcopy_stats is not None:
                postcopy_stats = (tuple(postcopy_stats[0]), tuple(postcopy_stats[1]), postcopy_stats[2])
            key = (base_key, postcopy_stats)
            m = cache.get(('record', key))
            if m is None:
                m = validate_record(staging, batch_id, allow_examples=not production)
        else:
            m = validate_record(staging, batch_id, allow_examples=not production)
            if cache is not None and m['batch'].get('result') == 'COPY_SIZE_VERIFIED':
                try:
                    postcopy_stats = candidate_signature(staging, batch_id, manifest=m, cache=cache)
                except PostcopyError as exc:
                    raise ArchiveError(str(exc)) from exc
                if postcopy_stats is not None:
                    postcopy_stats = (tuple(postcopy_stats[0]), tuple(postcopy_stats[1]), postcopy_stats[2])
                key = (base_key, postcopy_stats)
            elif cache is not None:
                key = base_key
        if not production and m.get('example_data') is not True:
            raise ArchiveError('演示执行器拒绝真实批次')
        if production and m.get('example_data') is not False:
            raise ArchiveError('生产执行器拒绝合成批次')
        if cache is not None and key is not None:
            cache[('record-base', base_key)] = m
            cache[('record', key)] = m
            if ('digest', id(m)) not in cache:
                cache[('digest', id(m))] = digest(m)
        runtime_check(runtime_provider(), m)
        dependency_key = ('source-dependencies', id(m))
        dependencies = cache.get(dependency_key) if cache is not None else None
        if dependencies is None:
            dependencies = tuple(dict.fromkeys(f['existing_copy']['batch_id'] for f in m['files']
                                              if f['copy_status'] == 'skipped_existing'))
            if cache is not None:
                cache[dependency_key] = dependencies
        for dependency in dependencies:
            load(dependency)
        records[batch_id] = m
        visiting.remove(batch_id)
        return m

    if not unit.get('files') or not unit.get('provenance'):
        raise ArchiveError('素材单元缺少文件或清单来源')
    if unit['unit_id'] != 'unit-' + digest(sorted(unit['files'], key=lambda f: f['source_path']))[:24]:
        raise ArchiveError('素材单元标识与文件内容证据不符')
    for provenance in unit['provenance']:
        m = load(provenance['batch_id'])
        if m['manifest_id'] != provenance['manifest_id']:
            raise ArchiveError('确认模型引用了其他版本清单')
    known = {}
    source_records = {}
    evidence = {}
    for bid, m in records.items():
        row = {'manifest_id': m['manifest_id'], 'content_digest':
               cache[('digest', id(m))] if cache is not None else digest(m)}
        postcopy = m.get('_postcopy_evidence')
        if postcopy is not None:
            row['postcopy'] = {key: postcopy[key] for key in (
                'schema', 'status', 'verified_at', 'batch_id', 'batch_uid', 'revision',
                'manifest_id', 'source_id', 'original_manifest_blake3',
                'original_ingest_complete_blake3', 'receipt_signature', 'receipt_blake3',
                'receipt_root')}
        amendment = amendment_evidence(m)
        if amendment is not None:
            # Keep the receipt-bound amendment visible in archive evidence;
            # this prevents an old confirmation from silently describing the
            # original clock after a media file has been corrected.
            row['amendment'] = amendment
        evidence[bid] = row
    for provenance in unit['provenance']:
        resolved_key = ('resolved', provenance['batch_id'], digest(evidence))
        resolved = cache.get(resolved_key) if cache is not None else None
        if resolved is None:
            # Validate media belonging to this unit below. Other units from the
            # same batch may already have been moved by a completed archive job.
            resolved = resolve_files(staging, records[provenance['batch_id']], records, check_media=False)
            if cache is not None:
                cache[resolved_key] = resolved
        index_key = ('resolved-index', resolved_key)
        index = cache.get(index_key) if cache is not None else None
        if index is None:
            index = {f['resolved_path']: f for f in resolved}
            if cache is not None:
                cache[index_key] = index
        relevant = [index[d['source_path']] for d in unit['files'] if d['source_path'] in index]
        if not relevant:
            raise ArchiveError('清单来源与本单元没有对应文件')
        for f in relevant:
            known[f['resolved_path']] = f
            source_records[f['resolved_path']] = records[provenance['batch_id']]
    for item in unit['files']:
        relative(item['source_path'])
        relative(item['name'])
        if type(item['size_bytes']) is not int or item['size_bytes'] <= 0:
            raise ArchiveError('空文件或无效长度不能归档')
        f = known.get(item['source_path'])
        if f is None or (f['size_bytes'], f['hash']['source'], f['relative_path']) != (item['size_bytes'], item['blake3'], item['name']):
            raise ArchiveError('确认模型文件与原始成功清单不符')
        if check_media:
            media_stat(staging, item['source_path'], item['size_bytes'])
            postcopy = source_records[item['source_path']].get('_postcopy_evidence')
            if postcopy is not None:
                receipt_file = postcopy['files'].get(item['source_path'])
                if receipt_file is None:
                    raise ArchiveError('独立校验回执遗漏所选来源文件')
                fd = _open(staging, item['source_path'])
                try:
                    from .postcopy_receipt import source_signature
                    receipt_actual = source_signature(fd, postcopy['schema'])
                    current = os.fstat(fd)
                    actual = {'dev': current.st_dev, 'ino': current.st_ino,
                              'size': current.st_size, 'mtime_ns': current.st_mtime_ns,
                              'ctime_ns': current.st_ctime_ns}
                    if (source_signature(fd, postcopy['schema']) != receipt_actual or
                            any(actual[k] != receipt_actual[k] for k in ('ino', 'size', 'mtime_ns', 'ctime_ns'))):
                        raise ArchiveError('核对期间来源文件身份发生变化')
                    if receipt_actual != receipt_file['source_signature']:
                        expected = receipt_file['source_signature']
                        legacy_remount = postcopy['schema'] == 'postcopy-verification/1' and (
                            {k:v for k,v in receipt_actual.items() if k != 'dev'} ==
                            {k:v for k,v in expected.items() if k != 'dev'})
                        if legacy_remount:
                            raise ArchiveError('NAS 重启后旧校验回执的磁盘编号改变；需完成一次独立续验，请勿重复提交归档')
                        if not _attributes_touched_only(receipt_actual, expected):
                            raise ArchiveError('所选来源文件与独立校验回执签名不一致，请核对文件是否变化')
                        # Attributes were rewritten without touching the bytes.
                        # Prove the content here instead of demanding a new
                        # independent verification for an attribute-only edit.
                        from .archive_io import hash_fd
                        if hash_fd(fd) != item['blake3']:
                            raise ArchiveError('来源文件属性变化且内容摘要与回执签名不一致，请重新核对来源')
                finally:
                    os.close(fd)
                if validated_signatures is not None:
                    validated_signatures[item['source_path']] = dict(actual)
    fresh_groups = group_files(list(known.values()), [], [], 'archive-validation')
    fresh_units = [u for group in fresh_groups for u in group['units']]
    if any(any(u.get(field) != unit.get(field) for field in ('capture_time','capture_date','timezone_trusted','device')) for u in fresh_units):
        raise ArchiveError('确认后拍摄时间、时区或设备证据改变，需要重新确认')
    proxy_authorized = unit.get('_proxy_archive_authorized') is True
    if proxy_authorized and unit.get('kind') != 'proxy_only':
        raise ArchiveError('代理授权标记只能用于 proxy_only 单元')
    if proxy_authorized:
        from .material_triage import standard_dji_proxy_file
        if standard_dji_proxy_file(unit) is None:
            raise ArchiveError('代理授权标记只能用于标准单一 DJI LRF 单元')
    allowed = ('photo', 'video', 'audio') + (('proxy_only',) if proxy_authorized else ())
    if companion_parent is not None:
        from .material_triage import companion_links, check_companion_metadata
        parent_proxy = companion_parent.get('_proxy_archive_authorized') is True
        if parent_proxy:
            if (unit.get('kind') != 'auxiliary' or any(
                    os.path.splitext(f['name'])[1].lower() not in ('.thm', '.scr')
                    for f in unit.get('files', ()) )):
                raise ArchiveError('已批准 LRF 代理只能携带同片段 THM/SCR 附属文件')
            if companion_parent.get('kind') != 'proxy_only':
                raise ArchiveError('代理授权标记只能用于 proxy_only 主单元')
            from .material_triage import standard_dji_proxy_file
            if standard_dji_proxy_file(companion_parent) is None:
                raise ArchiveError('代理授权标记只能用于标准单一 DJI LRF 主单元')
        links, _ = companion_links({'units': [unit, companion_parent]},
                                   approved_proxy_ids=((companion_parent['unit_id'],)
                                                       if parent_proxy else ()))
        if links.get(unit['unit_id']) != companion_parent['unit_id']:
            raise ArchiveError('附属文件与主视频的来源或片段编号不一致')
        if check_media:
            check_companion_metadata(staging, unit, companion_parent)
        parent_evidence = verify_sources(staging, companion_parent, runtime_provider,
                                        production=production, reviewed_metadata=True,
                                        cache=cache, check_media=False)
        if any(bid in evidence and evidence[bid] != value for bid, value in parent_evidence.items()):
            raise ArchiveError('主视频与伴随文件的清单证据冲突')
        evidence.update(parent_evidence)
        allowed = ('auxiliary', 'proxy_only')
    if any(u['kind'] not in allowed or u['kind'] != unit['kind'] or
           (not reviewed_metadata and (not u['timezone_trusted'] or u['warnings'])) for u in fresh_units):
        raise ArchiveError('素材类型、时区或伴随关系问题尚未解决')
    # These digests describe the exact validated records, not an authorization token.
    return evidence
