"""One isolated browser job. Never prints cookies, challenge material or page HTML."""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ORIGIN = "https://glados.rocks"
RESULT_MARKER = "GLADOS_RESULT:"

# Observe only the actual official-page POST response. No requests are synthesized.
OBSERVER = r"""(() => {
  window.__gladosCheckinResult = null;
  const record = (url, body) => {
    try {
      const u = new URL(url, location.href);
      if (u.origin !== location.origin || u.pathname !== '/api/user/checkin') return;
      const d = typeof body === 'string' ? JSON.parse(body) : body;
      if (d && typeof d === 'object') {
        window.__gladosCheckinResult = {
          code: d.code, message: String(d.message || '').slice(0, 1000),
          reason: String(d.reason || '').slice(0, 100), repeated: d.repeated === true
        };
      }
    } catch (_) {}
  };
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...args) {
    this.__gladosURL = String(url); this.__gladosMethod = String(method).toUpperCase();
    return open.call(this, method, url, ...args);
  };
  XMLHttpRequest.prototype.send = function(...args) {
    if (this.__gladosMethod === 'POST') this.addEventListener('load', () => {
      record(this.__gladosURL, this.responseType === 'json' ? this.response : this.responseText);
    });
    return send.apply(this, args);
  };
  const originalFetch = window.fetch;
  window.fetch = async function(input, init) {
    const response = await originalFetch.apply(this, arguments);
    const method = String(init?.method || input?.method || 'GET').toUpperCase();
    if (method === 'POST') {
      try { record(response.url, await response.clone().text()); } catch (_) {}
    }
    return response;
  };
})();
"""

STATUS_QUERY = """
const done = arguments[arguments.length - 1];
fetch('/api/user/status', {credentials: 'same-origin'})
  .then(r => r.json()).then(d => done({code: d.code,
    data: d.data ? {email: d.data.email, leftDays: d.data.leftDays} : null}))
  .catch(() => done(null));
"""


