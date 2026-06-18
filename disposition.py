"""
Box helpers for the bulk pipeline's folder-state machine.

Folders:  1-Inbox -> evaluate ; pass -> 2-Filed ; fail -> 3-Exceptions
A file's folder IS its status. Re-runs only read Inbox, so processing is idempotent.

NOTE on auth: uses a Box access token (BOX_TOKEN env). The developer token expires
(~60 min); for production move to a Box JWT/CCG app. The move is treated as
NON-FATAL bookkeeping -- a doc filed to Vault but not moved is still correctly filed.
"""
from __future__ import annotations
import os
import json
import urllib.request
import urllib.error

BOX_API = "https://api.box.com/2.0"


def _box_token() -> str:
    # same env var as create.py / vault_filing for consistency
    return os.environ.get("BOX_DEVELOPER_TOKEN") or os.environ["BOX_TOKEN"]


def _req(method: str, url: str, token: str, data=None, headers=None) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def list_folder(folder_id: str, token: str | None = None, limit: int = 200) -> list[dict]:
    """Return file items (not subfolders) in a Box folder."""
    token = token or _box_token()
    url = f"{BOX_API}/folders/{folder_id}/items?limit={limit}&fields=id,name,type"
    data = _req("GET", url, token)
    return [it for it in data.get("entries", []) if it.get("type") == "file"]


def download_file(file_id: str, token: str | None = None) -> bytes:
    """Download a Box file's bytes."""
    token = token or _box_token()
    url = f"{BOX_API}/files/{file_id}/content"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def move_file(file_id: str, dest_folder_id: str, token: str | None = None,
              new_name: str | None = None) -> dict:
    """Move a file by updating its parent. Optionally rename (e.g. stamp outcome).
    Returns the updated file object. Caller should treat failure as non-fatal."""
    token = token or _box_token()
    payload = {"parent": {"id": str(dest_folder_id)}}
    if new_name:
        payload["name"] = new_name
    return _req("PUT", f"{BOX_API}/files/{file_id}", token, data=payload)


def upload_companion(folder_id: str, name: str, content: str,
                     token: str | None = None) -> dict:
    """Upload a small text/JSON companion file (the exception summary) into a folder.
    Uses the upload endpoint (multipart)."""
    token = token or _box_token()
    boundary = "----tmfexcsummary"
    attrs = json.dumps({"name": name, "parent": {"id": str(folder_id)}})
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="attributes"\r\n\r\n{attrs}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f"Content-Type: text/plain\r\n\r\n{content}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        "https://upload.box.com/api/2.0/files/content",
        data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def upload_file(folder_id: str, name: str, content: bytes,
                token: str | None = None) -> dict:
    """Upload arbitrary file BYTES into a folder (multipart). Used to explode an
    archive's members back into the inbox as individual documents."""
    token = token or _box_token()
    boundary = "----tmfuploadbin"
    attrs = json.dumps({"name": name, "parent": {"id": str(folder_id)}})
    pre = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="attributes"\r\n\r\n{attrs}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    post = f"\r\n--{boundary}--\r\n".encode()
    body = pre + content + post
    req = urllib.request.Request(
        "https://upload.box.com/api/2.0/files/content",
        data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def delete_file(file_id: str, token: str | None = None) -> int:
    """Permanently delete a Box file (used by the demo reset to clear the
    pipeline's .WHY.txt / .eval.json companions). Returns HTTP status (204)."""
    token = token or _box_token()
    req = urllib.request.Request(
        f"{BOX_API}/files/{file_id}",
        headers={"Authorization": f"Bearer {token}"}, method="DELETE")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status
