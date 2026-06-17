"""
Vault reference data: classification taxonomy + study/site resolution.

DESIGN (speed + risk):
- Taxonomy is Vault CONFIGURATION (rarely changes) -> fetch the full type tree
  ONCE from the metadata API, cache in-memory with a TTL, refresh on schedule.
  The gate's classify() matches against this controlled snapshot, not a live
  per-document call. Fewer API calls, matches a known/validated set.
- Study/Country/Site are BUSINESS RECORDS (change over time) -> resolve via the
  existing lookup (index or VQL), but CACHE per batch: many docs in a ~100-doc
  batch share the same study/site, so resolve each unique reference once.
- parent_validated stays the hard guard regardless of source: a site's parent
  study/country must match, which catches a wrong resolution no matter how fetched.

Both are exposed as the dependencies run_all_checks expects:
  get_taxonomy() -> list[dict]
  make_resolver() -> fn(text) -> record dict
"""
from __future__ import annotations
import os
import time
import logging

from lib import vault_filing   # reuse the shared session + http

logger = logging.getLogger("tmf.reference")

VAULT_BASE_URL = os.environ["VAULT_BASE_URL"]
VAULT_API_VERSION = os.environ.get("VAULT_API_VERSION", "v25.1")
VAULT_CLIENT_ID = os.environ["VAULT_CLIENT_ID"]

TAXONOMY_TTL_SECONDS = int(os.environ.get("TMF_TAXONOMY_TTL_SECONDS", str(24 * 3600)))

_taxonomy_cache = {"data": None, "expires_at": 0}


# ---------------------------------------------------------------------------
# CLASSIFICATION TAXONOMY  (metadata API, cached)
# ---------------------------------------------------------------------------
def get_taxonomy(force=False) -> list[dict]:
    """Return the flattened type/subtype/classification taxonomy.
    Cached for TAXONOMY_TTL_SECONDS (default 24h). force=True refreshes now.

    Source: Vault Document Types metadata API (the idiomatic surface for the
    type tree -- richer than VQL on the doc-type object).
      GET /metadata/objects/documents/types
      GET /metadata/objects/documents/types/{type}
    """
    now = time.time()
    if not force and _taxonomy_cache["data"] and _taxonomy_cache["expires_at"] > now:
        return _taxonomy_cache["data"]

    flat = _fetch_taxonomy()
    _taxonomy_cache["data"] = flat
    _taxonomy_cache["expires_at"] = now + TAXONOMY_TTL_SECONDS
    logger.info("Taxonomy refreshed: %d classification entries", len(flat))
    return flat


def _get(path: str) -> dict:
    session = vault_filing.get_vault_session()
    r = vault_filing.http.get(
        f"{VAULT_BASE_URL}/api/{VAULT_API_VERSION}{path}",
        headers={"Authorization": session, "X-VaultAPI-ClientID": VAULT_CLIENT_ID,
                 "Accept": "application/json"},
        timeout=30)
    r.raise_for_status()
    return r.json()


def _fetch_taxonomy() -> list[dict]:
    """Walk the document type tree -> flat list of valid filing triples.

    NOTE: the exact nesting of the metadata response should be VERIFIED against
    your Vault (types -> subtypes -> classifications). The flattening below is
    the common shape; adjust field access if your response differs.
    Each flat entry: {type__v, subtype__v, classification__v, label, tmf_rm_v3?}
    """
    flat: list[dict] = []
    types = _get("/metadata/objects/documents/types").get("types", [])
    for t in types:
        t_name = t.get("name") or t.get("value")
        # fetch the type detail to get subtypes/classifications
        detail = _get(f"/metadata/objects/documents/types/{t_name}")
        type_obj = detail.get("type", detail)
        subtypes = type_obj.get("subtypes") or []
        if not subtypes:
            flat.append({"type__v": t_name, "subtype__v": None,
                         "classification__v": None, "label": type_obj.get("label")})
            continue
        for st in subtypes:
            st_name = st.get("name") or st.get("value")
            classifications = st.get("classifications") or []
            if not classifications:
                flat.append({"type__v": t_name, "subtype__v": st_name,
                             "classification__v": None, "label": st.get("label")})
                continue
            for cl in classifications:
                flat.append({
                    "type__v": t_name,
                    "subtype__v": st_name,
                    "classification__v": cl.get("name") or cl.get("value"),
                    "label": cl.get("label"),
                    "tmf_rm_v3": cl.get("tmf_rm_v3") or cl.get("reference_model_id"),
                })
    return flat


