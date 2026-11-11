#!/usr/bin/env python3
"""
pihole-sync.ertechlab.com — K3s Web Application Frontend & API Gateway
======================================================================
Serves the Pi-hole & Nebula-Sync Control Center UI, /ads.txt, /healthz,
and proxies /api/* calls to the PVE & Nebula-Sync Control Agent at 192.168.0.30:8765.
"""

import http.server
import json
import os
import socketserver
import urllib.error
import urllib.request

PORT = int(os.environ.get("PORT", 8080))
AGENT_URL = os.environ.get("AGENT_URL", "http://192.168.0.30:8765")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
ADS_TXT = "google.com, pub-1596056523034714, DIRECT, f08c47fec0942fa0\n"


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WebHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_to_agent(self, method: str):
        length = int(self.headers.get("Content-Length", 0))
        req_body = self.rfile.read(length) if length > 0 else None
        headers = {"Content-Type": "application/json"} if req_body is not None else {}
        target = f"{AGENT_URL}{self.path}"
        try:
            req = urllib.request.Request(target, data=req_body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=65) as resp:
                data = resp.read()
                self._send(resp.status, data, "application/json; charset=utf-8")
        except urllib.error.HTTPError as e:
            err_data = e.read()
            self._send(e.code, err_data, "application/json; charset=utf-8")
        except Exception as e:
            err_json = json.dumps({"ok": False, "error": f"Agent connection error: {e}"}).encode("utf-8")
            self._send(502, err_json, "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/ads.txt":
            self._send(200, ADS_TXT.encode("utf-8"), "text/plain; charset=utf-8")
        elif path == "/healthz":
            self._send(200, b'{"status":"ok","service":"pihole-sync"}', "application/json; charset=utf-8")
        elif path.startswith("/api/"):
            self._proxy_to_agent("GET")
        elif path in ("/", "/index.html"):
            index_path = os.path.join(STATIC_DIR, "index.html")
            with open(index_path, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            file_path = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
            if file_path.startswith(STATIC_DIR) and os.path.isfile(file_path):
                ctype = "text/plain"
                if file_path.endswith(".css"):
                    ctype = "text/css"
                elif file_path.endswith(".js"):
                    ctype = "application/javascript"
                elif file_path.endswith(".svg"):
                    ctype = "image/svg+xml"
                with open(file_path, "rb") as f:
                    self._send(200, f.read(), ctype)
            else:
                index_path = os.path.join(STATIC_DIR, "index.html")
                with open(index_path, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path.startswith("/api/"):
            self._proxy_to_agent("POST")
        else:
            self._send(404, b'{"error":"Not found"}')


if __name__ == "__main__":
    server = ThreadedHTTPServer(("0.0.0.0", PORT), WebHandler)
    print(f"pihole-sync.ertechlab.com listening on 0.0.0.0:{PORT}")
    server.serve_forever()
