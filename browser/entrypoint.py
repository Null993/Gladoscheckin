"""Supervise one shared X display, password-protected noVNC, and the API."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def validate_vnc_password(value):
    # Classic VNC authentication supports only the first eight bytes. Require
    # exactly eight instead of silently truncating a longer shared API token.
    if len(value) != 8 or not value.isascii() or not value.isalnum():
        raise ValueError("GLADOS_VNC_PASSWORD must contain exactly 8 ASCII letters/digits")
    return value


def main():
    secret = validate_vnc_password(os.environ.get("GLADOS_VNC_PASSWORD", ""))
    token = os.environ.get("GLADOS_BROWSER_TOKEN", "")
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("GLADOS_BROWSER_TOKEN must contain at least 32 ASCII characters without spaces")
    passwd = Path("/run/glados-vnc.pass")
    subprocess.run(["x11vnc", "-storepasswd", secret, str(passwd)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    passwd.chmod(0o600)
    os.environ.update(DISPLAY=":99", GLADOS_GUI_ENABLED="1")
    children = []

    def launch(args, quiet=True):
        p = subprocess.Popen(args, stdout=subprocess.DEVNULL if quiet else None,
                             stderr=subprocess.DEVNULL if quiet else None)
        children.append(p)
        return p

    def stop(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        display = launch(["Xvfb", ":99", "-screen", "0", "1360x900x24", "-nolisten", "tcp", "-ac"])
        for _ in range(50):
            if display.poll() is not None:
                raise RuntimeError("Xvfb startup failed")
            check = subprocess.run(["xdpyinfo", "-display", ":99"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=3)
            if check.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Xvfb did not become ready")
        launch(["openbox", "--sm-disable"])
        launch(["x11vnc", "-display", ":99", "-localhost", "-rfbauth", str(passwd),
                "-forever", "-shared", "-noxdamage"])
        launch(["websockify", "--web=/usr/share/novnc", "0.0.0.0:8094", "127.0.0.1:5900"])
        launch([sys.executable, "/app/server.py"], quiet=False)
        print("NAS login desktop ready on port 8094 (VNC password required)", flush=True)
        while True:
            if any(p.poll() is not None for p in children):
                raise RuntimeError("A browser service component exited")
            time.sleep(1)
    finally:
        for p in reversed(children):
            if p.poll() is None:
                p.terminate()
        for p in reversed(children):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
