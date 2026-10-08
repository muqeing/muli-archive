"""Completion gates and review generation; source media are only stat'ed."""
from datetime import datetime, timezone
import os
from .intake import EvidenceError, _open, read_bytes, resolve_files, runtime_check, validate_record, media_stat
from .time_correction_receipt import ReceiptError, receipt_signature
from .matching import group_files
from .review import build_review_model, digest


def completed(row):
    return row['state']=='COMPLETED' and row['result']=='COPY_VERIFIED' and bool(row['completed_at']) and row['revision']>=1


def size_verified_candidate(staging, row, *, allow_examples=False):
    """Return private validated evidence for a size-only batch, else None."""
    if (not isinstance(row, dict) or row.get('state') != 'COMPLETED' or
            row.get('result') != 'COPY_SIZE_VERIFIED' or not row.get('completed_at') or
            type(row.get('revision')) is not int or row['revision'] < 1):
        return None
    try:
        manifest = validate_record(staging, row['batch_id'], allow_examples=allow_examples)
    except (EvidenceError, OSError, KeyError, TypeError, ValueError):
        return None
    return manifest.get('_postcopy_evidence')


def signal_parts(staging, batch_ids, snapshot, *, allow_examples=False):
    """Small receipt plus manifest attributes, not repeated full-manifest hashing.

    This invalidation hint never authorizes an archive; execution needs fresh hashes.
    """
    states = {s['batch_id']:s for s in snapshot['batches']}
    values = {}
    for bid in sorted(set(batch_ids)):
        row = states.get(bid)
        candidate = size_verified_candidate(staging, row, allow_examples=allow_examples) if row else None
        if row is None or (not completed(row) and candidate is None):
            raise EvidenceError('当前批次或被引用批次尚未最终完成')
        receipt = read_bytes(staging, bid+'/ingest_complete.json')
        if len(receipt)>65536:
            raise EvidenceError('完成回执超过大小限制')
        attrs = []
        for name in ('ingest_manifest.json','ingest_manifest.md'):
            fd = _open(staging, bid+'/'+name)
            try:
                s = os.fstat(fd)
                attrs.append([s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns])
            finally:
                os.close(fd)
        try:
            correction_receipt = receipt_signature(staging, bid)
        except ReceiptError as exc:
            raise EvidenceError(str(exc)) from exc
        # Optional by design: keep the exact legacy signal when absent.  Once
        # present, creation, replacement, or removal invalidates the signal.
        if correction_receipt is not None:
            attrs.append(['time_correction_receipt', correction_receipt])
        if candidate is not None:
            attrs.append(['postcopy_receipt', candidate['receipt_signature']])
        values[bid] = digest([row, digest(receipt.hex()), attrs])
    return values


def signal(staging, batch_ids, snapshot, projects, *, allow_examples=False):
    return digest([sorted(signal_parts(staging,batch_ids,snapshot,allow_examples=allow_examples).items()),projects])


def build_job(staging, batch_id, snapshot, projects, *, allow_examples=False):
    records, visiting, before = {}, set(), {}
    def load(bid):
        if bid in visiting:
            raise EvidenceError('跨批引用形成循环')
        if bid in records:
            return
        visiting.add(bid)
        before.update(signal_parts(staging,[bid],snapshot,allow_examples=allow_examples))
        m = validate_record(staging, bid, allow_examples=allow_examples)
        if allow_examples and not m['example_data']:
            raise EvidenceError('合成队列不能读取真实标识的批次')
        runtime_check(snapshot, m)
        for f in m['files']:
            if f['copy_status']=='skipped_existing':
                load(f['existing_copy']['batch_id'])
        records[bid] = m
        visiting.remove(bid)
    load(batch_id)
    m = records[batch_id]
    files = resolve_files(staging, m, records, check_media=False)
    dependencies=sorted(records)
    manifest_id, example_data=m['manifest_id'], m['example_data']
    # Resolved descriptors now own the fields needed for grouping. Full ingest
    # manifests and postcopy receipts need not remain live during model export.
    records.clear()
    del m
    missing = set()
    for file in files:
        try:
            media_stat(staging,file['resolved_path'],file['size_bytes'])
        except FileNotFoundError:
            # A previous move must not hide other, still-present units in the
            # same batch. Missing units remain visible but cannot be submitted.
            missing.add(file['resolved_path'])
    groups = group_files(files, projects, [], manifest_id)
    files.clear()
    for group in groups:
        for unit in group['units']:
            absent = [f['source_path'] for f in unit['files'] if f['source_path'] in missing]
            if absent:
                unit['warnings'].append('中转文件已不在原路径，请查看归档记录或核对来源')
    report = {'mode':'read_only_preview', 'example_data':example_data,
              'generated_at':datetime.now(timezone.utc).isoformat(), 'projects':projects,
              'batches':[{'batch_id':batch_id,'manifest_id':manifest_id,'status':'verified','groups':groups}]}
    if before != signal_parts(staging,dependencies,snapshot,allow_examples=allow_examples):
        raise EvidenceError('读取清单期间完成证据发生变化')
    return build_review_model(report, _consume_input=True), dependencies, digest([sorted(before.items()),projects])
