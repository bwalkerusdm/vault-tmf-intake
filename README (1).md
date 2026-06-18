"""
POST /api/etmf/actions/report_unprocessable   {box_file_id, filename, reason}   (bearer)

The agent calls this when it CANNOT process a document at all -- it couldn't be
read/extracted, the read timed out, or it's an unsupported/corrupt format. Rather
than letting the agent skip the file silently (which would leave it sitting in the
inbox with no record), this holds it in 3-Exceptions with a .WHY.txt explaining the
processing failure and writes an audit event. "We couldn't process it" becomes a
documented exception, never a silent gap.

Response: {disposition:"held", status:"unprocessable", disposition_kind, reason}
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import json
import os

# --- make the repo root importable so `from lib import ...` resolves on Vercel ---
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from lib import box_client, disposition
from lib.run_all_checks import record_for_unprocessable

GLEAN_BEARER_TOKEN = os.environ.get("GLEAN_BEARER_TOKEN", "")
INBOX = os.environ["BOX_INBOX_FOLDER_ID"]
FILED = os.environ["BOX_FILED_FOLDER_ID"]
EXCEPTIONS = os.environ["BOX_EXCEPTIONS_FOLDER_ID"]
BULK_DELEGATOR_USER_ID = int(os.environ.get("BULK_DELEGATOR_USER_ID", "30979130"))
REJECTED = os.environ.get("BOX_REJECTED_FOLDER_ID")
_FOLDERS = {"inbox": INBOX, "filed": FILED, "exceptions": EXCEPTIONS}
if REJECTED:
    _FOLDERS["rejected"] = REJECTED


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        if self.headers.get("Authorization", "").replace("Bearer ", "") != GLEAN_BEARER_TOKEN:
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or "{}")
        except Exception:
            return self._send(400, {"error": "bad_json"})
        box_file_id = body.get("box_file_id")
        filename = body.get("filename")
        reason = body.get("reason")
        if not box_file_id or not filename:
            return self._send(400, {"error": "box_file_id and filename are required"})

        try:
            record = record_for_unprocessable(filename, box_file_id, reason)
            token = box_client._box_token()
            item = {"id": box_file_id, "name": filename}
            outcome = disposition.process_one(
                item, None, record, token,
                delegator_userid=BULK_DELEGATOR_USER_ID, folders=_FOLDERS)
        except Exception as e:
            return self._send(502, {"error": "report_unprocessable_failed", "detail": str(e)[:200]})

        return self._send(200, {"disposition": outcome["action"], "status": "unprocessable",
                                "disposition_kind": outcome.get("disposition_kind"),
                                "reason": reason or "agent could not process this document"})
