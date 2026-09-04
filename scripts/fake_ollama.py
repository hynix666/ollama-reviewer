"""Fake Ollama server for offline end-to-end tests.

Stdlib only (the repo has zero dependencies). Serves /api/tags and
/api/generate on an ephemeral localhost port from a per-model script, so
selftest.py can drive the real collect -> review -> consensus -> render path
and both front ends against scripted model behavior without a live Ollama
install. The client steers here through its normal OLLAMA_HOST seam.

Usage:
    srv = fake_ollama.start({"m": [{"body": "prose", "delay": 0.5}]})
    ... run with cfg["base_url"] = srv.base_url or OLLAMA_HOST=... ...
    srv.log      # every request: model, behavior name, schema present, time
    srv.close()  # shutdown + join

A behavior is a dict consumed in order (the last one repeats):
    name    label recorded in srv.log (default "")
    body    completion text; default '{"findings": []}'
    status  HTTP status to return instead of 200 (drives client error kinds)
    delay   seconds to sleep before responding (drives deadline behavior)
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    """Handle to a running fake server."""

    def __init__(self, httpd, scripts, log, lock):
        self.httpd = httpd
        self.scripts = scripts
        self.log = log
        self.lock = lock
        self.base_url = "http://127.0.0.1:%d" % httpd.server_address[1]
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def start(scripts):
    """Start a fake Ollama serving `scripts` (model name -> behavior list)."""
    handle = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/tags"):
                models = [{"name": m} for m in handle["scripts"]]
                self._reply(200, {"models": models})
            else:
                self._reply(404, {"error": "unknown path %s" % self.path})

        def do_POST(self):
            if not self.path.startswith("/api/generate"):
                self._reply(404, {"error": "unknown path %s" % self.path})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._reply(400, {"error": "bad JSON body"})
                return
            queue = handle["scripts"].get(payload.get("model", ""))
            if queue is None:
                self._reply(404, {"error": "model not scripted"})
                return
            with handle["lock"]:
                behavior = queue.pop(0) if len(queue) > 1 else queue[0]
            handle["log"].append(
                {
                    "model": payload.get("model", ""),
                    "behavior": behavior.get("name", ""),
                    "schema": payload.get("format") is not None,
                    "t": time.monotonic(),
                }
            )
            time.sleep(float(behavior.get("delay", 0)))
            # The client may abort while we sleep (deadline met); a failed
            # write to a closed socket must not crash the server thread.
            try:
                self.wfile.write(b"")
            except (ConnectionError, BrokenPipeError):
                return
            status = int(behavior.get("status", 200))
            if status != 200:
                self._reply(status, {"error": "scripted failure"})
                return
            body = behavior.get("body", '{"findings": []}')
            self._reply(
                200,
                {
                    "response": body,
                    "done_reason": "stop",
                    "eval_count": max(1, len(body.split())),
                    "prompt_eval_count": 100,
                },
            )

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    handle.update({"scripts": scripts, "log": [], "lock": threading.Lock()})
    return FakeOllama(httpd, handle["scripts"], handle["log"], handle["lock"])