# ---------------------------------------------------------------------------
# STUDY / COUNTRY / SITE RESOLUTION  (VQL/index, cached per batch)
# ---------------------------------------------------------------------------
def make_resolver(use_index=True):
    """Return a resolver fn(text)->record with an in-process cache so repeated
    references to the same study/site in a batch resolve once.

    The resolver extracts study/country/site hints from the document text, looks
    them up, validates the parent relationship, and returns the record dict the
    resolve_records check expects. Wire the actual lookup (index or VQL) in
    _lookup(); the caching wrapper is the speed win and is source-agnostic."""
    cache: dict[str, dict] = {}

    def resolver(text: str) -> dict:
        key = _resolution_key(text)
        if key in cache:
            return cache[key]
        rec = _lookup(text, use_index=use_index)
        cache[key] = rec
        return rec

    return resolver


def _resolution_key(text: str) -> str:
    """Cheap key from the study/site hints so identical references hit the cache.
    Built from the same hints _lookup resolves on, so the cache stays consistent."""
    h = _extract_hints(text or "")
    key = "|".join(v for v in (h.get("study"), h.get("country"), h.get("site")) if v)
    return key or (text or "")[:60]


# Identifier patterns. VERIFY/extend to match how your studies, countries, and
# sites actually appear in document text (or swap _extract_hints for an LLM/index
# extractor -- the rest of resolution is unchanged).
import re
_STUDY_RE = re.compile(r"\b([A-Z]{2,5}-\d{2,5})\b")              # e.g. AFT-38, ONC-1234
_SITE_RE = re.compile(r"\bsite[\s#:]*([A-Za-z0-9\-]{2,12})\b", re.I)
_COUNTRY_RE = re.compile(
    r"\b(United States|USA|US|Canada|United Kingdom|UK|Germany|France|Spain|"
    r"Italy|Japan|Australia|Brazil|[A-Z]{2,3}-\d{3})\b")


def _extract_hints(text: str) -> dict:
    """Pull study / country / site hints from the document text. Conservative by
    design: a missed hint -> unresolved -> the gate holds the doc (fail-safe),
    never a wrong auto-file."""
    study = _STUDY_RE.search(text or "")
    site = _SITE_RE.search(text or "")
    country = _COUNTRY_RE.search(text or "")
    return {
        "study": study.group(1) if study else None,
        "site": site.group(1) if site else None,
        "country": country.group(1) if country else None,
    }


def _lookup(text: str, use_index: bool) -> dict:
    """Resolve study/country/site to Vault IDs + validate parent relationship.

    Resolution order:
      - if use_index: try the Glean index first (fast candidate records), then
        confirm via VQL. The index path is a seam (_resolve_via_index); until it
        is wired it returns None and we fall through to VQL, which is the
        system-of-record path and fully implemented below.
      - parent validation (site -> study_country -> study) is the hard guard and
        runs regardless of how records were found, so a wrong resolution is caught
        no matter the source.

    Returns:
      {study__v, study_country__v, site__v, study_name, study_country_name,
       site_name, all_resolved: bool, parent_validated: bool, resolved_via: str}
    Fail-safe: anything unresolved/ambiguous -> all_resolved False (gate holds).

    VERIFY object/field API names against your Vault config: TMF_STUDY_OBJECT,
    TMF_STUDY_COUNTRY_OBJECT, TMF_SITE_OBJECT, and the parent fields used in
    _validate_parentage.
    """
    hints = _extract_hints(text or "")
    if not any(hints.values()):
        return {"all_resolved": False, "parent_validated": False,
                "resolved_via": "no_hints"}
    if use_index:
        via_index = _resolve_via_index(hints)
        if via_index is not None:
            return via_index
    return _resolve_via_vql(hints)


