"""
POST /api/etmf/actions/list_inbox    {}    (bearer auth)

Glean agent tool: returns the documents waiting in the Box 1-Inbox so the agent can
work through them. Companions (.eval.json / .WHY.txt) are filtered out. Pure I/O --
no reasoning, no model.

Response: {documents: [{box_file_id, filename}], count}
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import json
import os

# --- make the repo root importable so `from lib import ...` resolves on Vercel ---
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from lib import box_client, archive

GLEAN_BEARER_TOKEN = os.environ.get("GLEAN_BEARER_TOKEN", "")
INBOX = os.environ["BOX_INBOX_FOLDER_ID"]
_COMPANION_SUFFIXES = (".eval.json", ".WHY.txt")


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
            token = box_client._box_token()
            items = box_client.list_folder(INBOX, token)
            docs = [{"box_file_id": it["id"], "filename": it["name"],
                     "is_archive": archive.is_archive(it["name"])}
                    for it in items
                    if not it["name"].endswith(_COMPANION_SUFFIXES)]
            archives = sum(1 for d in docs if d["is_archive"])
            return self._send(200, {"documents": docs, "count": len(docs),
                                    "archives": archives})
        except Exception as e:
            return self._send(502, {"error": "list_inbox_failed", "detail": str(e)[:200]})

    do_GET = do_POST
