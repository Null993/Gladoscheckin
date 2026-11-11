"""Authenticated LAN browser executor. One job at a time, bounded lifetime."""
import hmac
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
WORKER = Path(__file__).with_name("worker.py")
MARKER = "GLADOS_RESULT:"
# worker 每进入一个阶段就把名字写到这里；被 SIGKILL 后靠它才知道死在哪一步。
STAGE_FILE = Path(os.environ.get("GLADOS_STAGE_FILE", str(Path(tempfile.gettempdir()) / "glados-stage")))
LOGIN_FINISH = STAGE_FILE.with_name("glados-login-finish")
LOGIN_STATE = {"running": False, "profile_id": None, "result": None}
LOGIN_STATE_LOCK = threading.Lock()
VERSION = "2026-11-11.6"


def begin_login(profile_id):
    if os.environ.get("GLADOS_GUI_ENABLED") != "1":
        return 503, {"ok": False, "error": "gui_not_ready"}
    if not LOCK.acquire(blocking=False):
        return 409, {"ok": False, "error": "busy"}
    LOGIN_FINISH.unlink(missing_ok=True)
    with LOGIN_STATE_LOCK:
        LOGIN_STATE.update(running=True, profile_id=profile_id, result=None)

    def login_job():
        result = {"ok": False, "error": "browser_error"}
        try:
            result = execute_job({
                "operation": "login", "session_mode": "profile", "profile_id": profile_id,
                "timeout": 900, "cookie": "", "user_agent": None, "finish_file": str(LOGIN_FINISH),
            })
        except Exception:
            result = {"ok": False, "error": "browser_error"}
        finally:
            with LOGIN_STATE_LOCK:
                LOGIN_STATE.update(running=False, result=result)
            print("Login job finished: " + ("saved" if result.get("login_saved") else "failed"), flush=True)
            LOCK.release()
    threading.Thread(target=login_job, daemon=True).start()
    return 202, {"ok": True, "profile_id": profile_id}


def read_stage():
    try:
        value = STAGE_FILE.read_text(encoding="ascii").strip()
    except OSError:
        return None
    if value.isascii() and value.replace("_", "").isalnum() and len(value) <= 64:
        return value
    return None


