"""Opt-in real Chrome smoke test against a local fixture, never GLaDOS.

Install browser/requirements.txt and provide CHROME_BINARY first.
Run with GLADOS_BROWSER_SMOKE=1. Windows uses headless Chrome for the fixture.
"""
import importlib.util
import json
import os
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


@unittest.skipUnless(os.environ.get("GLADOS_BROWSER_SMOKE") == "1", "opt-in real Chrome fixture")
class BrowserSmokeTests(unittest.TestCase):
    def test_selftest_runs_javascript_without_external_site(self):
        from seleniumbase import SB
        spec = importlib.util.spec_from_file_location("selftest_worker", Path(__file__).resolve().parents[1] / "browser/worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)

        def fixture_browser(**options):
            if os.name == "nt":
                options["headless"] = True
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
