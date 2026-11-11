"""离线模拟官网协议，不读取真实 Cookie，不发起网络或通知。"""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    "qinglong_glados", Path(__file__).resolve().parents[1] / "qinglong" / "glados.py"
)
glados = importlib.util.module_from_spec(spec)
spec.loader.exec_module(glados)


def response(payload=None, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    if status >= 400:
        result.raise_for_status.side_effect = glados.requests.HTTPError()
    return result


def challenge(nonce="test-nonce", **values):
    return response(dict(code=0, nonce=nonce, **values))


class CheckinTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(glados.time, "sleep").start()
        self.clock = patch.object(glados.time, "monotonic", return_value=100).start()
        patch("sys.stdout", new_callable=io.StringIO).start()
        self.addCleanup(patch.stopall)
        self.session = Mock()

    def run_checkin(self, retries=1, service_retries=1):
        return glados.checkin_with_service_retry(
            self.session, {"cookie": "fake-cookie"}, 1, retries, 30, service_retries, 30
        )

    def test_protocol_payload_wait_and_origin(self):
        def request(method, url, **kwargs):
            if method == "GET":
                return challenge()
            self.sleep.assert_called_once_with(2.7)
            return response({"code": 0})
        self.session.request.side_effect = request
        self.run_checkin()
        get_call, post_call = self.session.request.call_args_list
        self.assertEqual(get_call.args, ("GET", glados.CHALLENGE_URL))
        self.assertEqual(post_call.args, ("POST", glados.CHECKIN_URL))
        self.assertEqual(post_call.kwargs["json"], {
            "token": "glados.rocks", "nonce": "test-nonce",
            "proof": hashlib.sha256(b"test-nonce.glados-observe-v1").hexdigest(),
        })
        self.assertEqual(get_call.kwargs["headers"], post_call.kwargs["headers"])
        self.assertFalse(post_call.kwargs["allow_redirects"])
        self.sleep.assert_called_once_with(2.7)

    def test_invalid_challenge_never_posts(self):
        invalid = [
            {"code": -2}, {"code": 0}, {"code": 0, "nonce": ""},
            {"code": 0, "nonce": 123}, {"code": 0, "nonce": "x" * 4097},
        ] + [dict(code=0, nonce="x", ttl=ttl) for ttl in (
            0, -1, 2700, True, "300000", None, float("nan"), float("inf"),
        )]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.session.reset_mock(side_effect=True)
                self.session.request.return_value = response(payload)
                with self.assertRaises(RuntimeError):
                    self.run_checkin()
                self.assertEqual(self.session.request.call_count, 1)

    def test_expired_challenge_never_posts(self):
        self.session.request.return_value = challenge(ttl=3000)
        self.clock.side_effect = [100, 103]
        with self.assertRaisesRegex(glados.RetryableRequestError, "已过期"):
            self.run_checkin(retries=0)
        self.assertEqual(self.session.request.call_count, 1)

    def test_post_network_errors_get_new_nonce(self):
        failures = [
            glados.requests.Timeout("private details"), response(status=502),
            response(status=429), response(["invalid JSON shape"]),
        ]
        bad_json = response()
        bad_json.json.side_effect = ValueError("private response")
        failures.append(bad_json)
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.session.reset_mock(side_effect=True)
                self.session.request.side_effect = [
                    challenge("first"), failure, challenge("second"), response({"code": 0})
                ]
                self.run_checkin()
                calls = self.session.request.call_args_list
                self.assertEqual([c.args[0] for c in calls], ["GET", "POST", "GET", "POST"])
                self.assertEqual(calls[3].kwargs["json"]["nonce"], "second")

    def test_challenge_network_retry_is_bounded(self):
        self.session.request.side_effect = glados.requests.Timeout()
        with self.assertRaises(glados.RetryableRequestError):
            self.run_checkin(retries=2)
        self.assertEqual(self.session.request.call_count, 3)

    def test_post_retry_exhaustion_reports_failure_without_secrets(self):
        self.session.request.side_effect = [
            challenge("private-nonce"), glados.requests.Timeout("private-cookie"),
            challenge("private-nonce-2"), glados.requests.Timeout("private-cookie"),
        ]
        with self.assertRaises(glados.RetryableRequestError) as caught:
            self.run_checkin()
        self.assertEqual(self.session.request.call_count, 4)
        self.assertNotIn("private-", str(caught.exception))
        self.assertNotIn("private-", glados.sys.stdout.getvalue())

    def test_http_auth_rejection_or_redirect_never_replays(self):
        for status in (301, 302, 307, 308, 401, 403):
            with self.subTest(status=status):
                self.session.reset_mock(side_effect=True)
                self.session.request.side_effect = [challenge(), response(status=status)]
                with self.assertRaises(RuntimeError):
                    self.run_checkin()
                self.assertEqual(self.session.request.call_count, 2)

    def test_service_retry_also_gets_new_nonce(self):
        self.session.request.side_effect = [
            challenge("first"), response({"code": 1, "message": "Submarine cable data not ready. Please try again in a moment."}),
            challenge("second"), response({"code": 0, "message": "Checkin! Got 12 Points"}),
        ]
        self.run_checkin()
        self.assertEqual(self.session.request.call_args_list[3].kwargs["json"]["nonce"], "second")
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2.7, 30, 2.7])

    def test_auth_message_overrides_success_or_retry_markers(self):
        result = {"code": -2, "message": "already checked in; permission denied; try again later"}
        self.session.request.side_effect = [challenge(), response(result)]
        self.assertEqual(self.run_checkin(), result)
        self.assertFalse(glados.is_successful_checkin(result))
        self.assertEqual(self.session.request.call_count, 2)

    def test_new_observation_duplicate_response_is_success(self):
        self.assertTrue(glados.is_successful_checkin({
            "code": 1, "message": "Today's observation logged",
        }))

    def test_one_point_success_does_not_send_failure_email(self):
        with patch.dict(os.environ, {"GLADOS_COOKIE": "fake-cookie", "WECOM_WEBHOOK": ""}, clear=True), \
                patch.object(glados.requests, "Session") as factory, \
                patch.object(glados, "send_failure_email") as email, \
                patch.object(glados, "send_wecom"):
            factory.return_value.__enter__.return_value = self.session
            self.session.request.side_effect = [
                challenge(), response({"code": 0, "message": "Checkin! Got 1 Points"}),
                response({"code": 0, "data": {"email": "test@example.com", "leftDays": 123.5}}),
            ]
            self.assertEqual(glados.main(), 0)
            email.assert_not_called()

    def test_multi_account_failure_does_not_block_next(self):
        with patch.dict(os.environ, {"GLADOS_COOKIE": "first&second"}, clear=True), \
                patch.object(glados, "checkin_account", side_effect=[RuntimeError("验证失败"), "签到成功"]) as account, \
                patch.object(glados, "send_failure_email") as email, \
                patch.object(glados, "send_wecom"):
            self.assertEqual(glados.main(), 1)
            self.assertEqual(account.call_count, 2)
            email.assert_called_once()

    def test_user_agent_configuration(self):
        for value, expected in (
            ("", glados.DEFAULT_USER_AGENT),
            ('User-Agent: "Mozilla/5.0 test"', "Mozilla/5.0 test"),
        ):
            with patch.dict(os.environ, {"GLADOS_USER_AGENT": value}):
                self.assertEqual(glados.browser_user_agent(), expected)
        for value in ("first\nsecond", "中文", "User-Agent:", "bad\x01header"):
            with patch.dict(os.environ, {"GLADOS_USER_AGENT": value}):
                with self.assertRaises(ValueError):
                    glados.browser_user_agent()


if __name__ == "__main__":
    unittest.main()
