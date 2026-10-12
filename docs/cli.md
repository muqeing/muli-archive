# Archive CLI for AI tools

`muli-archive` is the HTTP client in the same Python distribution as the service.
It uses the existing console API and does not mount or operate on media locally.
Existing `muli-sorter` preview and server commands remain available.
Python 3.12 or later is required. No new NAS deployment is needed to use the client
with the existing console API.

## Install and update

```sh
uv tool install --from git+https://github.com/muqeing/muli-archive.git muli-sorter-preview
uv tool upgrade muli-sorter-preview
muli-archive --version
```

For a reproducible version, install from a Git tag or commit instead of the default
branch. GitHub Actions builds and tests the wheel on each push, installs that exact
wheel in a clean environment, and uploads it as a workflow artifact. The CLI version
must match package metadata. Source, CLI, tests and this contract are released together;
`version --remote` reports the actual server version separately. A NAS image does not
update merely because a GitHub branch or local client was updated.

## Read-only discovery

Set `MULI_ARCHIVE_URL` to the configured service origin, or put `--url` before the
command. The default is `http://127.0.0.1:18767`; a LAN console must use its configured
origin. Do not put credentials in a URL. Proxies and redirects are disabled.

```sh
muli-archive schema
muli-archive health
muli-archive status
muli-archive discovery
muli-archive version --remote
muli-archive review
muli-archive projects --search example
muli-archive segments --date 2026-01-01 --limit 20
muli-archive jobs
muli-archive job JOB_ID
```

`review` summarizes the current model; `review --full` returns the full model.
`segments` returns up to 500 segments per call, with unit IDs, capture times, files,
source evidence and candidate projects; use `--offset` for more. These commands read
the existing JSON model embedded in `/api/review-page`; they do not execute page
scripts, fetch thumbnails, or request media. Page generation may return `page_not_ready`.
Do not treat an empty date filter or a page that is still building as proof that all
materials have been archived.

## Explicit archive workflow

`plan-template` emits a current-report plan with **every segment pending and no
project selected**. Extract `data` from the JSON envelope into a local UTF-8 JSON
file, then explicitly choose the segments, unit IDs and project IDs to confirm.
Keep the report ID and source evidence unchanged. The server checks the selected
scope, date acknowledgements, content proofs, conflicts and coordination locks.

```sh
muli-archive plan-template > plan-envelope.json
python3 -c 'import json; p=json.load(open("plan-envelope.json")); json.dump(p["data"],open("decisions.json","w"),ensure_ascii=False,indent=2)'
# Edit decisions.json to reflect the operator-approved assignments.
muli-archive preflight --client-id 0123456789abcdef0123456789abcdef --revision 1 --decisions decisions.json
muli-archive preflight --client-id 0123456789abcdef0123456789abcdef --revision 1 --decisions decisions.json --execute
muli-archive wait check CHECK_ID --deadline 600
muli-archive ticket PREVIEW_ID
muli-archive submit --preview-id PREVIEW_ID --decisions decisions.json
muli-archive submit --preview-id PREVIEW_ID --decisions decisions.json --execute --confirmed
muli-archive wait submission PREVIEW_ID
muli-archive wait job JOB_ID --deadline 3600
```

Replace uppercase IDs with the exact IDs returned by the service. Preflight returns
`check_id`; a ready check provides `preview_id`; the submission uses that preview ID
as its durable submission ID and eventually returns `job_id`. `accepted` means the
submission was accepted, **not** that the archive job completed. A ready ticket is
not an authorization to change assignments or move additional materials.

POST commands are previews by default: the client prints the method, path and body
without connecting. `--execute` sends exactly one request. A move plan additionally
requires `--confirm-move` on submit because its execution may remove staging sources.
`--confirmed` is required for submit and retry. These flags record the caller's
intent; they do not replace human authorization in an AI tool's own operating rules.

`cancel-check --client-id ... --revision ... --execute` cancels a preflight explicitly.
`retry JOB_ID --execute --confirmed` retries only a previously failed/partial job;
inspect its original scope and move policy first. The client never retries automatically.
Input can also come from stdin with `--decisions -`.

## Machine contract

Every operational response is one UTF-8 JSON object on stdout:

```json
{"schema_version":1,"ok":true,"service":"muli-archive","command":"job","cli_version":"0.16.3","data":{"status":"running"}}
```

Failures use `error: {code, message, details}`. HTTP and operation failures preserve
server `issues`, including segment/unit IDs and file paths. Exit codes are 0 for a
successful request, 2 for invalid input, 3 for HTTP/network/protocol errors, 4 for a
blocked/failed operation, 5 for a wait deadline, and 130 for an interrupted client.
`--help` remains conventional text. Querying a failed job succeeds as a read request;
`wait job` exits 4 on failed/partial/unverified jobs.

`wait` only polls GET. It never cancels, retries, submits another task, or interprets
an unknown state as success. A transport timeout during POST may leave the result
unknown: read `submission PREVIEW_ID`, `check CHECK_ID`, or `job JOB_ID` before any
further write. Do not invent success or repeat a timed-out submission blindly.
Default request timeout is 15 seconds; wait defaults to 300 seconds and 2-second
polls. They are bounded, positive, finite values.

Use the schema command and command help when adapting an AI agent. Do not put
customer plans, receipts, media, access codes or private screenshots in public Git.