def parse_cookies(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 32000:
        raise ValueError("invalid_cookie")
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError("invalid_cookie")
    cookies = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        if not sep or not name or any(c.isspace() for c in name):
            raise ValueError("invalid_cookie")
        cookies[name] = value
    if not cookies:
        raise ValueError("invalid_cookie")
    return cookies


def run_browser(job, progress=None):
    progress = progress if progress is not None else {}
    progress["stage"] = "parse_input"
    selftest = job.get("operation") == "selftest"
    cookies = {}
    if not selftest:
        try:
            cookies = parse_cookies(job.get("cookie"))
        except ValueError:
            return {"ok": False, "error": "invalid_cookie"}
    progress["stage"] = "import_seleniumbase"
    from seleniumbase import SB

    budget = max(60, min(int(job.get("timeout", 180)), 300))
    deadline = time.monotonic() + budget
    options = {
        "uc": True, "headless": False, "locale": "en",
        "xvfb": sys.platform.startswith("linux"), "chromium_arg": "--no-sandbox",
        "driver_version": "keep", "no_screenshot": True,
    }
    if os.environ.get("CHROME_BINARY"):
        options["binary_location"] = os.environ["CHROME_BINARY"]
    if job.get("user_agent"):
        options["agent"] = job["user_agent"]
    with tempfile.TemporaryDirectory(prefix="glados-profile-") as profile:
        options["user_data_dir"] = profile
        progress["stage"] = "start_browser"
        with SB(**options) as sb:
            progress["stage"] = "configure_browser"
            sb.driver.set_page_load_timeout(40)
            sb.driver.set_script_timeout(20)
            if selftest:
                progress["stage"] = "selftest_page"
                sb.open("data:text/html,<html><title>GLaDOS browser self-test</title></html>")
                value = sb.execute_script("return 21 * 2")
                if value != 42:
                    raise RuntimeError("browser_javascript_failed")
                if sys.platform.startswith("linux"):
                    progress["stage"] = "selftest_display"
                    import pyautogui
                    pyautogui.size()
                return {"ok": True, "selftest": True, "stage": "complete"}
            progress["stage"] = "inject_cookies"
            # Set host-scoped cookies before opening the app, so login and
            # challenge requests all run in the same browser, UA and egress.
            sb.driver.execute_cdp_cmd("Network.setCookies", {"cookies": [
                {"name": name, "value": value, "url": ORIGIN + "/", "path": "/", "secure": True}
                for name, value in cookies.items()
            ]})
            progress["stage"] = "open_checkin_page"
            sb.uc_open_with_reconnect(ORIGIN + "/console/checkin", reconnect_time=3)
            # UC opens a new tab. Install observation on that tab before the
            # first click, rather than attaching it to the old blank target.
            sb.execute_script(OBSERVER)
            progress["stage"] = "wait_checkin_page"
            clicked = False
            last_gui_attempt = 0.0
            while time.monotonic() < deadline:
                current = sb.driver.current_url
                if current.startswith(ORIGIN + "/authenticate") or current.startswith(ORIGIN + "/login"):
                    return {"ok": False, "error": "login_required"}
                if not current.startswith(ORIGIN + "/"):
                    return {"ok": False, "error": "login_required"}
                result = sb.execute_script("return window.__gladosCheckinResult || null")
                if result:
                    progress["stage"] = "read_status"
                    state = sb.driver.execute_async_script(STATUS_QUERY)
                    return {"ok": True, "checkin": result, "state": state}
                # Reference project uses UC + a real display for optional
                # Turnstile interaction. Let the official component own its
                # nonce, callback, token and final POST throughout.
                if time.monotonic() - last_gui_attempt >= 5:
                    last_gui_attempt = time.monotonic()
                    try:
                        sb.uc_gui_click_captcha()
                    except Exception:
                        pass
                if not clicked and sb.is_element_visible("button.ckb:not([disabled])"):
                    progress["stage"] = "click_checkin"
                    sb.click("button.ckb:not([disabled])")
                    clicked = True  # Never click again, including on errors.
                    progress["stage"] = "wait_checkin_response"
                time.sleep(0.5)
            return {"ok": False, "error": "verification_required" if clicked else "page_changed"}


def classify_exception(exc):
    """Inspect locally; only emit fixed labels, never raw exceptions or URLs."""
    message = str(exc).lower()
    for markers, label in (
        (("cannot find chrome binary", "chrome not found", "binary is not a chrome executable"), "chrome_missing"),
        (("only supports chrome version", "current browser version", "version of chromedriver"), "driver_mismatch"),
        (("missing x server", "cannot open display", "bad display", "displaynameerror", "xvfb"), "display_error"),
        (("devtoolsactiveport", "chrome failed to start", "chrome instance exited", "chrome not reachable", "cannot connect to chrome"), "chrome_start_failed"),
        (("err_name_not_resolved", "name resolution", "getaddrinfo"), "dns_error"),
        (("err_connection", "err_proxy", "err_tunnel"), "network_error"),
        (("err_cert_", "certificate verify failed"), "certificate_error"),
        (("invalid cookie", "invalid parameters"), "cookie_rejected"),
    ):
        if any(marker in message for marker in markers):
            return label
    return "unclassified"


def main():
    progress = {"stage": "read_input"}
    try:
        job = {"operation": "selftest", "timeout": 60} if "--selftest" in sys.argv else json.loads(sys.stdin.read(65536))
        # Suppress third-party debug output: it may include session details.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = run_browser(job, progress)
    except Exception as exc:
        frames = traceback.extract_tb(exc.__traceback__)
        location = frames[-1] if frames else None
        result = {
            "ok": False, "error": "browser_error", "diagnostic": type(exc).__name__,
            "stage": progress["stage"], "category": classify_exception(exc),
        }
        if location:
            result["diagnostic_file"] = Path(location.filename).name
            result["diagnostic_line"] = location.lineno
    print(RESULT_MARKER + json.dumps(result, ensure_ascii=True), flush=True)
    if "--selftest" in sys.argv and not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
