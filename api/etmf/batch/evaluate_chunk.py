"""
POST /api/etmf/batch/evaluate_chunk   { "chunk": 5 }    (bearer auth)

Evaluates the next chunk of un-evaluated files in 1-Inbox and returns their
structured results + how many remain. The orchestrator calls this repeatedly
until remaining == 0. Idempotent: only Inbox files are evaluated, and the
evaluation result is cached (here: written as a companion .eval.json in Inbox so
file_chunk can read it and a re-run won't redo work).

Vercel: keep `chunk` small enough that one call stays under the function timeout.
Evaluation is NOT the single-threaded Vault filing, so it's faster than file_chunk,
but large PDFs + LLM calls still add up -- tune `chunk`.
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import json
import os

# --- wiring to your existing modules (adjust import paths to your repo) ---
# --- make the repo root importable so `from lib import ...` resolves on Vercel ---
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from lib import box_client
from lib.vault_reference import get_taxonomy, make_resolver
from lib.llm import get_llm_call
from lib.run_all_checks import run_all_checks

GLEAN_BEARER_TOKEN = os.environ.get("GLEAN_BEARER_TOKEN", "")
INBOX = os.environ["BOX_INBOX_FOLDER_ID"]
EVAL_SUFFIX = ".eval.json"


def _already_evaluated(name: str, existing_names: set[str]) -> bool:
    return f"{name}{EVAL_SUFFIX}" in existing_names


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
        names = {it["name"] for it in items}
        # only real docs (skip our own .eval.json companions), not yet evaluated
        pending = [it for it in items
                   if not it["name"].endswith(EVAL_SUFFIX)
                   and not _already_evaluated(it["name"], names)]

        # --- dependencies: cached taxonomy (metadata API) + per-batch resolver ---
        taxonomy = get_taxonomy()
        resolver = make_resolver()
        llm_call = get_llm_call()

        results = []
        for it in pending[:chunk]:
            try:
                raw = box_client.download_file(it["id"], token)
                text = _extract_text(raw, it["name"])
                doc = {"filename": it["name"], "box_file_id": it["id"], "text": text}
                record = run_all_checks(doc, llm_call, taxonomy, resolver)
                # cache the eval next to the file so file_chunk can read it
                box_client.upload_companion(
                    INBOX, f'{it["name"]}{EVAL_SUFFIX}', json.dumps(record), token)
                results.append({"filename": it["name"], "status": record["status"],
                                "fileable": record["fileable"],
                                "flag_count": len(record["flags"])})
            except Exception as e:
                results.append({"filename": it["name"], "status": "error",
                                "error": str(e)[:160]})

        remaining = max(0, len(pending) - len(results))
        return self._send(200, {"evaluated": results, "remaining": remaining})


def _extract_text(raw: bytes, name: str) -> str:
    """Pipeline-proof text extraction. HARDEN -> OCR/Document AI for scans + non-PDF."""
    if name.lower().endswith(".txt"):
        return raw.decode(errors="ignore")
    try:
        from pypdf import PdfReader
        import io
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
    except Exception:
        return raw.decode(errors="ignore")[:20000]
