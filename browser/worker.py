"""One isolated browser job. Never prints cookies, challenge material or page HTML."""
import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import time
import traceback
import base64
from pathlib import Path
from urllib.parse import urlsplit

# 会话只用于明确配置的 HTTPS 站点；profile 模式还核对保存时的域名。
def configured_origin():
    value = (os.environ.get("GLADOS_ORIGIN") or "https://glados.rocks").strip().rstrip("/")
    if "://" not in value:
        value = "https://" + value
    url = urlsplit(value)
    if (url.scheme != "https" or url.hostname not in ("glados.rocks", "glados.one", "glados.cloud")
            or url.port not in (None, 443) or url.username or url.password
            or url.path or url.query or url.fragment):
        raise ValueError("GLADOS_ORIGIN must be a supported HTTPS GLaDOS origin")
    return "https://" + url.hostname


ORIGIN = configured_origin()
RESULT_MARKER = "GLADOS_RESULT:"
# 阶段落盘：server.py 在硬超时 SIGKILL 时读这个文件，才知道 worker 死在哪一步。
STAGE_FILE = os.environ.get("GLADOS_STAGE_FILE", str(Path(tempfile.gettempdir()) / "glados-stage"))
PROFILE_ROOT = Path(os.environ.get("GLADOS_PROFILE_ROOT", "/data/profiles"))


def profile_path(profile_id):
    if type(profile_id) is not int or not 1 <= profile_id <= 20:
        raise ValueError("invalid_profile")
    return PROFILE_ROOT / str(profile_id)


def saved_profile_origin(path):
    try:
        return json.loads((path / "session.json").read_text(encoding="utf-8")).get("origin")
    except (OSError, ValueError, AttributeError):
        return None


@contextlib.contextmanager
def running_browser(factory, options, persistent):
    with factory(**options) as browser:
        try:
            yield browser
        finally:
            if persistent:
                # UC.quit() terminates Chrome directly (TerminateProcess on
                # Windows). Ask Chrome to flush its own profile first instead.
                try:
                    browser.driver.execute_cdp_cmd("Browser.close", {})
                except Exception:
                    pass  # Closing the browser may also close the CDP socket.
                pid = getattr(browser.driver, "browser_pid", None)
                if type(pid) is int:
                    try:
                        import psutil
                        psutil.Process(pid).wait(timeout=10)
                    except Exception:
                        pass


@contextlib.contextmanager
def browser_profile(job):
    if job.get("operation") != "selftest" and (job.get("session_mode") == "profile" or job.get("operation") == "login"):
        path = profile_path(job.get("profile_id", 1))
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        yield str(path)
    else:
        with tempfile.TemporaryDirectory(prefix="glados-profile-") as path:
            yield path

# 减少 Chromium 无关后台联网。NAS 约 136 秒的启动耗时在添加这些参数后
# 仍存在，尚不能据此归因于组件更新或认定已解决。
# SeleniumBase 的 chromium_arg 支持逗号分隔多个参数。
CHROME_ARGS = ",".join([
    "--no-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-client-side-phishing-detection",
    "--metrics-recording-only",
    # 注意：不能写成 --disable-features=A,B —— SeleniumBase 用逗号分隔参数，
    # 值里带逗号会被拆成多个非法参数。只保留单个特性。
    "--disable-features=Translate",
])


def mark(progress, stage):
    """记录当前阶段。落盘是唯一能在 SIGKILL 后存活的通道。"""
    progress["stage"] = stage
    try:
        Path(STAGE_FILE).write_text(stage, encoding="ascii")
    except OSError:
        pass


class StageTimeout(Exception):
    """某个阻塞的浏览器调用超时。由 SIGALRM 打断，仅在 Linux 容器内生效。"""


def _raise_stage_timeout(signum, frame):
    raise StageTimeout()


@contextlib.contextmanager
def watchdog(seconds):
    """给一段阻塞调用加硬超时。Selenium 的调用无法被 socket 超时打断，
    但 SIGALRM 可以，它会在调用返回前把异常抛进主线程。"""
    if not hasattr(signal, "SIGALRM"):  # Windows 上不存在，直接放行
        yield
        return
    previous = signal.signal(signal.SIGALRM, _raise_stage_timeout)
    signal.setitimer(signal.ITIMER_REAL, max(5, int(seconds)))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# Observe only the actual official-page POST response. No requests are synthesized.
