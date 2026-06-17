"""
POST /api/etmf/batch/run_intake_batch   { "max_seconds": 40, "chunk": 5 }   (bearer auth)

MANUAL "process now" trigger. Drives the same evaluate -> file pipeline the
threshold/scheduled path uses -- same endpoints' logic, same Box state machine,
same gate, same audit. The ONLY difference from the scheduled path is that a
person initiates it.

Bounded-pass design (respects the Vercel function timeout): this endpoint keeps
processing chunks until either the Inbox is drained OR it approaches `max_seconds`,
then returns a summary with `done` / `more_remaining`. If `more_remaining > 0`,
the caller (agent or UI) calls again -- enabling a "processed 20... 40... done"
progress UX without any single request timing out.

Idempotent + safe to coexist with the scheduled trigger: both read the Inbox and
move processed files out (Box folder-state), so a doc is never double-processed
even if a manual run and a scheduled run overlap.
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import json
import os
import time

# --- make the repo root importable so `from lib import ...` resolves on Vercel ---
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from lib import box_client
from lib import disposition
from lib.run_all_checks import run_all_checks

GLEAN_BEARER_TOKEN = os.environ.get("GLEAN_BEARER_TOKEN", "")
INBOX = os.environ["BOX_INBOX_FOLDER_ID"]
FILED = os.environ["BOX_FILED_FOLDER_ID"]
EXCEPTIONS = os.environ["BOX_EXCEPTIONS_FOLDER_ID"]
BULK_DELEGATOR_USER_ID = int(os.environ.get("BULK_DELEGATOR_USER_ID", "30979130"))
EVAL_SUFFIX = ".eval.json"
_FOLDERS = {"inbox": INBOX, "filed": FILED, "exceptions": EXCEPTIONS}

# Safety margin under the Vercel function timeout. Set DEFAULT_MAX_SECONDS below
# your actual timeout (e.g. 50 for a 60s function, 250 for 300s).
DEFAULT_MAX_SECONDS = int(os.environ.get("INTAKE_MAX_SECONDS", "40"))


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
        max_seconds = int(body.get("max_seconds", DEFAULT_MAX_SECONDS))

        # dependencies (wire to your repo, same as evaluate_chunk)
        taxonomy, resolver, llm_call = _deps()

        started = time.time()
        token = box_client._box_token()

        # tallies for the run summary
        summary = {"evaluated": 0, "filed": 0, "held": 0, "errors": 0,
                   "held_reasons": {}, "filed_docs": [], "held_docs": []}

        # Process in passes: each pass evaluates a chunk of un-evaluated files and
        # files/holds a chunk of evaluated ones, until time runs out or Inbox drains.
        while time.time() - started < max_seconds:
            did_work = False

            # --- evaluate one chunk of un-evaluated files ---
            items = box_client.list_folder(INBOX, token)
            names = {it["name"] for it in items}
            pending_eval = [it for it in items
                            if not it["name"].endswith(EVAL_SUFFIX)
                            and f'{it["name"]}{EVAL_SUFFIX}' not in names]
            for it in pending_eval[:chunk]:
                if time.time() - started >= max_seconds:
                    break
                did_work = True
                try:
                    raw = box_client.download_file(it["id"], token)
                    text = _extract_text(raw, it["name"])
                    doc = {"filename": it["name"], "box_file_id": it["id"], "text": text}
                    record = run_all_checks(doc, llm_call, taxonomy, resolver)
                    box_client.upload_companion(
                        INBOX, f'{it["name"]}{EVAL_SUFFIX}', json.dumps(record), token)
                    summary["evaluated"] += 1
                except Exception:
                    summary["errors"] += 1

            # --- file/hold one chunk of evaluated files ---
            items = box_client.list_folder(INBOX, token)
            by_name = {it["name"]: it for it in items}
            ready = [it for it in items
                     if not it["name"].endswith(EVAL_SUFFIX)
                     and f'{it["name"]}{EVAL_SUFFIX}' in by_name]
            for it in ready[:chunk]:
                if time.time() - started >= max_seconds:
                    break
                did_work = True
                try:
                    eval_item = by_name[f'{it["name"]}{EVAL_SUFFIX}']
                    record = json.loads(box_client.download_file(eval_item["id"], token))
                    outcome = disposition.process_one(
                        it, eval_item, record, token,
                        delegator_userid=BULK_DELEGATOR_USER_ID, folders=_FOLDERS)
                    _tally(summary, outcome)
                except Exception:
                    summary["errors"] += 1

            if not did_work:
                break  # nothing left to do -> Inbox drained

        # how much remains for a follow-up call
        items = box_client.list_folder(INBOX, token)
        names = {it["name"] for it in items}
        remaining = len([it for it in items if not it["name"].endswith(EVAL_SUFFIX)])

        return self._send(200, {
            "done": remaining == 0,
            "more_remaining": remaining,
            "elapsed_seconds": round(time.time() - started, 1),
            "summary": summary,
            "message": _human_summary(summary, remaining),
        })

def _tally(summary, outcome):
    """Fold one shared-disposition outcome into the run summary."""
    if outcome["action"] == "filed":
        summary["filed"] += 1
        summary["filed_docs"].append(
            {"filename": outcome["filename"],
             "vault_document_id": outcome["vault_document_id"]})
    else:
        summary["held"] += 1
        summary["held_docs"].append(
            {"filename": outcome["filename"], "status": outcome["status"]})
        for rule in outcome["flag_rules"]:
            summary["held_reasons"][rule] = summary["held_reasons"].get(rule, 0) + 1


def _human_summary(s, remaining):
    msg = (f'Processed {s["evaluated"]} document(s): '
           f'{s["filed"]} filed to Vault (In Progress), {s["held"]} held for review')
    if s["held_reasons"]:
        reasons = ", ".join(f'{k}: {v}' for k, v in s["held_reasons"].items())
        msg += f' (held reasons -> {reasons})'
    if s["errors"]:
        msg += f', {s["errors"]} error(s)'
    if remaining > 0:
        msg += f'. {remaining} still waiting -- run again to continue.'
    else:
        msg += '. Inbox fully processed.'
    return msg


def _extract_text(raw, name):
    if name.lower().endswith(".txt"):
        return raw.decode(errors="ignore")
    try:
        from pypdf import PdfReader
        import io
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
    except Exception:
        return raw.decode(errors="ignore")[:20000]


def _deps():
    """Real dependencies: cached taxonomy (metadata API) + per-batch resolver + LLM."""
    from lib.vault_reference import get_taxonomy, make_resolver
    from lib.llm import get_llm_call
    return get_taxonomy(), make_resolver(), get_llm_call()
