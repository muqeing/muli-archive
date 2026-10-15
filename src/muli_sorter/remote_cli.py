"""JSON client for the existing archive console; never operates on media locally."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import __version__

MAX_BYTES = 64 * 1024 * 1024
COMMANDS = {
    "health": "GET /health", "status": "GET /api/review-status",
    "discovery": "GET /api/discovery-status", "review": "GET /api/review-page (JSON model only)",
    "projects": "GET /api/review-page (projects only)", "segments": "GET /api/review-page (paged segments)",
    "plan-template": "GET /api/review-page (unconfirmed local plan)",
    "jobs": "GET /api/jobs?view=summary", "job": "GET /api/jobs/{id}",
    "check": "GET /api/preflight-checks/{id}", "ticket": "GET /api/confirmation-tickets/{id}",
    "submission": "GET /api/submissions/{id}",
    "preflight": "POST /api/preflight-checks", "cancel-check": "POST /api/preflight-checks/cancel",
    "submit": "POST /api/submissions", "retry": "POST /api/jobs/{id}/retry",
    "recover-submission": "POST /api/submissions/{id}/recover (explicit original-plan recovery)",
    "wait": "GET only polling of check, submission, or job", "version": "local version; optional remote GET",
}


class ClientError(Exception):
    def __init__(self, code, message, details=None, exit_code=3):
        super().__init__(message)
        self.code, self.details, self.exit_code = code, details, exit_code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ClientError("invalid_arguments", message, exit_code=2)


def number(value):
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number")
    if not math.isfinite(result) or not 0 < result <= 86400:
        raise argparse.ArgumentTypeError("must be finite and between 0 and 86400 seconds")
    return result


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def identifier(value):
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise argparse.ArgumentTypeError("expected a 64-character lowercase hex identifier")
    return value


def client_identifier(value):
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise argparse.ArgumentTypeError("client id must be 32 lowercase hex characters; generate one with uuid.uuid4().hex")
    return value


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("non-finite JSON value")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, url, timeout):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname and parsed.port != 0
                     and parsed.username is None and parsed.password is None and not parsed.query
                     and not parsed.fragment and parsed.path in ("", "/")
                     and not any(ord(c) <= 32 for c in url))
        except ValueError:
            valid = False
        if not valid:
            raise ClientError("invalid_url", "Use a service origin without credentials, path, query or fragment", exit_code=2)
        self.url, self.timeout = url.rstrip("/"), timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, body=None, *, html=False, timeout=None):
        headers = {"Accept": "text/html" if html else "application/json", "Origin": self.url,
                   "User-Agent": "muli-archive-cli/" + __version__}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            headers.update({"Content-Type": "application/json", "X-Muli-Request": "1"})
        request = Request(self.url + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ClientError("response_too_large", "Response exceeds the 64 MiB limit")
                if html and response.status == 200:
                    return raw.decode("utf-8")
                result = strict_json(raw)
                if html:
                    raise ClientError("page_not_ready", "Archive page is not ready", result, 4)
                if not isinstance(result, (dict, list)):
                    raise ValueError("expected JSON object or array")
                return result
        except HTTPError as exc:
            try:
                details = strict_json(exc.read(1024 * 1024))
            except (ValueError, UnicodeError):
                details = None
            finally:
                exc.close()
            failure = {"http_status": exc.code, "response": details}
            if body is not None and exc.code >= 500:
                failure.update(result_unknown=True, automatic_retry=False, method="POST", path=path)
            raise ClientError("http_error", f"Service returned HTTP {exc.code}", failure)
        except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
            details = {"method": request.method, "path": path}
            if body is not None:
                details.update(result_unknown=True, automatic_retry=False,
                               next_step="Read submission/check/job state before considering another write")
            raise ClientError("network_error", "Connection failed or timed out", details)
        except (ValueError, UnicodeError):
            raise ClientError("invalid_response", "Service returned an invalid response",
                              {"method": request.method, "path": path, "result_unknown": body is not None, "automatic_retry": False})


class ModelParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "review-model":
            self.active = True
            self.count += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def read_model(client):
    parser = ModelParser()
    parser.feed(client.request("/api/review-page", html=True))
    try:
        model = strict_json("".join(parser.parts))
        if (parser.count != 1 or not isinstance(model, dict) or not model.get("report_id")
                or not all(isinstance(model.get(k), list) for k in ("projects", "units", "initial_segments"))):
            raise ValueError()
        return model
    except (ValueError, TypeError):
        raise ClientError("incompatible_model", "Review model contract is missing or incompatible")


def decisions_file(path):
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        else:
            with Path(path).open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("decisions exceed size limit")
        value = strict_json(raw)
        if not isinstance(value, dict):
            raise ValueError("decisions must be an object")
        required = {"schema_version", "mode", "media_write_authorized", "report_id", "created_at", "segments"}
        optional = {"archive_options", "manual_projects"}
        if (not required <= value.keys() or value.keys() - required - optional
                or value["schema_version"] not in ("0.2", "0.3")
                or value["mode"] != "classification_confirmation_only"
                or value["media_write_authorized"] is not False or not isinstance(value["segments"], list)):
            raise ValueError("expected a classification confirmation plan; server validates scope and evidence")
        return value
    except (ValueError, OSError, UnicodeError) as exc:
        raise ClientError("invalid_decisions", str(exc), exit_code=2)


def parser():
    p = Parser(description="Archive API client for people and AI tools (JSON output; writes are previewed by default)")
    p.add_argument("--url", default=os.environ.get("MULI_ARCHIVE_URL", "http://127.0.0.1:18767"))
    p.add_argument("--timeout", type=number, default=15.0)
    p.add_argument("--version", action="store_true")
    commands = p.add_subparsers(dest="command", parser_class=Parser)
    for name in ("schema", "health", "status", "discovery", "jobs", "plan-template"):
        commands.add_parser(name)
    version = commands.add_parser("version")
    version.add_argument("--remote", action="store_true")
    review = commands.add_parser("review")
    review.add_argument("--full", action="store_true")
    projects = commands.add_parser("projects")
    projects.add_argument("--search", default="")
    segments = commands.add_parser("segments")
    segments.add_argument("--date", help="YYYY-MM-DD; compares capture dates, not ingest dates")
    segments.add_argument("--offset", type=int, default=0)
    segments.add_argument("--limit", type=positive, default=50)
    for name in ("job", "check", "ticket", "submission", "retry", "recover-submission"):
        sub = commands.add_parser(name)
        sub.add_argument("id", type=identifier)
        if name in ("retry", "recover-submission"):
            sub.add_argument("--execute", action="store_true")
            sub.add_argument("--confirmed", action="store_true")
    for name in ("preflight", "submit", "cancel-check"):
        sub = commands.add_parser(name)
        sub.add_argument("--execute", action="store_true")
        if name != "cancel-check":
            sub.add_argument("--decisions", required=True, help="UTF-8 JSON plan file, or - for stdin")
        if name in ("preflight", "cancel-check"):
            sub.add_argument("--client-id", required=True, type=client_identifier)
            sub.add_argument("--revision", required=True, type=positive)
        if name == "submit":
            sub.add_argument("--preview-id", required=True, type=identifier)
            sub.add_argument("--confirmed", action="store_true")
            sub.add_argument("--confirm-move", action="store_true", help="Explicitly authorize source removal if the plan uses move")
    wait = commands.add_parser("wait")
    wait.add_argument("kind", choices=("check", "submission", "job"))
    wait.add_argument("id", type=identifier)
    wait.add_argument("--deadline", type=number, default=300.0)
    wait.add_argument("--interval", type=number, default=2.0)
    return p


def resource(kind, token):
    return "/api/" + {"job": "jobs", "check": "preflight-checks", "ticket": "confirmation-tickets", "submission": "submissions"}[kind] + "/" + token


def check_result(value):
    if isinstance(value, dict) and value.get("status") in ("blocked", "rejected", "unknown", "not_found", "cancelled", "failed", "partial", "copied_unverified"):
        raise ClientError("operation_failed", "Operation needs attention; no automatic retry", value, 4)
    return value


def run(args):
    if args.command == "schema":
        return {"api_contract": 1, "commands": COMMANDS, "writes_default": "dry_run", "output_schema": 1,
                "exit_codes": {"success": 0, "invalid_input": 2, "transport_or_protocol": 3, "operation_failed": 4, "wait_timeout": 5},
                "decisions": {"schema_version": "0.3", "mode": "classification_confirmation_only", "media_write_authorized": False,
                              "report_id": "from plan-template", "created_at": "ISO8601 with timezone", "manual_projects": [],
                              "segments": [{"segment_id": "from segments", "label": "display name", "unit_ids": ["from segments"],
                                            "project_id": None, "decision": "pending|confirmed|deferred", "acknowledge_date_mismatch": False}],
                              "archive_options": {"mode": "copy|move", "existing": "error|skip_identical"}}}
    if args.version or args.command == "version" and not args.remote:
        return {"version": __version__, "api_contract": 1}
    if args.command is None:
        raise ClientError("invalid_arguments", "A command is required; see --help or schema", exit_code=2)
    if args.command == "segments":
        if args.offset < 0 or args.limit > 500 or args.date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
            raise ClientError("invalid_arguments", "offset >=0, limit <=500, date YYYY-MM-DD required", exit_code=2)
    # Preparing a mutation makes no connection, including no health request.
    if args.command in ("preflight", "submit", "cancel-check", "retry", "recover-submission"):
        body = {}
        if args.command in ("preflight", "submit"):
            body["decisions"] = decisions_file(args.decisions)
        if args.command in ("preflight", "cancel-check"):
            body.update(client_id=args.client_id, revision=args.revision)
            path = "/api/preflight-checks" + ("/cancel" if args.command == "cancel-check" else "")
        elif args.command == "submit":
            body.update(preview_id=args.preview_id, confirmed=args.confirmed)
            path = "/api/submissions"
            options = body["decisions"].get("archive_options", {"mode": "copy", "existing": "error"})
            if args.execute and options.get("mode") == "move" and not args.confirm_move:
                raise ClientError("confirmation_required", "Move submission requires --confirm-move for source removal", exit_code=2)
        elif args.command == "recover-submission":
            path, body = resource("submission", args.id) + "/recover", {"confirmed": args.confirmed}
        else:
            path, body = resource("job", args.id) + "/retry", {"confirmed": args.confirmed}
        if not args.execute:
            return {"dry_run": True, "request": {"method": "POST", "path": path, "body": body}, "network_used": False}
        if args.command in ("submit", "retry", "recover-submission") and not args.confirmed:
            raise ClientError("confirmation_required", "Execution requires --confirmed", exit_code=2)
        value = Client(args.url, args.timeout).request(path, body)
        return value if args.command == "cancel-check" else check_result(value)
    client = Client(args.url, args.timeout)
    if args.command == "version":
        remote = client.request("/health")
        return {"version": __version__, "server": remote, "exact_version_match": remote.get("version") == __version__, "api_contract": 1}
    simple = {"health": "/health", "status": "/api/review-status", "discovery": "/api/discovery-status", "jobs": "/api/jobs?view=summary"}
    if args.command in simple:
        return client.request(simple[args.command])
    if args.command in ("job", "check", "ticket", "submission"):
        return client.request(resource(args.command, args.id))
    if args.command == "wait":
        success = {"job": {"completed"}, "check": {"ready"}, "submission": {"accepted"}}[args.kind]
        active = {"job": {"queued", "running"}, "check": {"queued", "checking"}, "submission": {"checking"}}[args.kind]
        end = time.monotonic() + args.deadline
        last = None
        while time.monotonic() < end:
            last = client.request(resource(args.kind, args.id), timeout=min(args.timeout, max(0.001, end-time.monotonic())))
            check_result(last)
            state = last.get("status")
            if state in success:
                return last
            if state not in active:
                raise ClientError("unexpected_state", "Unknown state; not considered completed", last)
            time.sleep(min(args.interval, max(0, end-time.monotonic())))
        raise ClientError("wait_timeout", "Wait deadline reached; operation has not been stopped or retried", last, 5)
    model = read_model(client)
    if args.command == "review":
        return model if args.full else {"report_id": model["report_id"], "snapshot_at": model.get("snapshot_at"),
                                       "units": len(model["units"]), "segments": len(model["initial_segments"]),
                                       "projects": len(model["projects"]), "excluded_batches": model.get("excluded_batches", [])}
    if args.command == "projects":
        return {"report_id": model["report_id"], "projects": [p for p in model["projects"] if args.search.casefold() in json.dumps(p, ensure_ascii=False).casefold()]}
    if args.command == "plan-template":
        return {"schema_version": "0.3", "mode": "classification_confirmation_only", "media_write_authorized": False,
                "report_id": model["report_id"], "created_at": datetime.now(timezone.utc).isoformat(), "manual_projects": [],
                "segments": [{**s, "project_id": None, "decision": "pending", "acknowledge_date_mismatch": False} for s in model["initial_segments"]],
                "archive_options": {"mode": "copy", "existing": "skip_identical"}}
    if args.command == "segments":
        if args.offset < 0 or args.limit > 500:
            raise ClientError("invalid_arguments", "offset must be >=0 and limit <=500", exit_code=2)
        units = {u["unit_id"]: u for u in model["units"]}
        selected = [s for s in model["initial_segments"] if not args.date or any(units[uid].get("capture_date") == args.date for uid in s["unit_ids"])]
        return {"report_id": model["report_id"], "total": len(selected), "offset": args.offset,
                "segments": [{**s, "units": [units[uid] for uid in s["unit_ids"]]} for s in selected[args.offset:args.offset+args.limit]]}
    raise ClientError("invalid_arguments", "Unsupported command", exit_code=2)


def main(argv=None):
    command = None
    try:
        args = parser().parse_args(argv)
        command = args.command or ("version" if args.version else None)
        data = run(args)
        result, code = {"data": data}, 0
    except ClientError as exc:
        result, code = {"error": {"code": exc.code, "message": str(exc), "details": exc.details}}, exc.exit_code
    except (ValueError, KeyError, TypeError, RecursionError):
        result, code = {"error": {"code": "invalid_response", "message": "Input or server contract is incompatible; no automatic retry"}}, 3
    except KeyboardInterrupt:
        result, code = {"error": {"code": "interrupted", "message": "Client stopped; server operation was not cancelled"}}, 130
    print(json.dumps({"schema_version": 1, "ok": code == 0, "service": "muli-archive", "command": command,
                      "cli_version": __version__, **result}, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