OBSERVER = r"""(() => {
  if (window.__gladosObserverInstalled) return;
  window.__gladosObserverInstalled = true;
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
          ,loginDevice: String(d.loginDevice || '').slice(0, 40), currentDevice: String(d.currentDevice || '').slice(0, 40)
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

# 判断并点击签到按钮。返回 'clicked' / 'hidden' / 'missing'。
# 不用 sb.is_element_visible + sb.click：前者会把带 :not([disabled]) 的 CSS 选择器
# 误判成链接文本而永远返回 False；后者依赖真实鼠标位置，被遮罩挡住就失败。
CLICK_JS = r"""
const pick = () => {
  const direct = document.querySelector('button.ckb:not([disabled])');
  if (direct) return direct;
  return Array.from(document.querySelectorAll('button'))
    .find(b => !b.disabled && /^\s*check\s*-?\s*in\s*$/i.test((b.innerText || '').trim())) || null;
};
const btn = pick();
if (!btn) return 'missing';
const rect = btn.getBoundingClientRect();
const style = window.getComputedStyle(btn);
if (!(rect.width > 0 && rect.height > 0) || style.display === 'none' || style.visibility === 'hidden') {
  return 'hidden';
}
try { btn.scrollIntoView({ block: 'center' }); } catch (_) {}
btn.click();
return 'clicked';
"""

STATUS_QUERY = """
const done = arguments[arguments.length - 1];
fetch('/api/user/status', {credentials: 'same-origin'})
  .then(r => r.json()).then(d => done({code: d.code,
    data: d.data ? {email: d.data.email, leftDays: d.data.leftDays} : null}))
  .catch(() => done(null));
