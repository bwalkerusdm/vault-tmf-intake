"""
Shared Vault filing functions, extracted from create.py so BOTH the interactive
endpoint (create.py) and the bulk pipeline (file_chunk.py) use one implementation.

Delegated-access filing: documents are attributed to the delegating Vault user,
not the service account. For the bulk lane, the delegator is a single batch
delegator (a Vault user id) -- see BULK_DELEGATOR_USER_ID in file_chunk.

This module is import-safe and side-effect free at import time except reading env.
"""
from __future__ import annotations
import os
import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

VAULT_BASE_URL = os.environ["VAULT_BASE_URL"]
VAULT_API_VERSION = os.environ.get("VAULT_API_VERSION", "v25.1")
VAULT_USERNAME = os.environ["VAULT_USERNAME"]
VAULT_PASSWORD = os.environ["VAULT_PASSWORD"]
VAULT_CLIENT_ID = os.environ["VAULT_CLIENT_ID"]
VAULT_ID = int(os.environ.get("VAULT_ID", "61650"))
BOX_DEVELOPER_TOKEN = os.environ["BOX_DEVELOPER_TOKEN"]

_session_cache = {"session_id": None, "expires_at": 0}


def make_http_session():
    s = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=[429, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s


http = make_http_session()

# Separate session for /auth with NO retry adapter. Verified risk: retrying a
# failing /auth (wrong password, or auth burst limit) is what causes account
# lockout. Auth must fail fast, not retry.
_auth_http = requests.Session()

# Cache TTL must be < the Vault's configured session duration (Admin > Domain
# Settings; options as low as 10 min). Default to a conservative 9 min; override
# via env to match your Vault. We also react to actual 401s rather than trust this.
SESSION_TTL_SECONDS = int(os.environ.get("VAULT_SESSION_TTL_SECONDS", str(9 * 60)))


def get_vault_session(force=False):
    """Authenticate as the service account; cache for SESSION_TTL_SECONDS.
    force=True bypasses the cache (used to recover from an expired session)."""
    now = time.time()
    if (not force and _session_cache["session_id"]
            and _session_cache["expires_at"] > now + 30):
        return _session_cache["session_id"]
    try:
        response = _auth_http.post(
            f"{VAULT_BASE_URL}/api/{VAULT_API_VERSION}/auth",
            data={"username": VAULT_USERNAME, "password": VAULT_PASSWORD},
            headers={"X-VaultAPI-ClientID": VAULT_CLIENT_ID},
            timeout=30,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Vault auth request failed: {e}")
    body = {}
    try:
        body = response.json()
    except Exception:
        pass
    # Verified: auth burst limit returns API_LIMIT_EXCEEDED. Do NOT hammer it.
    err_type = (body.get("errors") or [{}])[0].get("type", "") if body else ""
    if response.status_code == 429 or err_type == "API_LIMIT_EXCEEDED":
        raise RuntimeError(
            "Vault auth burst limit hit (API_LIMIT_EXCEEDED). Backing off; "
            "do not retry immediately. Wait ~60s before re-attempting.")
    if body.get("responseStatus") != "SUCCESS":
        raise RuntimeError(f"Vault auth failed: {body or response.text[:300]}")
    _session_cache["session_id"] = body["sessionId"]
    _session_cache["expires_at"] = now + SESSION_TTL_SECONDS
    return body["sessionId"]


# delegated sessions cached per delegator for the life of the process so a bulk
# batch under one delegator doesn't re-login per document.
_delegated_cache: dict[int, dict] = {}


def get_delegated_session(service_session_id, delegator_userid, reuse=True):
    """Exchange the service session for a session acting AS delegator_userid.
    With reuse=True (bulk), caches per delegator to avoid re-login each doc."""
    now = time.time()
    if reuse:
        c = _delegated_cache.get(delegator_userid)
        if c and c["expires_at"] > now + 60:
            return c["session_id"]
    response = http.post(
        f"{VAULT_BASE_URL}/api/{VAULT_API_VERSION}/delegation/login",
        headers={
            "Authorization": service_session_id,
            "X-VaultAPI-ClientID": VAULT_CLIENT_ID,
            "Accept": "application/json",
        },
        data={"vault_id": VAULT_ID, "delegator_userid": delegator_userid},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("responseStatus") != "SUCCESS":
        raise RuntimeError(f"Vault delegated session failed: {body}")
    sid = body["delegated_sessionid"]
    if reuse:
        _delegated_cache[delegator_userid] = {"session_id": sid,
                                              "expires_at": now + SESSION_TTL_SECONDS}
    return sid


def _clear_sessions():
    """Drop all cached sessions so the next call re-authenticates. Used to
    recover from an expired/invalid session (verified: sessions expire on
    inactivity per Vault's configured duration, as low as 10 min)."""
    _session_cache["session_id"] = None
    _session_cache["expires_at"] = 0
    _delegated_cache.clear()


def fetch_file_from_box(file_id):
    response = http.get(
        f"https://api.box.com/2.0/files/{file_id}/content",
        headers={"Authorization": f"Bearer {BOX_DEVELOPER_TOKEN}"},
        timeout=60,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.content


def post_document_to_vault(session_id, file_bytes, filename, metadata):
    files = {"file": (filename, file_bytes, "application/pdf")}
    response = http.post(
        f"{VAULT_BASE_URL}/api/{VAULT_API_VERSION}/objects/documents",
        headers={"Authorization": session_id, "X-VaultAPI-ClientID": VAULT_CLIENT_ID},
        files=files,
        data=metadata,
        timeout=120,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("responseStatus") != "SUCCESS":
        raise RuntimeError(f"Vault document creation failed: {body}")
    return body["id"]


def _is_session_error(exc) -> bool:
    """Detect an expired/invalid Vault session from an HTTPError."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    if resp.status_code == 401:
        return True
    try:
        errs = resp.json().get("errors", [])
        return any(e.get("type") in ("INVALID_SESSION_ID", "SESSION_TIMEOUT",
                                     "AUTHENTICATION_FAILED") for e in errs)
    except Exception:
        return False


def file_one_document(file_bytes, filename, metadata, delegator_userid, reuse_session=True):
    """service session -> delegated session -> create. Returns doc id.
    Reacts to an expired session: clears caches, re-authenticates, retries ONCE.
    (Verified: don't trust a timer for expiry; Vault session duration is
    configurable and can be as low as 10 min, so a long batch may outlive it.)"""
    def _attempt():
        service = get_vault_session()
        delegated = get_delegated_session(service, delegator_userid, reuse=reuse_session)
        return post_document_to_vault(delegated, file_bytes, filename, metadata)

    try:
        return _attempt()
    except requests.HTTPError as e:
        if _is_session_error(e):
            logger.warning("Vault session expired/invalid; re-authenticating and retrying once.")
            _clear_sessions()
            return _attempt()   # one clean retry with a fresh session
        raise


def vault_link(doc_id) -> str:
    return f"{VAULT_BASE_URL}/ui/#doc_info/{doc_id}"


# --- Demo reset support: find + delete ONLY documents this pipeline created. ---
# Tightly scoped (created_via_glean_agent__c marker), capped, and only ever called
# behind the reset action's explicit ALLOW_VAULT_RESET gate. Never use in prod.

def query_document_ids(where_clause: str) -> list:
    sid = get_vault_session()
    r = http.post(
        f"{VAULT_BASE_URL}/api/{VAULT_API_VERSION}/query",
        headers={"Authorization": sid, "X-VaultAPI-ClientID": VAULT_CLIENT_ID,
                 "Accept": "application/json"},
        data={"q": f"SELECT id FROM documents WHERE {where_clause}"}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("responseStatus") != "SUCCESS":
        raise RuntimeError(f"Vault query failed: {body}")
    return [row["id"] for row in body.get("data", []) if row.get("id") is not None]


def delete_document(doc_id) -> object:
    sid = get_vault_session()
    r = http.delete(
        f"{VAULT_BASE_URL}/api/{VAULT_API_VERSION}/objects/documents/{doc_id}",
        headers={"Authorization": sid, "X-VaultAPI-ClientID": VAULT_CLIENT_ID,
                 "Accept": "application/json"}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("responseStatus") != "SUCCESS":
        raise RuntimeError(f"Vault delete failed for {doc_id}: {body}")
    return doc_id


def delete_agent_documents(where_clause: str, max_delete: int = 200) -> dict:
    """Delete documents matching where_clause (intended: the agent-created marker).
    Refuses if the match count exceeds max_delete -- a guard against a too-broad
    clause nuking real content."""
    ids = query_document_ids(where_clause)
    if len(ids) > max_delete:
        raise RuntimeError(
            f"refusing to delete {len(ids)} documents (> safety cap {max_delete}). "
            f"Narrow VAULT_RESET_WHERE or raise VAULT_RESET_MAX intentionally.")
    deleted = [delete_document(did) for did in ids]
    return {"matched": len(ids), "deleted_count": len(deleted),
            "deleted_ids": deleted, "where": where_clause}
