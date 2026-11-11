"""Persistent-login API tests. No account credentials or external site access."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = load("profile_server", "browser/server.py")
worker = load("profile_worker", "browser/worker.py")
glados = load("profile_glados", "qinglong/glados.py")
entrypoint = load("profile_entrypoint", "browser/entrypoint.py")
TOKEN = "profile-test-token-" + "x" * 32


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="glados-profile-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.http = server.create_server(("127.0.0.1", 0), TOKEN)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.http.server_port)
        self.addCleanup(self.stop_server)
        self.headers = {"Authorization": "Bearer " + TOKEN}

    def stop_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=5)

    def test_login_page_requires_auth_and_basic_post_requires_csrf_header(self):
        response = requests.get(self.url, timeout=5)
        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response.headers["WWW-Authenticate"])
        session = requests.Session()
        session.auth = ("glados", TOKEN)
        self.assertEqual(session.get(self.url, timeout=5).status_code, 200)
        response = session.post(self.url + "/login/start", json={"profile_id": 1}, timeout=5)
        self.assertEqual(response.status_code, 403)
        session.close()

    def test_login_is_background_job_and_finish_has_no_credentials(self):
        entered, finish = threading.Event(), threading.Event()
        finish_path = self.root / "finish"

        def job_runner(job):
            self.assertEqual(job["operation"], "login")
            self.assertEqual(job["cookie"], "")
            self.assertIsNone(job["user_agent"])
            entered.set()
            finish.wait(timeout=5)
            return {"ok": True, "login_saved": True, "profile_id": 2}

        with patch.dict(os.environ, {"GLADOS_GUI_ENABLED": "1"}), \
                patch.object(server, "LOGIN_FINISH", finish_path), \
                patch.object(server, "execute_job", side_effect=job_runner):
            try:
                response = requests.post(self.url + "/login/start", headers=self.headers, json={"profile_id": 2}, timeout=5)
                self.assertEqual(response.status_code, 202)
                self.assertTrue(entered.wait(timeout=5))
                busy = requests.post(self.url + "/checkin", headers=self.headers, json={"session_mode": "profile", "profile_id": 2}, timeout=5)
                self.assertEqual(busy.status_code, 409)
                response = requests.post(self.url + "/login/finish", headers=self.headers, json={}, timeout=5)
                self.assertEqual(response.status_code, 202)
                self.assertTrue(finish_path.exists())
            finally:
                finish.set()
                with server.LOCK:
                    pass
        self.assertFalse(server.LOGIN_STATE["running"])

    def test_profile_mode_ignores_desktop_cookie_and_user_agent(self):
        env = {"GLADOS_MODE": "browser", "GLADOS_BROWSER_SESSION": "profile",
               "GLADOS_BROWSER_PROFILES": "2,1,2", "GLADOS_BROWSER_URL": self.url,
               "GLADOS_BROWSER_TOKEN": TOKEN, "GLADOS_COOKIE": "desktop=must-not-send",
               "GLADOS_USER_AGENT": "Windows must-not-send"}
        result = {"ok": True, "checkin": {"code": 0, "message": "Checkin! Got 1 Points"},
                  "state": {"code": 0, "data": {"leftDays": "12"}}}
        with patch.dict(os.environ, env, clear=True), patch.object(server, "execute_job", return_value=result) as execute, \
                patch.object(glados, "send_failure_email") as email, patch.object(glados, "send_wecom"):
            self.assertEqual(glados.main(), 0)
        self.assertEqual([c.args[0]["profile_id"] for c in execute.call_args_list], [2, 1])
        for call in execute.call_args_list:
            self.assertEqual(call.args[0]["cookie"], "")
            self.assertIsNone(call.args[0]["user_agent"])
        email.assert_not_called()

    def test_profile_missing_and_wrong_origin_stop_before_launch(self):
        with patch.object(worker, "PROFILE_ROOT", self.root), patch.object(worker, "STAGE_FILE", str(self.root / "stage")):
            result = worker.run_browser({"session_mode": "profile", "profile_id": 1})
            self.assertEqual(result["error"], "profile_not_ready")
            path = self.root / "1"
            path.mkdir()
            (path / "session.json").write_text(json.dumps({"origin": "https://different.invalid"}))
            result = worker.run_browser({"session_mode": "profile", "profile_id": 1})
            self.assertEqual(result["error"], "profile_origin_mismatch")

    def test_profile_paths_are_isolated_and_not_traversable(self):
        with patch.object(worker, "PROFILE_ROOT", self.root):
            for invalid in ("../1", "1", 0, 21, True):
                with self.assertRaises(ValueError):
                    worker.profile_path(invalid)
            with worker.browser_profile({"session_mode": "profile", "profile_id": 1}) as path:
                Path(path, "test").write_text("kept")
            self.assertTrue((self.root / "1/test").exists())
            self.assertNotEqual(worker.profile_path(1), worker.profile_path(2))

    def test_device_mismatch_reports_both_platforms(self):
        result = {"ok": True, "checkin": {"code": 4, "reason": "device-mismatch", "loginDevice": "Windows", "currentDevice": "Linux"},
                  "state": {"code": 0, "data": {"leftDays": "12"}}}
        with patch.object(glados, "browser_service_request", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "Windows.*Linux"):
                glados.browser_checkin_account("", 1)

    def test_origin_validation_and_vnc_password_validation(self):
        for bad in ("https://evil.invalid", "http://glados.one", "https://glados.one/path", "https://secret@glados.one"):
            with patch.dict(os.environ, {"GLADOS_ORIGIN": bad}), self.assertRaises(ValueError):
                worker.configured_origin()
        with patch.dict(os.environ, {"GLADOS_ORIGIN": "glados.one"}):
            self.assertEqual(worker.configured_origin(), "https://glados.one")
        for bad in ("", "short", "longpassword123", "password!", "1234567中"):
            with self.assertRaises(ValueError):
                entrypoint.validate_vnc_password(bad)
        self.assertEqual(entrypoint.validate_vnc_password("Abc12345"), "Abc12345")
