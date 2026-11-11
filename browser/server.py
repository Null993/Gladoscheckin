"""Authenticated LAN browser executor. One job at a time, bounded lifetime."""
import hmac
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
WORKER = Path(__file__).with_name("worker.py")
MARKER = "GLADOS_RESULT:"


def execute_job(job):
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
        return {"ok": False, "error": "timeout"}
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
        if self.path in ("/", "/health"):
            self.reply(200, {"ok": True, "service": "glados-browser", "version": "2026-11-11.5"})
        else:
            self.reply(404, {"ok": False})

    def do_POST(self):
        if self.path not in ("/checkin", "/selftest"):
            self.reply(404, {"ok": False})
            return
        expected = "Bearer " + self.server.token
        actual = self.headers.get("Authorization", "")
        if not hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8")):
            self.reply(401, {"ok": False})
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
            selftest = self.path == "/selftest"
            if not selftest and (not isinstance(job.get("cookie"), str) or not 0 < len(job["cookie"]) <= 32000):
                raise ValueError()
            timeout = job.get("timeout", 180)
            if type(timeout) is not int or not 60 <= timeout <= 300:
                raise ValueError()
            agent = job.get("user_agent")
            if agent is not None and (
                not isinstance(agent, str) or not 0 < len(agent) < 2000
                or any(ord(c) < 32 or ord(c) == 127 for c in agent)
            ):
                raise ValueError()
            job = {
                "cookie": "" if selftest else job["cookie"], "user_agent": agent,
                "timeout": timeout, "operation": "selftest" if selftest else "checkin",
            }
        except (ValueError, OSError):
            self.reply(400, {"ok": False, "error": "invalid_request"})
            return
        if not LOCK.acquire(blocking=False):
            self.reply(409, {"ok": False, "error": "busy"})
            return
        try:
            self.connection.settimeout(timeout + 20)
            result = execute_job(job)
            label = "response_received" if result.get("ok") else "failed"
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
    server = create_server(("0.0.0.0", 8093), os.environ.get("GLADOS_BROWSER_TOKEN", ""))
    print("GLaDOS browser service 2026-11-11.5 ready on port 8093", flush=True)
    server.serve_forever()
