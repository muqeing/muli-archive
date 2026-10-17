"""Permanent historical completion records, never current cleanup permission.

Both services vendor these bytes. Only an explicitly enabled producer creates
facts after its original full binding checks. An old lease is not a fact.
"""
from . import handoff_protocol as wire

SCHEMA = 'muli-archive-completion/1'
ACK = 'muli-archive-completion-ack/1'


def seal(event):
    payload = {k: v for k, v in event.items() if k != 'fact_id'}
    return {**payload, 'fact_id': wire.digest(payload)}


def create(verified, *, published_at):
    """Accept only a freshly built verified event, never a stored expired lease."""
    wire.validate_event(verified, published_at)
    if verified['status'] != 'verified':
        raise ValueError('completion_requires_full_job_evidence')
    return seal({'schema': SCHEMA, 'published_at': wire.iso(published_at),
                 'historical_only': True, 'operation_authorized': False,
                 'evidence': verified})


def validate(fact):
    if (not isinstance(fact, dict) or fact.get('schema') != SCHEMA
            or fact != seal(fact) or fact.get('historical_only') is not True
            or fact.get('operation_authorized') is not False
            or set(fact) != {'schema', 'published_at', 'historical_only',
                             'operation_authorized', 'evidence', 'fact_id'}):
        raise ValueError('invalid_completion_fact')
    event = fact['evidence']
    # This validates the original historical proof, not today's file health.
    issued = wire.timestamp(fact['published_at'])
    wire.validate_event(event, issued)
    if event['status'] != 'verified':
        raise ValueError('invalid_completion_status')
    return event


def projection(batch, records, binding, expected, *, checked_at, ignored=()):
    """Exact original coverage across accepted facts; no expiring leases mixed in."""
    covered, jobs = {}, set()
    for record in records:
        if record['binding'] != binding or record.get('schema') != SCHEMA:
            raise ValueError('completion_binding_conflict')
        if not wire.HEX.fullmatch(record.get('fact_id', '')):
            raise ValueError('invalid_stored_completion_fact')
        jobs.add(record['job_id'])
        for row in record['files']:
            original = expected.get(row['file_id'])
            if original is None or any(row.get(k) != v for k, v in original.items()):
                raise ValueError('completion_file_conflict')
            old = covered.get(row['file_id'])
            if old and any(old[k] != row[k] for k in
                           ('target_path', 'archived_blake3', 'target_signature')):
                raise ValueError('completion_target_conflict')
            covered[row['file_id']] = row
    total, count = len(expected), len(covered)
    if count > total:
        raise ValueError('completion_coverage_overflow')
    modes = {r['mode'] for r in covered.values()}
    return {'schema': SCHEMA, 'batch_uid': batch['batch_uid'],
            'batch_id': batch['batch_id'], 'revision': batch['revision'],
            'binding_digest': wire.digest(binding),
            'status': 'confirmed' if total > 0 and total == count else 'partial',
            'confirmed_file_count': count, 'total_file_count': total,
            'missing_file_count': total - count,
            'ignored_file_count': len(list(ignored)),
            'checked_at': checked_at, 'historical_only': True,
            'operation_authorized': False, 'jobs': sorted(jobs)[:50], 'job_count': len(jobs),
            'fact_count': len(records),
            'coverage_digest': wire.digest(sorted({r['fact_id'] for r in records})),
            'source_disposition': ('moved' if modes == {'move'} and count == total
                                   else 'partly_moved' if 'move' in modes
                                   else 'retained' if modes else 'unknown')}


def confirmed(batch):
    value = batch.get('archive_completion')
    if not isinstance(value, dict):
        return False
    return (value.get('schema') == SCHEMA and value.get('status') == 'confirmed'
            and value.get('historical_only') is True
            and value.get('operation_authorized') is False
            and value.get('batch_uid') == batch.get('batch_uid')
            and value.get('batch_id') == batch.get('batch_id')
            and type(value.get('revision')) is int
            and value['revision'] == batch.get('revision')
            and type(value.get('total_file_count')) is int
            and value['total_file_count'] > 0
            and type(value.get('confirmed_file_count')) is int
            and value['confirmed_file_count'] == value['total_file_count']
            and type(value.get('missing_file_count')) is int
            and value['missing_file_count'] == 0
            and batch.get('state') == 'COMPLETED'
            and batch.get('result') in ('COPY_VERIFIED', 'COPY_SIZE_VERIFIED'))