"""

# Return booleans only. Never return, log, persist or replay the provider token.
TURNSTILE_STATE_JS = r"""
const inputs = Array.from(document.querySelectorAll('input[name="cf-turnstile-response"]'));
let response = inputs.some(e => !!e.value), widget = inputs.length > 0;
try {
  if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
    const r = window.turnstile.getResponse(); widget = true; response = !!r;
    if (typeof window.turnstile.isExpired === 'function' && window.turnstile.isExpired()) response = false;
  }
} catch (_) {}
const resources = performance.getEntriesByType('resource').some(e => {
  try { return new URL(e.name).hostname === 'challenges.cloudflare.com'; } catch (_) { return false; }
});
return {state: response ? 'ready' : widget ? 'pending' : 'absent', resources};
"""


def verification_file(suffix):
    return Path(STAGE_FILE).with_name("glados-verification." + suffix)


def clear_verification():
    for suffix in ("json", "png"):
        verification_file(suffix).unlink(missing_ok=True)


def find_turnstile_frame(driver):
    """Locate a visible provider iframe, including inside closed shadow roots."""
    driver.execute_cdp_cmd("DOM.enable", {})
    root = driver.execute_cdp_cmd("DOM.getDocument", {"depth": -1, "pierce": True})["root"]
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.get("children", []))
        stack.extend(node.get("shadowRoots", []))
        # Do not descend into nested documents: their rectangles use a
        # different coordinate system. Closed shadow roots share this page.
        if node.get("nodeName", "").upper() != "IFRAME":
            continue
        attrs = node.get("attributes", [])
        attrs = dict(zip(attrs[::2], attrs[1::2]))
        url = urlsplit(attrs.get("src", ""))
        if url.scheme != "https" or url.hostname != "challenges.cloudflare.com":
            continue
        if "/turnstile/" not in url.path and "/challenge-platform/" not in url.path:
            continue
        obj = driver.execute_cdp_cmd("DOM.resolveNode", {"backendNodeId": node["backendNodeId"]})["object"]["objectId"]
        try:
            box = driver.execute_cdp_cmd("Runtime.callFunctionOn", {
                "objectId": obj, "returnByValue": True,
                "functionDeclaration": "function(){this.scrollIntoView({block:'center'});const r=this.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};}",
            }).get("result", {}).get("value", {})
        finally:
            driver.execute_cdp_cmd("Runtime.releaseObject", {"objectId": obj})
        # Only the compact checkbox widget, not a full-screen/puzzle challenge.
        if 150 <= box.get("width", 0) <= 800 and 40 <= box.get("height", 0) <= 200:
            return box
    return None


def capture_verification(driver, box):
    if not box:
        return
    scroll = driver.execute_script("return {x:scrollX,y:scrollY}")
    result = driver.execute_cdp_cmd("Page.captureScreenshot", {
        "format": "png", "captureBeyondViewport": False,
        "clip": {"x": box["x"] + scroll["x"], "y": box["y"] + scroll["y"],
                 "width": box["width"], "height": box["height"], "scale": 1},
    })
    data = base64.b64decode(result["data"])
    if len(data) > 1000000 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return
    path = verification_file("png")
    path.write_bytes(data)
    path.chmod(0o600)


def click_turnstile_checkbox(browser, box):
    import pyautogui
    driver = browser.driver
    driver.execute_cdp_cmd("Page.bringToFront", {})
    metrics = driver.execute_script("return {x:screenX,y:screenY,w:outerWidth-innerWidth,h:outerHeight-innerHeight,ratio:devicePixelRatio,iw:innerWidth,ih:innerHeight}")
    # NAS Xvfb uses unscaled pixels. Avoid guessing when scaling differs.
    if abs(metrics["ratio"] - 1) > 0.05:
        return False
    x, y = box["x"] + 30, box["y"] + min(32, box["height"] / 2)
    if not (0 <= x < metrics["iw"] and 0 <= y < metrics["ih"]):
        return False
    border = max(0, metrics["w"] / 2)
    pyautogui.click(metrics["x"] + border + x, metrics["y"] + metrics["h"] - border + y)
    return True


class TurnstileController:
    def __init__(self, manual_wait=300, auto_wait=35, deadline=None):
        self.manual_wait = manual_wait
        self.auto_wait = auto_wait
        self.since = None
        self.manual_since = None
        self.last_attempt = None
        self.attempts = 0
        self.deadline = deadline

    def tick(self, browser, progress):
        now = time.monotonic()
        state = browser.execute_script(TURNSTILE_STATE_JS) or {}
        if state.get("state") == "ready":
            if self.since is not None:
                mark(progress, "verification_ready")
            self.since = self.manual_since = None
            clear_verification()
            return "ready"
        box = None
        if state.get("state") == "pending" or state.get("resources"):
            try:
                box = find_turnstile_frame(browser.driver)
            except Exception:
                pass
        if state.get("state") != "pending" and box is None:
            if self.manual_since is not None:
                clear_verification()
            self.since = self.manual_since = None
            return "absent"
        if self.since is None:
            self.since = now
        if self.manual_since is not None:
            mark(progress, "manual_verification")
            return "expired" if now - self.manual_since >= self.manual_wait else "manual"
        mark(progress, "verification_wait")
        if (box and now - self.since >= 5 and self.attempts < 3
                and (self.last_attempt is None or now - self.last_attempt >= 8)):
            self.attempts += 1
            self.last_attempt = now
            mark(progress, "verification_click")
            try:
                click_turnstile_checkbox(browser, box)
            except Exception:
                pass
        exhausted = self.attempts >= 3 and now - self.last_attempt >= 8
        if now - self.since >= self.auto_wait or exhausted:
            self.manual_since = now
            if self.deadline is not None:
                self.manual_wait = min(self.manual_wait, max(0, int(self.deadline - now)))
            try:
                capture_verification(browser.driver, box)
            except Exception:
                pass
            data = {"waiting": True, "since": time.time(), "wait_seconds": self.manual_wait}
            path = verification_file("json")
            path.write_text(json.dumps(data), encoding="ascii")
            path.chmod(0o600)
            mark(progress, "manual_verification")
            return "manual" if self.manual_wait else "expired"
        return "pending"


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
    mark(progress, "parse_input")
    selftest = job.get("operation") == "selftest"
    login = job.get("operation") == "login"
    persistent = not selftest and (login or job.get("session_mode") == "profile")
    cookies = {}
    if not selftest and not persistent:
        try:
            cookies = parse_cookies(job.get("cookie"))
        except ValueError:
            return {"ok": False, "error": "invalid_cookie", "stage": progress["stage"]}
    if persistent and not login:
        path = profile_path(job.get("profile_id", 1))
        origin = saved_profile_origin(path)
        if origin is None:
            return {"ok": False, "error": "profile_not_ready", "stage": progress["stage"]}
        if origin != ORIGIN:
            return {"ok": False, "error": "profile_origin_mismatch", "stage": progress["stage"]}
    if sys.platform.startswith("linux"):
        mark(progress, "import_tkinter")
        import tkinter  # Verify the same interpreter before MouseInfo can exit.
    mark(progress, "import_seleniumbase")
    from seleniumbase import SB

    # 内部预算必须比 server.py 的硬杀超时小一截，否则循环到点退出后还要执行
    # SB 的 teardown（关 Chrome 需数秒），期间 worker 已被 SIGKILL，
    # 真实 stage 永远回不到日志里，只剩一行裸的 "failed"。
    requested = int(job.get("timeout", 180))
    budget = max(30, min(requested - 25, 875 if login else 1175))
    deadline = time.monotonic() + budget
    verification = TurnstileController(manual_wait=job.get("manual_wait", 300), deadline=deadline)
    options = {
        "uc": True, "headless": False, "headed": True, "locale": "en",
        "xvfb": sys.platform.startswith("linux") and not os.environ.get("DISPLAY"), "chromium_arg": CHROME_ARGS,
        "driver_version": "keep", "no_screenshot": True,
        "window_size": "1280,850",
    }
    if os.environ.get("CHROME_BINARY"):
        options["binary_location"] = os.environ["CHROME_BINARY"]
    if job.get("user_agent") and not persistent:
        options["agent"] = job["user_agent"]
    # Chrome must receive its proxy explicitly. HTTP_PROXY environment variables
    # alone are not a reliable Chromium proxy configuration.
    proxy = os.environ.get("GLADOS_BROWSER_PROXY", "").strip()
    if proxy:
        options["proxy"] = proxy
    with browser_profile(job) as profile:
        options["user_data_dir"] = profile
        mark(progress, "start_browser")
        with running_browser(SB, options, persistent) as sb:
            mark(progress, "configure_browser")
            sb.driver.set_page_load_timeout(40)
            sb.driver.set_script_timeout(20)
            if not selftest and time.monotonic() >= deadline:
                return {"ok": False, "error": "timeout", "stage": progress["stage"]}
            if selftest:
                mark(progress, "selftest_page")
                sb.open("data:text/html,<html><title>GLaDOS browser self-test</title></html>")
                value = sb.execute_script("return 21 * 2")
                if value != 42:
                    raise RuntimeError("browser_javascript_failed")
                if sys.platform.startswith("linux"):
                    mark(progress, "selftest_display")
                    import pyautogui
                    pyautogui.size()
                return {"ok": True, "selftest": True, "stage": "complete"}
            mark(progress, "inject_cookies" if not persistent else "load_saved_session")
            # Set host-scoped cookies before opening the app, so login and
            # challenge requests all run in the same browser, UA and egress.
            if cookies:
                sb.driver.execute_cdp_cmd("Network.setCookies", {"cookies": [
                    {"name": name, "value": value, "url": ORIGIN + "/", "path": "/", "secure": True}
                    for name, value in cookies.items()
                ]})
            mark(progress, "open_checkin_page")
            page_url = ORIGIN + "/console/checkin"
            # uc_open_with_reconnect = uc_open + reconnect。拆成两步并各自打点，
            # 才能知道是"打开页面"卡住还是"CDP 重连"卡住。
            # watchdog 把这一步的等待上限压到 open_budget，超时立刻带 stage 报错，
            # 不必再空等满 timeout。
            open_budget = max(30, min(budget - 30, 90))
            with watchdog(open_budget):
                if hasattr(sb, "uc_open") and hasattr(sb, "reconnect"):
                    mark(progress, "open_checkin_page_uc_open")
                    sb.uc_open(page_url)
                    mark(progress, "open_checkin_page_reconnect")
                    sb.reconnect(3)
                else:  # 旧版 SeleniumBase 没有拆开的接口
                    sb.uc_open_with_reconnect(page_url, reconnect_time=3)
            mark(progress, "open_checkin_page_done")
            if login:
                # The user logs in through noVNC. Never inject desktop cookies,
                # submit credentials, click check-in, or export browser cookies.
                mark(progress, "manual_login")
                finish = Path(job["finish_file"])
                while time.monotonic() < deadline:
                    if urlsplit(sb.driver.current_url).netloc == urlsplit(ORIGIN).netloc:
                        if verification.tick(sb, progress) in ("absent", "ready"):
                            mark(progress, "manual_login")
                    if finish.exists():
                        finish.unlink(missing_ok=True)
                        if urlsplit(sb.driver.current_url).netloc != urlsplit(ORIGIN).netloc:
                            continue
                        state = sb.driver.execute_async_script(STATUS_QUERY)
                        if not isinstance(state, dict) or state.get("code") != 0 or not isinstance(state.get("data"), dict) or "leftDays" not in state["data"]:
                            mark(progress, "manual_login_incomplete")
                            continue
                        marker = Path(profile) / "session.json"
                        marker.write_text(json.dumps({"origin": ORIGIN}), encoding="utf-8")
                        marker.chmod(0o600)
                        mark(progress, "login_saved")
                        return {"ok": True, "login_saved": True, "profile_id": job["profile_id"], "stage": "login_saved"}
                    time.sleep(0.5)
                return {"ok": False, "error": "login_timeout", "stage": progress["stage"]}
            # UC opens a new tab. Install observation on that tab before the
            # first click, rather than attaching it to the old blank target.
            sb.execute_script(OBSERVER)
            mark(progress, "wait_checkin_page")
            clicked = False
            while time.monotonic() < deadline:
                current = sb.driver.current_url
                if current.startswith(ORIGIN + "/authenticate") or current.startswith(ORIGIN + "/login"):
                    mark(progress, "login_redirect")
                    return {"ok": False, "error": "login_required", "stage": progress["stage"]}
                if not current.startswith(ORIGIN + "/"):
                    mark(progress, "offsite_redirect")
                    return {"ok": False, "error": "login_required", "stage": progress["stage"]}
                # A top-level challenge may reload the document after solving.
                # Reinstall idempotently; never erase an already captured reply.
                sb.execute_script(OBSERVER)
                result = sb.execute_script("return window.__gladosCheckinResult || null")
                if result:
                    mark(progress, "read_status")
                    state = sb.driver.execute_async_script(STATUS_QUERY)
                    return {"ok": True, "checkin": result, "state": state, "stage": progress["stage"]}
                verification_state = verification.tick(sb, progress)
                if verification_state == "expired":
                    return {"ok": False, "error": "verification_required", "stage": progress["stage"]}
                if not clicked and verification_state in ("absent", "ready"):
                    # 不用 sb.is_element_visible / sb.click：
                    # 带 :not([disabled]) 伪类的 CSS 选择器会被 SeleniumBase 误判成
                    # 链接文本，导致永远返回 False（实测按钮可点却一次没点到）；
                    # 原生点击还可能被遮罩挡住。改成在页面里直接判断并 click()。
                    state = sb.execute_script(CLICK_JS)
                    if state == "clicked":
                        mark(progress, "click_checkin")
                        clicked = True  # Never click again, including on errors.
                        mark(progress, "wait_checkin_response")
                time.sleep(0.5)
            # 循环结束仍未点到按钮时，区分三种原因——它们的修法完全不同：
            #   button_not_found  页面改版，选择器失效
            #   button_disabled   按钮存在但禁用（今日已签 / 挑战未过）
            #   page_changed      按钮存在且可点，但一直没点到（罕见）
            if not clicked:
                try:
                    ui = sb.execute_script(
                        "const b = document.querySelector('button.ckb');"
                        "return {has: !!b, disabled: b ? !!b.disabled : null};"
                    ) or {}
                except Exception:
                    ui = {}
                if not ui.get("has"):
                    return {"ok": False, "error": "button_not_found", "stage": progress["stage"]}
                if ui.get("disabled"):
                    return {"ok": False, "error": "button_disabled", "stage": progress["stage"]}
            return {"ok": False, "error": "checkin_unknown" if clicked else "page_changed",
                    "stage": progress["stage"]}


def classify_exception(exc):
    """Inspect locally; only emit fixed labels, never raw exceptions or URLs."""
    message = str(exc).lower()
    for markers, label in (
        (("libtk8.6", "libtcl8.6", "_tkinter", "tkinter"), "tkinter_missing"),
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
    except (Exception, SystemExit) as exc:
        frames = traceback.extract_tb(exc.__traceback__)
        location = frames[-1] if frames else None
        result = {
            "ok": False, "error": "browser_error", "diagnostic": type(exc).__name__,
            "stage": progress["stage"],
            "category": "stage_timeout" if isinstance(exc, StageTimeout) else classify_exception(exc),
        }
        if location:
            result["diagnostic_file"] = Path(location.filename).name
            result["diagnostic_line"] = location.lineno
    print(RESULT_MARKER + json.dumps(result, ensure_ascii=True), flush=True)
    if "--selftest" in sys.argv and not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
