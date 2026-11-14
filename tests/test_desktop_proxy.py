import base64
import hashlib
import importlib.util
from pathlib import Path
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

spec = importlib.util.spec_from_file_location("desktop_server", Path(__file__).resolve().parents[1] / "browser/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
TOKEN = "desktop-proxy-test-" + "x" * 32


class DesktopProxyTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        seen = self.seen

        class Backend(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append(dict(self.headers))
                if self.headers.get("Upgrade") == "websocket":
                    key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                    accept = base64.b64encode(hashlib.sha1(key.encode()).digest()).decode()
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    self.rfile.read(6)  # Masked empty client text frame.
                    self.wfile.write(b"\x81\x00")
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    body = b"<html>local noVNC fixture</html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        self.api = server.create_server(("127.0.0.1", 0), TOKEN)
        self.threads = []
        for http in (self.backend, self.api):
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.proxy = patch.object(server, "DESKTOP_BACKEND", ("127.0.0.1", self.backend.server_port))
        self.proxy.start()
        self.url = "http://127.0.0.1:" + str(self.api.server_port)

    def tearDown(self):
        self.proxy.stop()
        for http in (self.api, self.backend):
            http.shutdown()
            http.server_close()
        for thread in self.threads:
            thread.join(timeout=5)

    def test_static_desktop_requires_auth_and_strips_api_credentials(self):
        self.assertEqual(requests.get(self.url + "/desktop/vnc.html", timeout=5).status_code, 401)
        response = requests.get(self.url + "/desktop/vnc.html", headers={"Authorization": "Bearer " + TOKEN}, timeout=5)
        self.assertIn("local noVNC", response.text)
        self.assertNotIn("Authorization", self.seen[0])

    def websocket(self, origin, forwarded_host=None):
        connection = socket.create_connection(("127.0.0.1", self.api.server_port), timeout=5)
        headers = ["GET /desktop/websockify HTTP/1.1", "Host: 127.0.0.1:" + str(self.api.server_port),
                   "Authorization: Bearer " + TOKEN, "Connection: Upgrade", "Upgrade: websocket",
                   "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Origin: " + origin]
        if forwarded_host:
            headers.append("X-Forwarded-Host: " + forwarded_host)
        connection.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            data += connection.recv(4096)
        return connection, data

    def test_https_gateway_websocket_forwards_binary_without_api_password(self):
        connection, headers = self.websocket("https://nas.example.test", "nas.example.test")
        try:
            self.assertIn(b"101 Switching Protocols", headers)
            connection.sendall(b"\x81\x80\x11\x22\x33\x44")
            self.assertEqual(connection.recv(2), b"\x81\x00")
            self.assertNotIn("Authorization", self.seen[0])
        finally:
            connection.close()

    def test_cross_origin_websocket_is_rejected(self):
        connection, headers = self.websocket("https://untrusted.example.test")
        try:
            self.assertIn(b"403 Forbidden", headers)
            self.assertEqual(self.seen, [])
        finally:
            connection.close()