# Object API names (override via env if your config differs)
_STUDY_OBJECT = os.environ.get("TMF_STUDY_OBJECT", "study__v")
_STUDY_COUNTRY_OBJECT = os.environ.get("TMF_STUDY_COUNTRY_OBJECT", "study_country__v")
_SITE_OBJECT = os.environ.get("TMF_SITE_OBJECT", "site__v")


def _resolve_via_index(hints: dict):
    """Seam for the Glean veevavaulttmf index-first path. Return a fully shaped
    record (same keys as _resolve_via_vql) or None to fall through to VQL.
    Not wired yet -> None."""
    return None


def _vql(query: str) -> list[dict]:
    """Run a VQL query with the shared service session; one retry on a stale session."""
    def _do(session):
        r = vault_filing.http.post(
            f"{VAULT_BASE_URL}/api/{VAULT_API_VERSION}/query",
            headers={"Authorization": session, "X-VaultAPI-ClientID": VAULT_CLIENT_ID,
                     "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"q": query}, timeout=30)
        r.raise_for_status()
        return r.json()

    body = _do(vault_filing.get_vault_session())
    if body.get("responseStatus") != "SUCCESS":
        vault_filing._clear_sessions()
        body = _do(vault_filing.get_vault_session(force=True))
    if body.get("responseStatus") != "SUCCESS":
        raise RuntimeError(f"VQL failed: {body}")
    return body.get("data", [])


def _esc(v) -> str:
    return str(v).replace("'", "\\'")


def _resolve_one(obj: str, hint, match_fields: list[str], parent=None):
    """Find exactly one record matching hint on any match field (optionally scoped
    to a parent). Returns {id, name} or None. >1 match -> None (fail-safe: never
    guess which record to file against)."""
    if not hint:
        return None
    where = "(" + " OR ".join(f"{f} = '{_esc(hint)}'" for f in match_fields) + ")"
    if parent:
        where += f" AND {parent[0]} = '{_esc(parent[1])}'"
    try:
        rows = _vql(f"SELECT id, name__v FROM {obj} WHERE {where}")
    except Exception as e:
        logger.warning("VQL resolve failed for %s/%r: %s", obj, hint, e)
        return None
    if len(rows) != 1:
        if len(rows) > 1:
            logger.info("Ambiguous %s for %r (%d) -> unresolved", obj, hint, len(rows))
        return None
    return {"id": rows[0]["id"], "name": rows[0].get("name__v")}


def _resolve_via_vql(hints: dict) -> dict:
    study = _resolve_one(_STUDY_OBJECT, hints.get("study"),
                         ["name__v", "study_number__v"])
    country = _resolve_one(_STUDY_COUNTRY_OBJECT, hints.get("country"), ["name__v"],
                           parent=("study__v", study["id"]) if study else None)
    site = _resolve_one(_SITE_OBJECT, hints.get("site"),
                        ["name__v", "site_number__v"],
                        parent=("study_country__v", country["id"]) if country else None)
    parent_validated = _validate_parentage(study, country, site)
    return {
        "study__v": study["id"] if study else None,
        "study_country__v": country["id"] if country else None,
        "site__v": site["id"] if site else None,
        "study_name": study["name"] if study else None,
        "study_country_name": country["name"] if country else None,
        "site_name": site["name"] if site else None,
        "all_resolved": bool(study and country and site),
        "parent_validated": parent_validated,
        "resolved_via": "vault_vql",
    }


def _validate_parentage(study, country, site) -> bool:
    """Confirm site -> study_country -> study in Vault (explicit re-read guard).
    VERIFY the parent reference field names against your object model."""
    if not (study and country and site):
        return False
    try:
        rows = _vql(f"SELECT id, study_country__v FROM {_SITE_OBJECT} "
                    f"WHERE id = '{_esc(site['id'])}'")
        if len(rows) != 1 or rows[0].get("study_country__v") != country["id"]:
            return False
        rows = _vql(f"SELECT id, study__v FROM {_STUDY_COUNTRY_OBJECT} "
                    f"WHERE id = '{_esc(country['id'])}'")
        return len(rows) == 1 and rows[0].get("study__v") == study["id"]
    except Exception as e:
        logger.warning("parentage validation failed (%s) -> not validated", e)
        return False
