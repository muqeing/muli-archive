# Permanent completion records

`completion_facts: true` in the existing channel configuration, or the explicit
`MULI_COMPLETION_FACTS=1` service environment, selects the new exchange. The
default remains the original short-lived exchange. Upgrade both endpoints
together; start the archive producer before the ingest consumer so that its
pending directory exists. Keep the channel mounts and permissions unchanged.

The archive owns a durable activation point and pending-work index. It does not
scan or import historical jobs on activation. Only a newly completed job can
produce a `muli-archive-completion/1` fact, after the original request, source,
content-verification and archive-result checks. A prepared fact is persisted
before publication and retains the same identity across an interrupted exchange.

Ingest checks the exact batch UID, revision, original manifest and per-file
coverage, then transactionally stores coverage, the completion projection and
the acknowledgement. Duplicate delivery only republishes the same acknowledgement.
Partial coverage stays partial; changed revisions or conflicting targets require
review. An acknowledged pending marker can leave the active directory; the fact,
original job and receipts remain durable. Neither endpoint periodically renews
an acknowledged permanent record.

The batch's `archive_completion` projection is a historical record. `confirmed`
means all eligible original files are covered for that exact revision. It does
not mean today's target files have just been checked. Both
`historical_only: true` and `operation_authorized: false` are required. These
records never grant source deletion, card formatting or another move. Existing
temporary permissions still expire; they are not silently made permanent.

Channel digests detect changed content; they are not signatures from an external
authority. The original filesystem ownership and separate producer/consumer
write permissions remain the trust boundary. Preserve failed controls and
inspect them explicitly; do not replace facts, clear an unresolved intent or
replay media operations to recover an acknowledgement.

Read-only CLI/API clients receive the same versioned status fields as the web
interface. Client/source versions ship together. An old partial or unverified
record is not automatically migrated by a software update. Production health,
synthetic exchange checks and a real newly completed archive are separate
acceptance results.
