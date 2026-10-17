"""One-time producer exchange. Only pending controls enter the worker loop."""
import json
import os
from pathlib import Path
from . import handoff_protocol as wire
from . import completion_facts as facts
from .completion_workflow import CompletionWorkflow


def initialize(producer):
    pending = producer.outbox / 'completion-pending'
    try:
        pending.mkdir(mode=0o700)
    except FileExistsError:
        pass
    producer.completion_pending = pending
    producer.completion_pending_identity = wire.identity(pending)
    producer.completion_workflow = CompletionWorkflow(producer.service.state,
        {'schema': facts.SCHEMA, 'roots': producer.service.identity,
         'channels': producer.identities, 'pending': producer.completion_pending_identity,
         'since': producer.since})


def roots(producer):
    producer.roots()
    if wire.identity(producer.completion_pending) != producer.completion_pending_identity:
        raise ValueError('completion_pending_directory_replaced')


def remove_marker(producer, jid, fact_id):
    roots(producer)
    try:
        value = wire.read(producer.completion_pending, jid + '.json')
    except FileNotFoundError:
        return
    if value != {'schema': 'completion-pending/1', 'job_id': jid, 'fact_id': fact_id}:
        raise ValueError('completion_pending_changed')
    with wire.directory(producer.completion_pending) as fd:
        # Only this feature's acknowledged marker is removed; facts stay durable.
        os.unlink(jid + '.json', dir_fd=fd)
        os.fsync(fd)


def acknowledge(producer, jid, fact):
    try:
        ack = wire.read(producer.acks, jid + '.completion.json')
    except FileNotFoundError:
        return False
    event = facts.validate(fact)
    if (ack.get('schema') == facts.ACK and ack.get('job_id') == jid
            and ack.get('fact_id') == fact['fact_id'] and ack.get('status') == 'needs_review'
            and ack.get('historical_only') is True and ack.get('operation_authorized') is False):
        wire.timestamp(ack['checked_at'])
        remove_marker(producer, jid, fact['fact_id'])
        producer.completion_workflow.fail(jid, ack.get('reason', 'consumer_requires_review'))
        return True
    if (ack.get('schema') != facts.ACK or ack.get('job_id') != jid
            or ack.get('fact_id') != fact['fact_id'] or ack.get('status') != 'recorded'
            or ack.get('historical_only') is not True
            or ack.get('operation_authorized') is not False
            or {x.get('batch_uid') for x in ack.get('batches', [])}
               != {x['binding']['batch_uid'] for x in event['batches']}):
        raise ValueError('invalid_completion_acknowledgement')
    wire.timestamp(ack['checked_at'])
    # Retire the marker before committing recorded. A crash here is recovered by
    # reading the same persisted fact and ack; no new evidence or task is made.
    remove_marker(producer, jid, fact['fact_id'])
    producer.completion_workflow.record(jid, fact['fact_id'], ack)
    return True


def tick(producer, clock):
    from .archive_handoff import FeedbackDeferred
    if not producer.idle():
        return
    roots(producer)
    work = producer.completion_workflow.next(clock)
    if work is None:
        return
    jid = work['job_id']
    producer.completion_workflow.defer(jid, clock)
    try:
        fact = json.loads(work['fact']) if work['fact'] else None
        if fact is None:
            job = producer.service.get(jid)
            if job.get('status') in ('queued', 'running'):
                return  # Control write may have crashed after registration.
            if (job.get('status') != 'completed'
                    or producer.completion_workflow.binding(job) != work['binding']):
                raise ValueError('completion_job_binding_changed')
            del job
            event = wire.seal({**producer.build(jid, clock), 'sequence': 1})
            fact = facts.create(event, published_at=clock)
            producer.completion_workflow.prepare(jid, work['binding'], fact)
        event = facts.validate(fact)
        if event['job_id'] != jid:
            raise ValueError('completion_fact_job_changed')
        if acknowledge(producer, jid, fact):
            return
        filename = jid + '.completion.json'
        try:
            previous = wire.read(producer.outbox, filename)
        except FileNotFoundError:
            wire.write(producer.outbox, filename, fact)
        else:
            if previous != fact:
                raise ValueError('completion_fact_replacement')
        marker = {'schema': 'completion-pending/1', 'job_id': jid, 'fact_id': fact['fact_id']}
        try:
            prior_marker = wire.read(producer.completion_pending, jid + '.json')
        except FileNotFoundError:
            wire.write(producer.completion_pending, jid + '.json', marker)
        else:
            if prior_marker != marker:
                raise ValueError('completion_pending_replacement')
        producer.completion_workflow.published(jid)
    except FeedbackDeferred:
        return
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # No automatic proof/media retry after an explicit failure. Preserve
        # the fact/intent and isolate this job for an explicit investigation.
        producer.completion_workflow.fail(jid, str(exc))
