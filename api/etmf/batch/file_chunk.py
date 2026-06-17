"""
POST /api/etmf/batch/file_chunk   { "chunk": 5 }   (bearer auth)

Processes the next chunk of EVALUATED Inbox files (those with a .eval.json companion):
  - status == clean  -> file to Vault In Progress (delegated) + glean_* fields
                        -> QC auto-starts natively in Vault (entry action + DAC)
                        -> move file (+ its .eval.json) to 2-Filed
  - status != clean  -> write exception summary into 3-Exceptions
                        -> move file (+ .eval.json) to 3-Exceptions   (stays in Box)
Returns per-file outcome + remaining count. Idempotent: a file already moved out of
Inbox is not reprocessed.

The actual file/hold/audit work lives in lib.disposition -- ONE implementation
shared with run_intake_batch, so there is a single validated filing path.

Filing is SINGLE-THREADED in Vault, so `chunk` * (~3-5s) must stay under the Vercel
timeout. Tune `chunk` (e.g. 5).
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import json
import os

# --- make the repo root importable so `from lib import ...` resolves on Vercel ---
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from lib import box_client
from lib import disposition

GLEAN_BEARER_TOKEN = os.environ.get("GLEAN_BEARER_TOKEN", "")
INBOX = os.environ["BOX_INBOX_FOLDER_ID"]
FILED = os.environ["BOX_FILED_FOLDER_ID"]
EXCEPTIONS = os.environ["BOX_EXCEPTIONS_FOLDER_ID"]
EVAL_SUFFIX = ".eval.json"

# Bulk delegator: the single Vault user id the batch is filed on behalf of.
BULK_DELEGATOR_USER_ID = int(os.environ.get("BULK_DELEGATOR_USER_ID", "30979130"))

_FOLDERS = {"inbox": INBOX, "filed": FILED, "exceptions": EXCEPTIONS}


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
        body = json.loads(self.rfile.read(length) or "{}")
        chunk = int(body.get("chunk", 5))

        token = box_client._box_token()
        items = box_client.list_folder(INBOX, token)
        by_name = {it["name"]: it for it in items}
        # docs that have an eval companion are ready to file/except
        ready = [it for it in items
                 if not it["name"].endswith(EVAL_SUFFIX)
                 and f'{it["name"]}{EVAL_SUFFIX}' in by_name]

        results = []
        for it in ready[:chunk]:
            name = it["name"]
            eval_item = by_name[f"{name}{EVAL_SUFFIX}"]
            try:
                record = json.loads(box_client.download_file(eval_item["id"], token))
            except Exception as e:
                results.append({"filename": name, "outcome": "error",
                                "error": f"could not read eval: {e}"[:160]})
                continue
            try:
                outcome = disposition.process_one(
                    it, eval_item, record, token,
                    delegator_userid=BULK_DELEGATOR_USER_ID, folders=_FOLDERS)
                results.append(_to_response(outcome))
            except Exception as e:
                results.append({"filename": name, "outcome": "error",
                                "error": str(e)[:160]})

        remaining = max(0, len(ready) - len(results))
        return self._send(200, {"processed": results, "remaining": remaining})


def _to_response(outcome: dict) -> dict:
    """Adapt the shared disposition outcome to this endpoint's response shape."""
    if outcome["action"] == "filed":
        return {"filename": outcome["filename"], "outcome": "filed",
                "vault_document_id": outcome["vault_document_id"],
                "vault_link": outcome["vault_link"]}
    return {"filename": outcome["filename"], "outcome": "exception",
            "status": outcome["status"], "reasons": outcome["reasons"]}
