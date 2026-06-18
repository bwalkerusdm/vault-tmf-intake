"""
POST /api/etmf/actions/resolve_record    (bearer auth)
body: {study?: str, country?: str, site?: str}

Glean agent tool: resolves the study / country / site references the agent read
from a document to Vault record IDs, against the bundled snapshot
(data/study_records.json) -- no live Vault call -- with a live VQL fallback if no
snapshot is present. The agent passes whatever identifiers it found (study code or
project code, country name, site number/name); resolution is fail-safe.

Returns:
  {study__v, study_country__v, site__v, study_name, study_country_name, site_name,
   all_resolved, parent_validated, resolved_via, notes[]}

all_resolved=false means HOLD: a study/site/country mismatch, a record not in the
study, or snapshot IDs not yet populated. `notes` explains why -- surface it to the
user so they can see exactly what didn't line up.
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import json
import os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from lib import vault_reference

GLEAN_BEARER_TOKEN = os.environ.get("GLEAN_BEARER_TOKEN", "")


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth_ok(self):
        return self.headers.get("Authorization", "").replace("Bearer ", "") == GLEAN_BEARER_TOKEN

    def do_POST(self):
        if not self._auth_ok():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or "{}")
        except Exception:
            return self._send(400, {"error": "bad_json"})
        try:
            rec = vault_reference.resolve(
                study=body.get("study"),
                country=body.get("country"),
                site=body.get("site"),
            )
            return self._send(200, rec)
        except Exception as e:
            return self._send(502, {"error": "resolve_failed", "detail": str(e)[:200]})
