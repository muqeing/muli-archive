# Generated page retention (0.18)

This feature is opt-in. Installing source, the wheel, a zipapp or these templates does not enable a timer or delete pages. Media, ingest/postcopy receipts, archive requests/jobs/tickets, reservations, history and classification artifacts are outside its deletion scope.

Default policy retains seven days, at least 20 generations, current base/display pointers and active/unresolved references. One run selects at most 20 generations and 1 GB. A five-second classifier-freeze budget is a target checked between indivisible generation pairs, not a hard realtime guarantee. No archive/copy service restarts are performed.

## Tools and versions

The installed wheel provides `muli-archive-maintenance`. A standard-library-only host zipapp can be built with:

```sh
python tools/build_maintenance.py --output dist/muli-archive-maintenance.pyz
python3 -I -S dist/muli-archive-maintenance.pyz --version
python3 -I -S dist/muli-archive-maintenance.pyz policy
```

The zipapp reuses the host Python (3.9+) and needs no archive media dependencies, extra Python installation, Docker socket mount or new persistent container. Its version must match the accepted archive image. The normal package still requires Python 3.12. CI builds both distributions and the version-bound zipapp.

`config.disabled.example.json` is a disabled example, not a deployment manifest. Replace paths/storage/container identities with independently checked values. Do not copy secrets into it. Actual run requires the exact accepted archive image ID, matching runtime version, the archive `--enable-snapshot-retention` flag, matching bound reference roots, stable six-service state, healthy storage and a current explicitly confirmed policy digest.

## Commands

- `policy`: local defaults, or validate/print the configured policy and SHA.
- `status`: read maintenance summary and any unresolved run; no writes.
- `preview` / `run` without `--execute`: no index refresh, freeze or deletion. A stale/not-ready reference index blocks a misleading preview.
- `init`: defaults to dry-run; explicit `--execute --confirmed-policy SHA` initializes only owned maintenance metadata. It does not enable the archive owner or a timer.
- `bootstrap`: explicit initial control-record indexing under the gate. No media traversal or page deletion. Run only in an approved idle maintenance window; first indexing costs have not been measured on production.
- `shadow`: defaults to dry-run. Explicit execution updates only the derived reference index, reports candidates and never freezes or deletes page files. It is read-only with respect to original business records, not to its own index.
- `run --execute --confirmed-policy SHA`: also requires `policy.enabled=true`. Index changes are budgeted; incomplete indexing returns `indexing`, activity returns `busy`, no eligible candidates returns `nothing_to_clean`.
- `recover-freezer`: explicit policy confirmation reconciles only the original classifier ID recorded by a pending run. It never repeats deletions or acknowledges the unknown run as complete; manual review remains required.

## Reference safety

The archive owner registers a shared gate around old-page preflight reads and control writes. Reference-meaning changes get durable pending fences. Progress-only job updates avoid another fence per media file. Failed fences block cleanup; they are never silently cleared.

Maintenance refreshes changed control metadata only. Large canonical requests/models are streamed in 64 KiB chunks; unchanged records are not decoded again. The derived SQLite index is not business authority. Its corruption stops cleanup while the original control-write path remains usable. Root identity/gate damage still fails closed.

A completed job releases its display-page pin only for a known admitted, self-contained request whose original full SHA, job ID, persistent roots and plan hashes bind. Unknown/failed/retained work stays protected; unexpired tickets (plus 120 seconds) and unresolved submissions also pin. The original controls and receipts remain on disk. No target/source media checks are weakened.

## Execution and recovery

Inventory happens outside the maintenance exclusive gate. Under the gate, refresh the index, select with fresh pins, recheck activity/instances, journal a fixed plan, freeze only the original classifier, validate no-follow directory/file identities and unlink only the two generated filenames. Stop at the first budget and always attempt to unfreeze that same instance.

Intents and per-generation results are durable. A killed process leaves `inflight.json`; all subsequent page deletion stops. The supplied service template has `ExecStopPost` reconciliation. Reboots, identity changes and unresolved deletions require inspection; do not remove fences or replay a run merely to continue. Successful maintenance logs alone can expire after 90 days; failed/pending logs and all original audits remain.

## Deployment gate

The `.in` units are templates only. Before deployment, obtain approval for the exact image/config/roots, archive feature flag, owner metadata/index initialization, user timer paths and any future page/log deletion policy. Do not reuse approval for an earlier one-time cleanup.

Recommended sequence:

1. Local synthetic and installed CLI checks; isolated NAS image checks without production media.
2. Approved idle archive update with the owner gate enabled; retain other service instances and resource/mount/security settings.
3. Bind/install the matching host zipapp with `enabled=false`; explicit bootstrap and a shadow service/timer. Shadow updates only its own index and logs.
4. Observe shadow candidates, indexing cost and business submission latency for 24 hours. No page deletion or classifier freeze is performed in shadow mode.
5. Present measured scope/results and obtain explicit approval before enabling `enabled=true` and its new policy SHA. Perform a small actual run and independently read back results.

Disable timers before changing an owner/maintenance version, coordinate unresolved runs, then install the matching validated versions. Do not restart unrelated services. After disabling the real timer there are no subsequent scheduled deletions; original business state is never rolled back.

The source tests simulate copies, both move strategies, interrupted admission recovery and feedback with page files removed; OS-kill recovery is also simulated. These are not production media acceptance, NAS latency measurements or a completed natural timer cycle.