def execute_job(job):
    try:
        STAGE_FILE.unlink()  # 每个任务从干净状态开始，避免读到上一次的残留
    except OSError:
        pass
    process = subprocess.Popen(
        [sys.executable, str(WORKER)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", start_new_session=os.name != "nt",
    )
    try:
        output, _ = process.communicate(json.dumps(job), timeout=job["timeout"])
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        result = {"ok": False, "error": "timeout"}
        stage = read_stage()
        if stage:
            result["stage"] = stage
        return result
    for line in reversed(output.splitlines()):
        if line.startswith(MARKER):
            try:
                value = json.loads(line[len(MARKER):])
                if isinstance(value, dict):
                    return value
            except ValueError:
                break
    return {
        "ok": False, "error": "browser_error", "diagnostic": "WorkerExit",
        "exit_code": process.returncode, "stage": "worker_process",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "GladosBrowser/1"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def authorization_kind(self):
        actual = self.headers.get("Authorization", "")
        if actual.startswith("Bearer "):
            return "bearer" if hmac.compare_digest(actual[7:].encode(), self.server.token.encode()) else None
        if actual.startswith("Basic "):
            try:
                decoded = base64.b64decode(actual[6:], validate=True).decode("utf-8")
                user, _, password = decoded.partition(":")
                if user == "glados" and hmac.compare_digest(password.encode(), self.server.token.encode()):
                    return "basic"
            except (ValueError, UnicodeError):
                pass
        return None

    def require_auth(self, browser=False):
        kind = self.authorization_kind()
        if kind is None:
            self.send_response(401)
            if browser:
                self.send_header("WWW-Authenticate", 'Basic realm="GLaDOS NAS Login"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        if self.command == "POST" and kind == "basic" and self.headers.get("X-Glados-UI") != "1":
            self.reply(403, {"ok": False})
            return False
        return True

    def reply(self, status, value):
        body = json.dumps(value, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"ok": True, "service": "glados-browser", "version": VERSION})
        elif self.path == "/":
            if not self.require_auth(browser=True):
                return
            body = Path(__file__).with_name("login.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/login/status":
            if not self.require_auth():
                return
            with LOGIN_STATE_LOCK:
                state = dict(LOGIN_STATE)
            state.update(ok=True, stage=read_stage() if state["running"] else None)
            self.reply(200, state)
        else:
            self.reply(404, {"ok": False})

    def do_POST(self):
        if self.path not in ("/checkin", "/selftest", "/login/start", "/login/finish"):
            self.reply(404, {"ok": False})
            return
        if not self.require_auth():
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536:
                raise ValueError()
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError()
            job = json.loads(body)
            if not isinstance(job, dict):
                raise ValueError()
            if self.path == "/login/finish":
                with LOGIN_STATE_LOCK:
                    running = LOGIN_STATE["running"]
                if not running:
                    self.reply(409, {"ok": False, "error": "no_login_session"})
                    return
                LOGIN_FINISH.touch(mode=0o600)
                self.reply(202, {"ok": True})
                return
            profile_id = job.get("profile_id", 1)
            if type(profile_id) is not int or not 1 <= profile_id <= 20:
                raise ValueError()
            if self.path == "/login/start":
                status, result = begin_login(profile_id)
                self.reply(status, result)
                return
            selftest = self.path == "/selftest"
            session_mode = job.get("session_mode", "cookie")
            if session_mode not in ("cookie", "profile"):
                raise ValueError()
            if not selftest and session_mode == "cookie" and (not isinstance(job.get("cookie"), str) or not 0 < len(job["cookie"]) <= 32000):
                raise ValueError()
            timeout = job.get("timeout", 180)
            if type(timeout) is not int or not 60 <= timeout <= 600:
                raise ValueError()
            agent = job.get("user_agent")
            if agent is not None and (
                not isinstance(agent, str) or not 0 < len(agent) < 2000
                or any(ord(c) < 32 or ord(c) == 127 for c in agent)
            ):
                raise ValueError()
            job = {
                "cookie": "" if selftest or session_mode == "profile" else job["cookie"],
                "user_agent": agent if session_mode == "cookie" else None,
                "timeout": timeout, "operation": "selftest" if selftest else "checkin",
                "session_mode": "cookie" if selftest else session_mode, "profile_id": profile_id,
            }
        except (ValueError, OSError):
            self.reply(400, {"ok": False, "error": "invalid_request"})
            return
        if not LOCK.acquire(blocking=False):
            self.reply(409, {"ok": False, "error": "busy"})
            return
        try:
            self.connection.settimeout(timeout + 20)
            # 打开始时间，否则只有结束那一行，任务耗时永远算不出来。
            # 只打 timeout 和操作类型，不含 Cookie / UA。
            print("Browser job started: timeout=%d operation=%s" % (timeout, job["operation"]), flush=True)
            result = execute_job(job)
            label = "response_received" if result.get("ok") else "failed"
            # error 也要打出来：login_required / verification_required / page_changed
            # 这些返回没有 stage，过去全渲染成裸的 "failed"，无法区分。
            error = result.get("error")
            if isinstance(error, str) and error.isascii() and error.replace("_", "").isalnum() and len(error) <= 64:
                label += " error=" + error
            for key in ("stage", "diagnostic", "category"):
                value = result.get(key)
                if isinstance(value, str) and value.isascii() and value.replace("_", "").isalnum() and len(value) <= 64:
                    label += " " + key + "=" + value
            if type(result.get("exit_code")) is int:
                label += " exit_code=" + str(result["exit_code"])
            filename, line = result.get("diagnostic_file"), result.get("diagnostic_line")
            if isinstance(filename, str) and filename.isascii() and filename.replace("_", "").replace(".", "").isalnum() and len(filename) <= 64 and type(line) is int:
                label += " location=" + filename + ":" + str(line)
            print("Browser job finished: " + label, flush=True)
            self.reply(200, result)
        except Exception:
            self.reply(500, {"ok": False, "error": "browser_error"})
        finally:
            LOCK.release()


def create_server(address, token):
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("GLADOS_BROWSER_TOKEN must be at least 32 ASCII characters without spaces")
    server = ThreadingHTTPServer(address, Handler)
    server.token = token
    return server


if __name__ == "__main__":
    from worker import ORIGIN
    server = create_server(("0.0.0.0", 8093), os.environ.get("GLADOS_BROWSER_TOKEN", ""))
    # 把实际生效的域名叫出来：域名与 Cookie 来源不一致是最隐蔽的失败原因。
    # 只打域名，不含任何凭据。
    print("GLaDOS browser service %s ready on port 8093 (origin=%s)" % (VERSION, ORIGIN), flush=True)
    server.serve_forever()
