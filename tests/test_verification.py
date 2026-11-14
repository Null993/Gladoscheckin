"""Verification controller and human handoff tests, without Cloudflare access."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load("verification_worker", "browser/worker.py")
server = load("verification_server", "browser/server.py")
glados = load("verification_glados", "qinglong/glados.py")
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"
TOKEN = "verification-test-" + "x" * 32
BOX = {"x": 50, "y": 60, "width": 300, "height": 65}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stage = patch.object(worker, "STAGE_FILE", str(self.root / "stage"))
        self.stage.start()
        self.addCleanup(self.stage.stop)
        self.browser = Mock()
        self.browser.execute_script.return_value = {"state": "pending", "resources": True}

    def test_three_targeted_attempts_then_manual_and_automatic_resume(self):
        control = worker.TurnstileController(manual_wait=30)
        with patch.object(worker, "find_turnstile_frame", return_value=BOX), \
                patch.object(worker, "click_turnstile_checkbox") as click, \
                patch.object(worker, "capture_verification") as capture:
            for timestamp in (0, 5, 13, 21):
                with patch.object(worker.time, "monotonic", return_value=timestamp):
                    self.assertEqual(control.tick(self.browser, {}), "pending")
            with patch.object(worker.time, "monotonic", return_value=29):
                self.assertEqual(control.tick(self.browser, {}), "manual")
            self.assertEqual(click.call_count, 3)
            capture.assert_called_once()
            self.assertTrue(worker.verification_file("json").exists())
            with patch.object(worker.time, "monotonic", return_value=30):
                self.assertEqual(control.tick(self.browser, {}), "manual")
            self.assertEqual(click.call_count, 3)  # No interference during human control.
            self.browser.execute_script.return_value = {"state": "ready"}
            with patch.object(worker.time, "monotonic", return_value=31):
                self.assertEqual(control.tick(self.browser, {}), "ready")
            self.assertFalse(worker.verification_file("json").exists())

    def test_already_successful_widget_is_never_clicked(self):
        self.browser.execute_script.return_value = {"state": "ready"}
        with patch.object(worker, "click_turnstile_checkbox") as click:
            self.assertEqual(worker.TurnstileController().tick(self.browser, {}), "ready")
            click.assert_not_called()

    def test_unlocated_widget_is_forwarded_without_blind_clicks(self):
        control = worker.TurnstileController(manual_wait=10)
        with patch.object(worker, "find_turnstile_frame", return_value=None), \
                patch.object(worker, "click_turnstile_checkbox") as click, \
                patch.object(worker, "capture_verification"):
            for timestamp, expected in ((0, "pending"), (35, "manual"), (46, "expired")):
                with patch.object(worker.time, "monotonic", return_value=timestamp):
                    self.assertEqual(control.tick(self.browser, {}), expected)
            click.assert_not_called()

    def test_closed_shadow_tree_only_selects_real_provider_host(self):
        self.browser.driver.execute_cdp_cmd.side_effect = [
            {}, {"root": {"shadowRoots": [{"children": [{"nodeName": "IFRAME", "backendNodeId": 9,
                "attributes": ["src", "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/widget/turnstile/a"]}]}]}},
            {"object": {"objectId": "fixture-node"}}, {"result": {"value": BOX}}, {},
        ]
        self.assertEqual(worker.find_turnstile_frame(self.browser.driver), BOX)
        self.browser.driver.execute_cdp_cmd.reset_mock(side_effect=True)
        self.browser.driver.execute_cdp_cmd.side_effect = [{}, {"root": {"children": [
            {"nodeName": "IFRAME", "attributes": ["src", "https://challenges.cloudflare.com.evil.invalid/turnstile/widget"]}
        ]}}]
        self.assertIsNone(worker.find_turnstile_frame(self.browser.driver))


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patcher = patch.object(server, "STAGE_FILE", self.root / "stage")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.http = server.create_server(("127.0.0.1", 0), TOKEN)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.http.server_port)
        self.addCleanup(self.stop_server)
        self.env = {"GLADOS_MODE": "browser", "GLADOS_BROWSER_SESSION": "profile",
                    "GLADOS_BROWSER_TOKEN": TOKEN, "GLADOS_BROWSER_URL": self.url}

    def stop_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=5)

    def write_challenge(self):
        server.verification_path("json").write_text(json.dumps({"waiting": True, "since": time.time(), "wait_seconds": 300}))
        server.verification_path("png").write_bytes(PNG)

    def test_notification_with_image_then_same_post_resumes(self):
        human = threading.Event()
        result = {"ok": True, "checkin": {"code": 0, "message": "Checkin! Got 1 Points"},
                  "state": {"code": 0, "data": {"leftDays": "12"}}}

        def job(payload):
            self.write_challenge()
            self.assertTrue(human.wait(timeout=8))
            return result

        def email(subject, content, image_data):
            self.assertIn("人工验证", subject)
            self.assertIn(self.url, content)
            self.assertEqual(image_data, PNG)
            human.set()

        with patch.dict(os.environ, self.env, clear=True), patch.object(server, "execute_job", side_effect=job) as execute, \
                patch.object(glados, "send_wecom") as wecom, patch.object(glados, "send_failure_email", side_effect=email) as mail:
            self.assertIn("Got 1 Points", glados.browser_checkin_account("", 1))
            execute.assert_called_once()
            mail.assert_called_once()
            wecom.assert_called_once()
        self.assertFalse(server.verification_path("png").exists())

    def test_screenshot_requires_authentication_and_matching_job(self):
        with server.TASK_STATE_LOCK:
            server.TASK_STATE.update(running=True, job_id="correct", operation="checkin", profile_id=1)
        self.write_challenge()
        try:
            self.assertEqual(requests.get(self.url + "/verification.png", timeout=5).status_code, 401)
            response = requests.get(self.url + "/verification.png", headers={"Authorization": "Bearer " + TOKEN, "X-Glados-Job": "wrong"}, timeout=5)
            self.assertEqual(response.status_code, 404)
            response = requests.get(self.url + "/verification.png", headers={"Authorization": "Bearer " + TOKEN, "X-Glados-Job": "correct"}, timeout=5)
            self.assertEqual(response.content, PNG)
        finally:
            with server.TASK_STATE_LOCK:
                server.TASK_STATE.clear()
                server.TASK_STATE.update(running=False)

    def test_lan_and_public_links_are_forwarded_without_secret(self):
        with patch.dict(os.environ, {"GLADOS_BROWSER_PUBLIC_URL": "https://nas.example.test"}, clear=True), \
                patch.object(glados.requests.Session, "get", side_effect=requests.ConnectionError), \
                patch.object(glados, "send_wecom"), patch.object(glados, "send_failure_email") as mail:
            glados.notify_verification(self.url, TOKEN, "job", 1, 250)
            content = mail.call_args.args[1]
            self.assertIn(self.url + "/desktop/", content)
            self.assertIn("https://nas.example.test/desktop/", content)
            self.assertNotIn(TOKEN, content)

    def test_email_attaches_only_the_supplied_widget_png(self):
        env = {"EMAIL_SMTP_HOST": "smtp.invalid", "EMAIL_SMTP_USER": "sender@example.test",
               "EMAIL_SMTP_PASSWORD": "offline-secret", "EMAIL_TO": "recipient@example.test"}
        with patch.dict(os.environ, env, clear=True), patch.object(glados.smtplib, "SMTP_SSL") as factory, \
                patch.object(glados.ssl, "create_default_context"):
            self.assertTrue(glados.send_failure_email("等待验证", "only fixture content", PNG))
            message = factory.return_value.__enter__.return_value.send_message.call_args.args[0]
            attachment = list(message.iter_attachments())[0]
            self.assertEqual(attachment.get_content_type(), "image/png")
            self.assertEqual(attachment.get_payload(decode=True), PNG)

    def test_monitor_never_forwards_another_jobs_challenge(self):
        original_status = server.task_status

        def wrong_status():
            value = original_status()
            value["job_id"] = "anotherjob"
            return value

        def job(_):
            self.write_challenge()
            time.sleep(2.4)
            return {"ok": True}

        with patch.dict(os.environ, self.env, clear=True), patch.object(server, "execute_job", side_effect=job), \
                patch.object(server, "task_status", side_effect=wrong_status), patch.object(glados, "notify_verification") as notify:
            self.assertTrue(glados.browser_service_request("", 1)["ok"])
            notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
