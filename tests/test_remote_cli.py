"""Exercise the client over a synthetic HTTP service, without media mounts."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from muli_sorter import __version__
from muli_sorter.remote_cli import Client, ClientError, main

TOKEN = "a" * 64
MODEL = {"report_id": "sha256:" + TOKEN, "projects": [{"project_id": "example", "name": "Synthetic"}],
         "units": [{"unit_id": "u1", "capture_date": "2026-01-01", "file_names": ["example.mp4"]}],
         "initial_segments": [{"segment_id": "s1", "label": "Synthetic", "unit_ids": ["u1"], "decision": "pending", "project_id": None}]}


class RemoteCliTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.responses = [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length)) if length else None
                outer.calls.append((self.command, self.path, dict(self.headers), body))
                status, value = outer.responses.pop(0) if outer.responses else (200, {"status": "ok"})
                raw = value.encode() if isinstance(value, str) else json.dumps(value).encode()
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/should-never-follow")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.plan = Path(self.temp.name) / "plan.json"
        self.decisions = {"schema_version": "0.3", "mode": "classification_confirmation_only", "media_write_authorized": False,
                          "report_id": MODEL["report_id"], "created_at": "2026-01-01T00:00:00+00:00", "segments": [], "manual_projects": []}
        self.plan.write_text(json.dumps(self.decisions))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def call(self, *argv):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(["--url", self.url, *argv])
        result = json.loads(stream.getvalue())
        self.assertEqual(result["ok"], code == 0)
        return code, result

    def model(self):
        self.responses.append((200, '<script id="review-model" type="application/json">' + json.dumps(MODEL) + '</script><img src="/never-fetch.jpg">'))

    def test_health_only_get_and_json_envelope(self):
        code, result = self.call("health")
        self.assertEqual((code, result["cli_version"], result["schema_version"]), (0, __version__, 1))
        self.assertEqual(self.calls[0][:2], ("GET", "/health"))
        self.assertEqual(self.calls[0][2]["Origin"], self.url)

    def test_model_extract_does_not_request_media(self):
        self.model()
        code, result = self.call("segments", "--date", "2026-01-01")
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["total"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "/api/review-page")

    def test_plan_template_never_confirms(self):
        self.model()
        code, result = self.call("plan-template")
        self.assertEqual(code, 0)
        self.assertIsNone(result["data"]["segments"][0]["project_id"])
        self.assertEqual(result["data"]["segments"][0]["decision"], "pending")
        self.assertEqual(result["data"]["archive_options"]["mode"], "copy")

    def test_write_dry_run_zero_network(self):
        code, result = self.call("submit", "--preview-id", TOKEN, "--decisions", str(self.plan))
        self.assertEqual(code, 0)
        self.assertTrue(result["data"]["dry_run"])
        self.assertFalse(result["data"]["network_used"])
        self.assertEqual(self.calls, [])

    def test_confirmation_and_move_guard(self):
        code, _ = self.call("submit", "--preview-id", TOKEN, "--decisions", str(self.plan), "--execute")
        self.assertEqual(code, 2)
        self.decisions["archive_options"] = {"mode": "move", "existing": "skip_identical"}
        self.plan.write_text(json.dumps(self.decisions))
        code, result = self.call("submit", "--preview-id", TOKEN, "--decisions", str(self.plan), "--execute", "--confirmed")
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "confirmation_required")
        self.assertEqual(self.calls, [])

    def test_execute_headers_and_exact_payload_no_repeat(self):
        self.responses.append((202, {"status": "checking", "submission_id": TOKEN}))
        code, result = self.call("submit", "--preview-id", TOKEN, "--decisions", str(self.plan), "--execute", "--confirmed")
        self.assertEqual(code, 0)
        method, path, headers, body = self.calls[0]
        self.assertEqual((method, path), ("POST", "/api/submissions"))
        self.assertEqual(headers["X-Muli-Request"], "1")
        self.assertEqual(body, {"preview_id": TOKEN, "decisions": self.decisions, "confirmed": True})
        self.assertEqual(result["data"]["status"], "checking")
        self.assertEqual(len(self.calls), 1)

    def test_blocked_check_has_details_and_failed_exit(self):
        issues = [{"segment_id": "s1", "source_path": "synthetic.mp4"}]
        self.responses.append((200, {"status": "blocked", "issues": issues}))
        code, result = self.call("preflight", "--decisions", str(self.plan), "--client-id", "0123456789abcdef0123456789abcdef", "--revision", "1", "--execute")
        self.assertEqual(code, 4)
        self.assertEqual(result["error"]["details"]["issues"], issues)

    def test_wait_completed_is_read_only(self):
        self.responses.extend([(200, {"status": "running"}), (200, {"status": "completed"})])
        code, result = self.call("wait", "job", TOKEN, "--interval", "0.01", "--deadline", "1")
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["status"], "completed")
        self.assertTrue(all(c[0] == "GET" for c in self.calls))

    def test_wait_failures_and_unknown_never_complete(self):
        for state, expected in (("partial", 4), ("copied_unverified", 4), ("unknown", 4), ("invented", 3)):
            with self.subTest(state=state):
                self.responses.append((200, {"status": state}))
                code, _ = self.call("wait", "job", TOKEN)
                self.assertEqual(code, expected)

    def test_wait_timeout_does_not_cancel(self):
        self.responses.extend([(200, {"status": "checking"})] * 3)
        code, result = self.call("wait", "check", TOKEN, "--deadline", "0.02", "--interval", "0.05")
        self.assertEqual(code, 5)
        self.assertEqual(result["error"]["details"]["status"], "checking")
        self.assertTrue(all(c[0] == "GET" for c in self.calls))

    def test_reject_redirect_no_follow(self):
        self.responses.append((302, {"error": "redirect"}))
        code, _ = self.call("health")
        self.assertEqual(code, 3)
        self.assertEqual(len(self.calls), 1)

    def test_network_write_unknown_never_retries(self):
        with patch("muli_sorter.remote_cli.Client.request", side_effect=ClientError("network_error", "timeout", {"result_unknown": True, "automatic_retry": False})) as request:
            code, result = self.call("retry", TOKEN, "--execute", "--confirmed")
        self.assertEqual(code, 3)
        self.assertTrue(result["error"]["details"]["result_unknown"])
        self.assertEqual(request.call_count, 1)

    def test_real_transport_unknown_write(self):
        client = Client(self.url, 0.01)
        with patch.object(client.opener, "open", side_effect=TimeoutError):
            with self.assertRaises(ClientError) as raised:
                client.request("/api/submissions", {"confirmed": True})
        self.assertTrue(raised.exception.details["result_unknown"])

    def test_http_diagnostics_preserved(self):
        self.responses.append((409, {"error": "changed", "issues": [{"unit_id": "u1"}]}))
        code, result = self.call("health")
        self.assertEqual(code, 3)
        self.assertEqual(result["error"]["details"]["response"]["issues"][0]["unit_id"], "u1")

    def test_page_building_not_ready(self):
        self.responses.append((202, {"status": "building"}))
        code, result = self.call("review")
        self.assertEqual(code, 4)
        self.assertEqual(result["error"]["code"], "page_not_ready")

    def test_invalid_input_zero_network(self):
        for argv in (("job", "../bad"), ("wait", "job", TOKEN, "--deadline", "nan"), ("segments", "--offset", "-1"), ("segments", "--limit", "501")):
            code, _ = self.call(*argv)
            self.assertEqual(code, 2)
        for raw in ('{"segments":[],"segments":[]}', '{"segments":NaN}', '[]'):
            self.plan.write_text(raw)
            code, _ = self.call("preflight", "--client-id", "0123456789abcdef0123456789abcdef", "--revision", "1", "--decisions", str(self.plan))
            self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])

    def test_url_credentials_paths_and_proxy_rejected(self):
        for url in ("file:///tmp/test", "http://user:pass@localhost", "http://@localhost", "http://localhost/api", "http://localhost?token=test"):
            with self.subTest(url=url), self.assertRaises(ClientError):
                Client(url, 1)
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1"}):
            self.assertEqual(self.call("health")[0], 0)

    def test_version_metadata_and_module_entrypoint(self):
        root = Path(__file__).resolve().parents[1]
        version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
        self.assertEqual(version, __version__)
        result = subprocess.run([sys.executable, "-m", "muli_sorter.remote_cli", "--version"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["cli_version"], version)
        self.assertEqual(self.calls, [])

    def test_cancel_returns_success_for_expected_cancelled_state(self):
        self.responses.append((200, {"status": "cancelled"}))
        code, result = self.call("cancel-check", "--client-id", "0123456789abcdef0123456789abcdef", "--revision", "1", "--execute")
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["status"], "cancelled")


from test_archive_jobs import Fixture


class ExistingConsoleClientTests(Fixture, unittest.TestCase):
    def test_synthetic_preflight_submit_and_verified_copy_through_existing_api(self):
        from muli_sorter.archive_console import make_server
        server = make_server(self.service, self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = server.public_origin
        plan = self.root / "cli-plan.json"
        plan.write_text(json.dumps(self.decisions))
        before = {p.relative_to(self.root / "staging").as_posix(): p.read_bytes()
                  for p in (self.root / "staging").rglob("*") if p.is_file()}

        def call(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--url", url, *argv])
            value = json.loads(out.getvalue())
            self.assertEqual(code, 0, value)
            return value["data"]
        try:
            check = call("preflight", "--decisions", str(plan), "--client-id", "0123456789abcdef0123456789abcdef", "--revision", "1", "--execute")
            ready = call("wait", "check", check["check_id"], "--deadline", "5", "--interval", "0.01")
            ticket = ready["preview_id"]
            call("submit", "--decisions", str(plan), "--preview-id", ticket, "--execute", "--confirmed")
            accepted = call("wait", "submission", ticket, "--deadline", "5", "--interval", "0.01")
            self.assertEqual(accepted["status"], "accepted")
            done = self.service.run_job(accepted["job_id"])
            result = call("wait", "job", accepted["job_id"], "--deadline", "5")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(done["summary"]["completed_files"], 4)
            self.assertEqual(before, {p.relative_to(self.root / "staging").as_posix(): p.read_bytes()
                                     for p in (self.root / "staging").rglob("*") if p.is_file()})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
