"""Opt-in real Chrome smoke test against a local fixture, never GLaDOS.

Install browser/requirements.txt and provide CHROME_BINARY first.
Run with GLADOS_BROWSER_SMOKE=1. Windows uses headless Chrome for the fixture.
"""
import importlib.util
import json
import os
from pathlib import Path
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


@unittest.skipUnless(os.environ.get("GLADOS_BROWSER_SMOKE") == "1", "opt-in real Chrome fixture")
class BrowserSmokeTests(unittest.TestCase):
    def test_native_login_profile_survives_browser_restart_and_does_not_inject_windows_cookie(self):
        from seleniumbase import SB
        spec = importlib.util.spec_from_file_location("persistent_smoke_worker", Path(__file__).resolve().parents[1] / "browser/worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        login_mode = [True]
        posts = []

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, content_type):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/user/status":
                    authenticated = "gld:sess=native-session" in self.headers.get("Cookie", "")
                    self.send(json.dumps({"code": 0 if authenticated else -2, "data": {"leftDays": "12", "email": "fixture@example.com"}}).encode(), "application/json")
                else:
                    script = "document.cookie='gld:sess=native-session;max-age=3600;path=/';" if login_mode[0] else ""
                    body = "<html><script>" + script + "</script><button class='ckb' onclick=\"this.disabled=true;fetch('/api/user/checkin',{method:'POST'})\">Checkin</button></html>"
                    self.send(body.encode(), "text/html")

            def do_POST(self):
                posts.append(self.headers.get("Cookie", ""))
                self.send(b'{"code":0,"message":"Checkin! Got 1 Points"}', "application/json")

        http = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()

        def fixture_browser(**options):
            self.assertNotIn("agent", options)
            if os.name == "nt":
                options["headless"] = True
                options["headed"] = False
            return SB(**options)

        try:
            with tempfile.TemporaryDirectory(prefix="glados-native-smoke-") as root, \
                    patch.object(worker, "ORIGIN", "http://127.0.0.1:" + str(http.server_port)), \
                    patch.object(worker, "PROFILE_ROOT", Path(root) / "profiles"), \
                    patch.object(worker, "STAGE_FILE", str(Path(root) / "stage")), \
                    patch("seleniumbase.SB", side_effect=fixture_browser):
                finish = Path(root) / "finish"
                finish.touch()
                result = worker.run_browser({"operation": "login", "profile_id": 1, "finish_file": str(finish), "timeout": 90,
                                             "cookie": "desktop=must-not-inject", "user_agent": "Windows must-not-inject"})
                self.assertTrue(result.get("login_saved"), result)
                self.assertEqual(posts, [])  # Login never checks in by itself.
                login_mode[0] = False
                result = worker.run_browser({"session_mode": "profile", "profile_id": 1, "timeout": 90,
                                             "cookie": "desktop=must-not-inject", "user_agent": "Windows must-not-inject"})
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["state"]["code"], 0)
                self.assertEqual(len(posts), 1)
                self.assertIn("gld:sess=native-session", posts[0])
                self.assertNotIn("desktop", posts[0])
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=5)

    def test_selftest_runs_javascript_without_external_site(self):
        from seleniumbase import SB
        spec = importlib.util.spec_from_file_location("selftest_worker", Path(__file__).resolve().parents[1] / "browser/worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)

        def fixture_browser(**options):
            if os.name == "nt":
                options["headless"] = True
                options["headed"] = False
            return SB(**options)

        with patch("seleniumbase.SB", side_effect=fixture_browser):
            result = worker.run_browser({"operation": "selftest", "timeout": 60})
        self.assertEqual(result, {"ok": True, "selftest": True, "stage": "complete"})

    def test_official_page_style_xhr_is_captured_once(self):
        from seleniumbase import SB
        spec = importlib.util.spec_from_file_location("smoke_worker", Path(__file__).resolve().parents[1] / "browser/worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        seen = []

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, content_type):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/user/status":
                    self.send(json.dumps({"code": 0, "data": {"email": "fixture@example.com", "leftDays": "12.5"}}).encode(), "application/json")
                else:
                    self.send(b'''<!doctype html><button class="ckb" onclick="this.disabled=true;
                      const xhr=new XMLHttpRequest(); xhr.open('POST','/api/user/checkin');
                      xhr.send('{}');">Check in</button>''', "text/html")

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                seen.append((self.path, self.headers.get("Cookie", "")))
                self.send(b'{"code":0,"message":"Checkin! Got 1 Points","nonce":"must-not-export"}', "application/json")

        http = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()

        def fixture_browser(**options):
            if os.name == "nt":
                options["headless"] = True
                options["headed"] = False
            return SB(**options)

        try:
            with patch.object(worker, "ORIGIN", "http://127.0.0.1:" + str(http.server_port)), \
                    patch("seleniumbase.SB", side_effect=fixture_browser):
                result = worker.run_browser({"cookie": "gld:sess=fake-value", "timeout": 60})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["state"]["data"]["email"], "fixture@example.com")
            self.assertEqual(result["checkin"]["message"], "Checkin! Got 1 Points")
            self.assertNotIn("nonce", result["checkin"])
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0][0], "/api/user/checkin")
            self.assertIn("gld:sess=fake-value", seen[0][1])
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=5)
