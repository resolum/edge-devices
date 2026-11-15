"""Backend falso (alivia-web-services): implementa el contrato real de /api/v1/edge/device-table y /sync-state.

Responde igual que el Spring real: ETag "<instalación>.<versión>", If-None-Match -> 304, ?wait=n (espera larga, máx. 25),
409 table_version_ahead, 404 sin instalación, 401 credencial inválida, 503 con Retry-After y 422 al confirmar de más.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CREDENTIAL = "5.secreto-de-prueba"


def node(device_id, mac, kind, status="IN_USE", room="sala", function="f"):
    return {"deviceId": device_id, "macAddress": mac, "deviceType": kind, "status": status, "roomKey": room, "function": function}


class FakeBackend:
    def __init__(self, nodes=None, version=1, installation=7):
        self.installation, self.version = installation, version
        self.nodes = list(nodes or [])
        self.mode = "ok"  # ok | 401 | 404 | 503 | 429 | bad-json
        self.retry_after = None
        self.acks: list[int] = []
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silencio
                pass

            def _send(self, code, body=None, headers=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def _auth(self):
                if self.headers.get("Authorization") != f"Bearer {CREDENTIAL}" or backend.mode == "401":
                    self._send(401)
                    return False
                if backend.mode == "404":
                    self._send(404)
                    return False
                if backend.mode in ("503", "429"):
                    h = {"Retry-After": str(backend.retry_after)} if backend.retry_after is not None else {}
                    self._send(int(backend.mode), headers=h)
                    return False
                return True

            def do_GET(self):
                u = urlparse(self.path)
                if u.path != "/api/v1/edge/device-table":
                    return self._send(404)
                backend.requests.append({"method": "GET", "inm": self.headers.get("If-None-Match"), "query": u.query})
                if not self._auth():
                    return
                if backend.mode == "bad-json":
                    return self._send(200, {"nope": 1}, {"ETag": backend.etag()})
                inm = self.headers.get("If-None-Match")
                if inm:
                    inst, _, ver = inm.strip('"').partition(".")
                    if inst.isdigit() and ver.isdigit() and int(inst) == backend.installation:
                        if int(ver) > backend.version:
                            return self._send(409, {"error": "table_version_ahead"})
                        if int(ver) == backend.version:
                            wait = min(int((parse_qs(u.query).get("wait") or ["0"])[0]), 25)
                            end = time.monotonic() + wait
                            while time.monotonic() < end:
                                if backend.version != int(ver) or backend.mode != "ok":
                                    break
                                time.sleep(0.02)
                            if backend.version == int(ver) and backend.mode == "ok":
                                return self._send(304, headers={"ETag": backend.etag()})
                            if backend.mode != "ok":
                                return self._auth() and None
                return self._send(200, backend.table(), {"ETag": backend.etag()})

            def do_PUT(self):
                if urlparse(self.path).path != "/api/v1/edge/sync-state":
                    return self._send(404)
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                backend.requests.append({"method": "PUT", "body": body})
                if not self._auth():
                    return
                applied = body.get("appliedVersion")
                if applied is None or applied < 0:
                    return self._send(400)
                if applied > backend.version:
                    return self._send(422)
                backend.acks.append(applied)
                self._send(200, {"appliedVersion": max(backend.acks), "ackedAt": "2026-11-10T00:00:00Z"})

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def etag(self):
        return f'"{self.installation}.{self.version}"'

    def table(self):
        with self._lock:
            return {"installationId": self.installation, "householdId": 3, "version": self.version, "nodes": list(self.nodes)}

    def change(self, nodes):
        """El administrador cambia los dispositivos del hogar: la versión sube en uno."""
        with self._lock:
            self.nodes = list(nodes)
            self.version += 1

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()
