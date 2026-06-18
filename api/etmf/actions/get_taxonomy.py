"""
POST /api/etmf/actions/get_taxonomy    {}    (bearer auth)

Glean agent tool: returns the valid classification taxonomy (flat list of
type/subtype/classification triples) the agent must classify within. Served from
the bundled snapshot (data/taxonomy_snapshot.json) when present -- no live Vault
call -- otherwise live-fetched and cached. The agent must pick ONLY a triple that
appears here; the gate re-checks membership against this same source.

Response: {taxonomy: [{type__v, subtype__v, classification__v, label, tmf_rm_v3}],
           count, source}
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
        try:
            status = vault_reference.snapshot_status()
            tax = vault_reference.get_taxonomy()
            source = "snapshot" if status["taxonomy_snapshot"] else "vault_live"
            return self._send(200, {"taxonomy": tax, "count": len(tax), "source": source})
        except Exception as e:
            return self._send(502, {"error": "get_taxonomy_failed", "detail": str(e)[:200]})

    do_GET = do_POST
