"""
POST /api/etmf/actions/expand_archive    {box_file_id, filename}    (bearer auth)

A .zip/.tar/.gz in the inbox is a bundle of separate TMF artifacts. This action
unpacks it and re-uploads each member into 1-Inbox as its own document, then moves
the original archive out of the inbox (to 2-Filed, renamed) so it isn't reprocessed.
The members then flow through the normal list_inbox -> file_or_flag path, each
classified and filed on its own. Nested archives are NOT recursed (flagged instead).

Response: {archive, extracted:[names], extracted_count, skipped:[[name,reason]]}
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
FILED = os.environ["BOX_FILED_FOLDER_ID"]


def _dedup(base, existing, stem):
    """Avoid inbox name collisions: keep the member's own name, but if it's already
    present, qualify it with the archive stem for provenance."""
    if base not in existing:
        return base
    qualified = f"{stem}__{base}"
    i = 1
    name = qualified
    while name in existing:
        i += 1
        name = f"{stem}__{i}__{base}"
    return name


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
        if not box_file_id or not filename:
            return self._send(400, {"error": "box_file_id and filename are required"})
        if not archive.is_archive(filename):
            return self._send(400, {"error": "not_an_archive", "filename": filename})

        try:
            token = box_client._box_token()
            raw = box_client.download_file(box_file_id, token)
            result = archive.extract_members(raw, filename)

            existing = {it["name"] for it in box_client.list_folder(INBOX, token)}
            stem = os.path.basename(filename).rsplit(".", 1)[0].rsplit(".tar", 1)[0]
            extracted = []
            for base, data in result["members"]:
                name = _dedup(base, existing, stem)
                box_client.upload_file(INBOX, name, data, token)
                existing.add(name)
                extracted.append(name)

            # move the consumed archive out of the inbox so it isn't reprocessed
            try:
                box_client.move_file(box_file_id, FILED, token,
                                     new_name=f"[expanded-{len(extracted)}-files] {filename}")
            except Exception:
                pass  # non-fatal: members are already in the inbox
        except Exception as e:
            return self._send(502, {"error": "expand_archive_failed", "detail": str(e)[:200]})

        return self._send(200, {
            "archive": filename,
            "extracted": extracted,
            "extracted_count": len(extracted),
            "skipped": result["skipped"],
            "message": (f'Expanded {filename} into {len(extracted)} document(s) in the inbox; '
                        f'{len(result["skipped"])} skipped. Re-list the inbox to process them.'),
        })
