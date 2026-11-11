"""Exercise the service protocol over a local socket; no real account requests."""
import importlib.util
import json
import io
import os
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch

import requests


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


server = module("browser_server", "browser/server.py")
worker = module("browser_worker", "browser/worker.py")
glados = module("browser_glados", "qinglong/glados.py")
TOKEN = "offline-test-token-" + "x" * 32
SUCCESS = {
    "ok": True, "checkin": {"code": 0, "message": "Checkin! Got 1 Points"},
    "state": {"code": 0, "data": {"email": "test@example.com", "leftDays": "10.5"}},
}


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = server.create_server(("127.0.0.1", 0), TOKEN)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.http.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(timeout=5)

    def client(self):
        return patch.dict(os.environ, {
            "GLADOS_MODE": "browser", "GLADOS_BROWSER_URL": self.url,
            "GLADOS_BROWSER_TOKEN": TOKEN, "GLADOS_COOKIE": "gld:sess=fake",
        }, clear=True)

    def test_authentication_and_validation_prevent_browser_launch(self):
        with patch.object(server, "execute_job") as execute:
            cases = [
                ({}, {"cookie": "x=y"}, 401),
                ({"Authorization": "Bearer " + TOKEN}, {"cookie": "x=y", "timeout": 99999}, 400),
                ({"Authorization": "Bearer " + TOKEN}, {"cookie": "x=y", "user_agent": "x\ny"}, 400),
            ]
            for headers, payload, status in cases:
                response = requests.post(self.url + "/checkin", headers=headers, json=payload, timeout=5)
                self.assertEqual(response.status_code, status)
            execute.assert_not_called()

    def test_browser_mode_preserves_one_point_success_without_email(self):
        with self.client(), patch.object(server, "execute_job", return_value=SUCCESS) as execute, \
                patch.object(glados, "send_failure_email") as email, patch.object(glados, "send_wecom"):
            self.assertEqual(glados.main(), 0)
            execute.assert_called_once()
            self.assertIsNone(execute.call_args.args[0]["user_agent"])
            email.assert_not_called()

    def test_browser_mode_does_not_replay_error_or_leak_unknown_error(self):
        with self.client(), patch.object(server, "execute_job", return_value={"ok": False, "error": "private-cookie"}) as execute:
            with self.assertRaises(RuntimeError) as caught:
                glados.checkin_account("x=y", 1, 3, 30, 3, 30)
            execute.assert_called_once()
            self.assertNotIn("private-cookie", str(caught.exception))

    def test_browser_auth_failure_is_not_success(self):
        payload = dict(SUCCESS, checkin={"code": -2, "message": "Automated check-in detected"})
        with self.client(), patch.object(server, "execute_job", return_value=payload):
            with self.assertRaisesRegex(RuntimeError, "认证/设备验证"):
                glados.browser_checkin_account("x=y", 1)

    def test_concurrent_job_is_rejected_instead_of_queued(self):
        server.LOCK.acquire()
        try:
            with self.client(), patch.object(server, "execute_job") as execute:
                with self.assertRaisesRegex(RuntimeError, "另一项签到"):
                    glados.browser_checkin_account("x=y", 1)
                execute.assert_not_called()
        finally:
            server.LOCK.release()

    def test_client_timeout_never_retries(self):
        with self.client(), patch.object(glados.requests.Session, "post", side_effect=requests.Timeout) as post:
            with self.assertRaisesRegex(RuntimeError, "状态未知"):
                glados.browser_checkin_account("x=y", 1)
            post.assert_called_once()

    def test_worker_timeout_kills_process(self):
        with patch.object(server.subprocess, "Popen") as factory, patch.object(server.os, "killpg", create=True) as killpg:
            process = factory.return_value
            process.communicate.side_effect = [subprocess.TimeoutExpired("worker", 60), ("", None)]
            self.assertEqual(server.execute_job({"timeout": 60}), {"ok": False, "error": "timeout"})
            if os.name == "nt":
                process.kill.assert_called_once()
            else:
                killpg.assert_called_once()

    def test_cookie_parser_preserves_equals_and_rejects_injection(self):
        self.assertEqual(worker.parse_cookies("gld:sess=a==; gld:sess.sig=b"), {
            "gld:sess": "a==", "gld:sess.sig": "b",
        })
        for raw in ("", None, "a=b\r\nsecret=x", "Cookie: a=b", "missing-value"):
            with self.assertRaises(ValueError):
                worker.parse_cookies(raw)

    def test_selftest_requires_no_cookie_and_does_not_send_notifications(self):
        env = {
            "GLADOS_MODE": "browser", "GLADOS_BROWSER_URL": self.url,
            "GLADOS_BROWSER_TOKEN": TOKEN, "GLADOS_BROWSER_SELFTEST": "1",
        }
        with patch.dict(os.environ, env, clear=True), \
                patch.object(server, "execute_job", return_value={"ok": True, "selftest": True}) as execute, \
                patch.object(glados, "send_failure_email") as email, patch.object(glados, "send_wecom") as wecom:
            self.assertEqual(glados.main(), 0)
            job = execute.call_args.args[0]
            self.assertEqual(job["cookie"], "")
            self.assertEqual(job["operation"], "selftest")
            email.assert_not_called()
            wecom.assert_not_called()

    def test_diagnostics_expose_stage_and_class_without_raw_exception(self):
        result = {
            "ok": False, "error": "browser_error", "stage": "start_browser",
            "diagnostic": "TypeError", "category": "unclassified", "message": "private-cookie",
        }
        with self.client(), patch.object(server, "execute_job", return_value=result):
            with self.assertRaises(RuntimeError) as caught:
                glados.browser_checkin_account("x=y", 1)
            self.assertIn("stage=start_browser", str(caught.exception))
            self.assertIn("diagnostic=TypeError", str(caught.exception))
            self.assertNotIn("private-cookie", str(caught.exception))

    def test_worker_without_output_reports_exit_code(self):
        with patch.object(server.subprocess, "Popen") as factory:
            process = factory.return_value
            process.communicate.return_value = ("native library crash", None)
            process.returncode = -9
            result = server.execute_job({"timeout": 60})
            self.assertEqual(result["diagnostic"], "WorkerExit")
            self.assertEqual(result["exit_code"], -9)

    def test_exception_classification_never_exports_original_details(self):
        for message, expected in (
            ("chrome not reachable private-cookie", "chrome_start_failed"),
            ("only supports Chrome version 130", "driver_mismatch"),
            ("Cannot open display :99 private-cookie", "display_error"),
            ("unexpected private-cookie", "unclassified"),
            ("libtk8.6.so: cannot open shared object file", "tkinter_missing"),
        ):
            self.assertEqual(worker.classify_exception(RuntimeError(message)), expected)

    def test_mouseinfo_system_exit_returns_diagnostic_instead_of_silent_exit(self):
        def mouseinfo_exit(job, progress):
            progress["stage"] = "start_browser"
            raise SystemExit(0)
        with patch.object(worker, "run_browser", side_effect=mouseinfo_exit), \
                patch.object(worker.sys, "stdin", io.StringIO('{"cookie":"fake"}')), \
                patch.object(worker.sys, "argv", ["worker.py"]), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            worker.main()
            payload = json.loads(output.getvalue().split(worker.RESULT_MARKER)[1])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["diagnostic"], "SystemExit")
        self.assertEqual(payload["stage"], "start_browser")


if __name__ == "__main__":
    unittest.main()
