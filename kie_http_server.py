#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Remote (Streamable HTTP) transport for the kie.ai MCP server.

This is what Claude "Custom Connectors" require: a public HTTPS endpoint that
speaks MCP over HTTP instead of stdio. All business logic and JSON-RPC handling
is reused from kie_server.py — this file only adds the HTTP layer.

Endpoint:
  POST /mcp    JSON-RPC 2.0 request  -> single JSON-RPC response (application/json)
  GET  /mcp    -> 405 (no server-initiated SSE stream is offered; not needed here)
  GET  /health -> {"status":"ok"}    (simple liveness probe for humans/hosts)

The MCP endpoint path (/mcp), bind host/port, and an optional bearer token are
configurable via environment variables (see below). Standard library only.

Run locally:
    KIE_API_KEY=... py mcp/kie_http_server.py        # listens on 0.0.0.0:8000

Env vars:
    KIE_API_KEY    kie.ai API key (same as the stdio server).
    KIE_SAVE_DIR   optional default download dir (usually unused when remote).
    PORT / KIE_MCP_PORT   listen port (default 8000; PORT wins, matches most PaaS).
    KIE_MCP_HOST   bind address (default 0.0.0.0).
    KIE_MCP_PATH   MCP endpoint path (default /mcp).
    KIE_MCP_TOKEN  optional bearer token; if set, POST /mcp requires
                   "Authorization: Bearer <token>". See README for the caveat that
                   Claude's connector UI only supplies OAuth, not a static header.
    KIE_TIMEOUT_SEC  default max seconds a generate call waits (default 180).
"""

import os
import sys
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Reuse all logic (tools, JSON-RPC dispatch) from the stdio server sitting next
# to this file. Importing it does NOT start the stdio loop (guarded by __main__).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kie_server as core

MCP_PATH = os.environ.get("KIE_MCP_PATH", "/mcp")
AUTH_TOKEN = os.environ.get("KIE_MCP_TOKEN", "").strip()


def authorized(headers):
    """True if no token is configured, or the request carries the right bearer."""
    if not AUTH_TOKEN:
        return True
    auth = headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[7:].strip() == AUTH_TOKEN


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "kie-mcp/%s" % core.SERVER_VERSION

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        core.log("http %s -" % self.address_string(), fmt % args)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json(200, {"status": "ok", "server": core.SERVER_NAME,
                             "version": core.SERVER_VERSION})
        elif path == MCP_PATH:
            # We do not offer a server-initiated SSE stream — 405 is the
            # spec-sanctioned answer to a GET on the MCP endpoint in that case.
            self._empty(405)
        else:
            self._empty(404)

    def do_DELETE(self):
        # Stateless server: no session to terminate.
        self._empty(405 if self.path.split("?", 1)[0] == MCP_PATH else 404)

    def do_POST(self):
        if self.path.split("?", 1)[0] != MCP_PATH:
            self._empty(404)
            return
        if not authorized(self.headers):
            self._json(401, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32001, "message": "Unauthorized"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        status, resp = core.process_request_body(raw)
        if resp is None:
            self._empty(status)      # 202 for notifications/responses
        else:
            self._json(status, resp)


def main():
    host = os.environ.get("KIE_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT") or os.environ.get("KIE_MCP_PORT") or 8000)
    core.log("HTTP MCP on %s:%d%s (auth: %s, key set: %s)" % (
        host, port, MCP_PATH, "on" if AUTH_TOKEN else "off", bool(core.api_key())))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
