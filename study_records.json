"""
POST /api/etmf/actions/reset_demo    {confirm: true, vault: true|false}    (bearer auth)

Returns the demo to its starting state so the SAME documents can be re-filed fresh:
  - Box: moves filed/held files back to 1-Inbox, un-renames them, clears companions
    (always runs; non-destructive to your real data).
  - Vault: deletes ONLY the documents the agent created, so re-filing doesn't
    duplicate. This is destructive, so it is triple-gated:
      1. request must include  "vault": true
      2. env  ALLOW_VAULT_RESET=true  must be set on the project
      3. it only ever deletes documents matching VAULT_RESET_WHERE
         (default: created_via_glean_agent__c = 'yes__c'), capped at VAULT_RESET_MAX
    Leave ALLOW_VAULT_RESET unset on any real vault and this can never delete there.

Response: {box:{...}, vault:{...}|null}
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import json
import os

# --- make the repo root importable so `from lib import ...` resolves on Vercel ---
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from lib import box_client, reset

GLEAN_BEARER_TOKEN = os.environ.get("GLEAN_BEARER_TOKEN", "")
INBOX = os.environ["BOX_INBOX_FOLDER_ID"]
FILED = os.environ["BOX_FILED_FOLDER_ID"]
EXCEPTIONS = os.environ["BOX_EXCEPTIONS_FOLDER_ID"]
REJECTED = os.environ.get("BOX_REJECTED_FOLDER_ID")
_FOLDERS = {"inbox": INBOX, "filed": FILED, "exceptions": EXCEPTIONS}
if REJECTED:
    _FOLDERS["rejected"] = REJECTED

DEFAULT_WHERE = "created_via_glean_agent__c = 'yes__c'"


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
        if body.get("confirm") is not True:
            return self._send(400, {"error": "confirmation_required",
                                    "detail": "send {\"confirm\": true} to run the reset"})

        try:
            token = box_client._box_token()
            box_result = reset.reset_box(token, _FOLDERS)
        except Exception as e:
            return self._send(502, {"error": "box_reset_failed", "detail": str(e)[:200]})

        vault_result = None
        if body.get("vault"):
            if os.environ.get("ALLOW_VAULT_RESET", "").lower() != "true":
                vault_result = {"skipped": "ALLOW_VAULT_RESET is not enabled on this project"}
            else:
                try:
                    from lib import vault_filing  # lazy: Box-only reset needs no Vault env
                    where = os.environ.get("VAULT_RESET_WHERE", DEFAULT_WHERE)
                    cap = int(os.environ.get("VAULT_RESET_MAX", "200"))
                    vault_result = vault_filing.delete_agent_documents(where, max_delete=cap)
                except Exception as e:
                    vault_result = {"error": "vault_reset_failed", "detail": str(e)[:300]}

        return self._send(200, {"box": box_result, "vault": vault_result})
