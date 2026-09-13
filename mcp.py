#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vercel serverless adapter for the kie.ai MCP server (Streamable HTTP transport).

Claude Custom Connectors need a public HTTPS endpoint that speaks MCP over HTTP.
This file is the Vercel function that provides it. All business logic and
JSON-RPC handling is reused from kie_server.py (repo root) — this file only adds
the HTTP layer in the shape Vercel's Python runtime expects: a class named
`handler` subclassing BaseHTTPRequestHandler.

Routing (see vercel.json):
  POST /mcp     -> /api/mcp  : JSON-RPC 2.0 request  -> JSON-RPC response
  GET  /mcp     -> /api/mcp  : returns the liveness payload (no SSE stream)
  GET  /health  -> /api/mcp  : {"status": "ok"}

Env vars:
  KIE_API_KEY     kie.ai API key (required). Set it in Vercel > Settings > Env Vars.
  KIE_TIMEOUT_SEC default max seconds a generate call waits (default 180). On
                  Vercel keep this BELOW maxDuration in vercel.json (60), or use a
                  small per-call timeout_sec and poll with kie_get_task.
  KIE_MCP_TOKEN   optional bearer token; if set, POST requires
                  "Authorization: Bearer <token>". Note: Claude's connector UI
                  only supplies OAuth, not a static header, so this is mainly for
                  manual tests / a fronting proxy (see README).
"""

import os
import sys
import json
from http.server import BaseHTTPRequestHandler

# kie_server.py lives at the repo root, one level up from this api/ folder.
# vercel.json ships it into the bundle via includeFiles, so this import works.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import kie_server as core  # noqa: E402

AUTH_TOKEN = os.environ.get("KIE_MCP_TOKEN", "").strip()


def _authorized(headers):
    """True if no token is configured, or the request carries the right bearer."""
    if not AUTH_TOKEN:
        return True
    auth = headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[7:].strip() == AUTH_TOKEN


class handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Reached via the /health rewrite (and a GET on /mcp). No SSE is offered,
        # so a simple liveness payload is the friendliest answer here.
        self._json(200, {"status": "ok",
                         "server": core.SERVER_NAME,
                         "version": core.SERVER_VERSION,
                         "key_set": bool(core.api_key())})

    def do_POST(self):
        if not _authorized(self.headers):
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
            # Notifications / responses need no body -> 202 Accepted.
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json(status, resp)
