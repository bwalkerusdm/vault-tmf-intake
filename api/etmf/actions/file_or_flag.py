"""
POST /api/etmf/actions/file_or_flag    (bearer auth)
body: {box_file_id, filename, findings: {tmf_content, classification, phi_pii,
       alcoa_plus, record_resolution}}

THE GATE. The Glean agent has done the reasoning and sends its structured findings.
This action does NOT trust them blindly: it re-validates the proposed classification
against the LIVE Vault taxonomy, applies the deterministic thresholds + fail-safe,
runs the gate, and then either files the document to Vault In Progress (delegated)
and moves it to 2-Filed, or holds it in 3-Exceptions (or 0-Rejected for not_tmf)
with a .WHY.txt. Every disposition is logged.

Response:
  filed -> {disposition:"filed", status, checks_hash, vault_document_id, vault_link}
  held  -> {disposition:"held",  status, checks_hash, disposition_kind, reasons[], flag_rules[]}
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import json
import os

# --- make the repo root importable so `from lib import ...` resolves on Vercel ---
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from lib import box_client, disposition
from lib.run_all_checks import gate_from_findings
from lib.vault_reference import get_taxonomy

GLEAN_BEARER_TOKEN = os.environ.get("GLEAN_BEARER_TOKEN", "")
INBOX = os.environ["BOX_INBOX_FOLDER_ID"]
FILED = os.environ["BOX_FILED_FOLDER_ID"]
EXCEPTIONS = os.environ["BOX_EXCEPTIONS_FOLDER_ID"]
BULK_DELEGATOR_USER_ID = int(os.environ.get("BULK_DELEGATOR_USER_ID", "30979130"))
REJECTED = os.environ.get("BOX_REJECTED_FOLDER_ID")  # optional: not_tmf docs route here if set
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
        findings = body.get("findings")
        if not box_file_id or not filename:
            return self._send(400, {"error": "box_file_id and filename are required"})

        try:
            taxonomy = get_taxonomy()  # cached; used to enforce classification membership
            record = gate_from_findings(
                findings, taxonomy, document={"filename": filename, "box_file_id": box_file_id})

            token = box_client._box_token()
            item = {"id": box_file_id, "name": filename}
            outcome = disposition.process_one(
                item, None, record, token,
                delegator_userid=BULK_DELEGATOR_USER_ID, folders=_FOLDERS)
        except Exception as e:
            return self._send(502, {"error": "file_or_flag_failed", "detail": str(e)[:200]})

        resp = {"disposition": outcome["action"], "status": record["status"],
                "checks_hash": record["checks_hash"]}
        if outcome["action"] == "filed":
            resp.update({"vault_document_id": outcome["vault_document_id"],
                         "vault_link": outcome.get("vault_link")})
        else:
            resp.update({"disposition_kind": outcome.get("disposition_kind"),
                         "reasons": outcome["reasons"], "flag_rules": outcome["flag_rules"]})
        return self._send(200, resp)
