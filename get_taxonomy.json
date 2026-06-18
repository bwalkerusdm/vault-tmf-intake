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
import json
import time
import logging

logger = logging.getLogger("tmf.reference")

VAULT_API_VERSION = os.environ.get("VAULT_API_VERSION", "v25.1")

TAXONOMY_TTL_SECONDS = int(os.environ.get("TMF_TAXONOMY_TTL_SECONDS", str(24 * 3600)))

_taxonomy_cache = {"data": None, "expires_at": 0}

# ---------------------------------------------------------------------------
# SNAPSHOT (indexed) DATA  -- bundled JSON, read at runtime, NO live Vault call
# ---------------------------------------------------------------------------
# The taxonomy and the study's study/country/site records are stable for a fixed
# study, so we serve them from committed snapshots instead of hitting Vault on
# every document. This removes per-document latency and, more importantly, keeps
# us off the Vault API burst/session limits during a batch or a live demo.
#
# Refresh with:  python setup/sync_snapshot.py   (pulls from Vault, rewrites the
# two JSON files); commit + redeploy. Until a snapshot file exists, each getter
# falls back to the live Vault path, so nothing breaks if a file is absent.
#
# Principle: the snapshot is authoritative AS OF its last refresh. The gate still
# validates the agent's classification against this snapshot (the anti-hallucination
# backstop) -- so keep it fresh (and re-sync right before a demo).
_DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
_TAXONOMY_SNAPSHOT = os.path.join(_DATA_DIR, "taxonomy_snapshot.json")
_RECORDS_SNAPSHOT = os.path.join(_DATA_DIR, "study_records.json")

_record_map_cache = {"data": None, "loaded": False}


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("snapshot %s unreadable (%s) -> ignoring", path, e)
        return None


def _load_taxonomy_snapshot():
    """Bundled taxonomy snapshot as a flat list, or None if absent/empty."""
    obj = _load_json(_TAXONOMY_SNAPSHOT)
    if not obj:
        return None
    flat = obj.get("taxonomy") if isinstance(obj, dict) else obj
    return flat or None


def _vault_env():
    """Read Vault env lazily so importing this module (and serving snapshots)
    works even where Vault creds aren't configured -- e.g. taxonomy is fully
    snapshot-served. Only the live-fetch path needs these."""
    return {
        "base": os.environ["VAULT_BASE_URL"],
        "client": os.environ["VAULT_CLIENT_ID"],
    }


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

    # Snapshot first (no live call) unless a refresh was explicitly requested.
    if not force:
        snap = _load_taxonomy_snapshot()
        if snap is not None:
            _taxonomy_cache["data"] = snap
            _taxonomy_cache["expires_at"] = now + TAXONOMY_TTL_SECONDS
            logger.info("Taxonomy from snapshot: %d entries", len(snap))
            return snap

    # Fallback (or force): live fetch from Vault.
    flat = _fetch_taxonomy()
    _taxonomy_cache["data"] = flat
    _taxonomy_cache["expires_at"] = now + TAXONOMY_TTL_SECONDS
    logger.info("Taxonomy refreshed from Vault: %d classification entries", len(flat))
    return flat


def _get(path: str) -> dict:
    from lib import vault_filing  # lazy: snapshot serving needs no Vault session
    env = _vault_env()
    session = vault_filing.get_vault_session()
    r = vault_filing.http.get(
        f"{env['base']}/api/{VAULT_API_VERSION}{path}",
        headers={"Authorization": session, "X-VaultAPI-ClientID": env["client"],
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
    """Index-first resolution from the bundled study_records.json snapshot.
    Returns a fully shaped record (same keys as _resolve_via_vql) when a snapshot
    is present -- resolved OR deliberately unresolved (e.g. study/site/country
    mismatch), so we DON'T fall through to a live VQL call. Returns None only when
    no snapshot file exists, letting _lookup fall back to VQL (old behavior)."""
    return resolve_from_snapshot(
        study=hints.get("study"),
        country=hints.get("country"),
        site=hints.get("site"),
    )


def _vql(query: str) -> list[dict]:
    """Run a VQL query with the shared service session; one retry on a stale session."""
    from lib import vault_filing  # lazy: snapshot resolution needs no Vault session
    env = _vault_env()

    def _do(session):
        r = vault_filing.http.post(
            f"{env['base']}/api/{VAULT_API_VERSION}/query",
            headers={"Authorization": session, "X-VaultAPI-ClientID": env["client"],
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


# ---------------------------------------------------------------------------
# SNAPSHOT RECORD MAP  -- study/country/site resolution from bundled JSON
# ---------------------------------------------------------------------------
def _load_record_map():
    """Load + cache data/study_records.json. None if absent."""
    if not _record_map_cache["loaded"]:
        _record_map_cache["data"] = _load_json(_RECORDS_SNAPSHOT)
        _record_map_cache["loaded"] = True
    return _record_map_cache["data"]


def _norm(v):
    return str(v).strip().lower() if v is not None else None


def _study_aliases(m) -> list[str]:
    al = [str(a) for a in (m.get("study_aliases") or [])]
    for k in ("study", "project_code", "study_name"):
        if m.get(k):
            al.append(str(m[k]))
    return [_norm(a) for a in al if a]


def _find_site(m, site_ref):
    """Return (site_key, site_record) matching the reference on key or aliases."""
    if not site_ref:
        return None, None
    target = _norm(site_ref)
    for k, v in (m.get("sites") or {}).items():
        aliases = [_norm(k)] + [_norm(a) for a in (v.get("aliases") or [])]
        if v.get("name"):
            aliases.append(_norm(v["name"]))
        if target in aliases:
            return k, v
    return None, None


def _country_aliases(countries, name) -> list[str]:
    rec = (countries or {}).get(name) or {}
    return [_norm(name)] + [_norm(a) for a in (rec.get("aliases") or [])]


def resolve_from_snapshot(study=None, country=None, site=None):
    """Resolve study/country/site references against the bundled record map.

    Returns the SAME shape as the live resolver:
      {study__v, study_country__v, site__v, study_name, study_country_name,
       site_name, all_resolved, parent_validated, resolved_via, notes}
    Fail-safe: a study/site/country mismatch, a missing record, or unpopulated
    __v IDs (template not yet synced) -> all_resolved False (the gate holds).
    Returns None ONLY when no snapshot file exists (caller may fall back to live).
    """
    m = _load_record_map()
    if not m:
        return None

    notes = []
    countries = m.get("countries") or {}

    # --- study match (single-study snapshot; a non-matching ref is a real signal)
    study_match = True
    if study is not None and _norm(study) not in _study_aliases(m):
        study_match = False
        notes.append(f"study '{study}' does not match snapshot study {m.get('study')!r}")

    # --- site
    site_key, site_rec = _find_site(m, site)
    if site and not site_rec:
        notes.append(f"site '{site}' not found in {m.get('study')!r}")

    # --- country: derive from the site; cross-check any provided country ref
    site_country = site_rec.get("country") if site_rec else None
    country_name = site_country or (country if (country in countries) else None)
    country_rec = countries.get(country_name) if country_name else None
    country_conflict = False
    if country and site_country and _norm(country) not in _country_aliases(countries, site_country):
        country_conflict = True
        notes.append(f"country '{country}' conflicts with site's country {site_country!r}")
    if country and not site_rec and not country_rec:
        notes.append(f"country '{country}' not found in {m.get('study')!r}")

    study__v = m.get("study__v")
    study_country__v = country_rec.get("study_country__v") if country_rec else None
    site__v = site_rec.get("site__v") if site_rec else None

    ids_present = bool(study__v and study_country__v and site__v)
    missing = []
    if (site_rec or country_rec) and not study__v:
        missing.append("study")
    if country_rec and not study_country__v:
        missing.append("country")
    if site_rec and not site__v:
        missing.append("site")
    if missing:
        notes.append("snapshot IDs not populated for " + ", ".join(missing)
                     + " -- run setup/sync_snapshot.py (or fill the __v fields)")

    no_conflict = study_match and not country_conflict
    all_resolved = bool(site_rec and country_rec and ids_present and no_conflict)
    # parent is validated by construction: the map encodes site -> country -> study.
    parent_validated = bool(site_rec and country_rec and ids_present and no_conflict)

    # --- SCOPE: does this document belong to THIS study/sites at all? This is a
    # SEPARATE axis from all_resolved. A doc can be in_scope but not resolvable
    # (e.g. IDs not synced yet), or affirmatively out_of_scope (names a study/site/
    # country that isn't part of this trial -> almost always misrouted). out_of_scope
    # must NOT be force-fit to the nearest site; it gets its own disposition.
    study_named = study is not None
    site_named = site is not None
    any_ref = bool(study_named or site_named or country)
    if study_named and not study_match:
        scope, scope_reason = "out_of_scope", "wrong_study"
    elif site_named and not site_rec:
        scope, scope_reason = "out_of_scope", "unrecognized_site"
    elif country_conflict:
        scope, scope_reason = "out_of_scope", "country_mismatch"
    elif not any_ref:
        scope, scope_reason = "indeterminate", "no_references"
    else:
        scope, scope_reason = "in_scope", None

    return {
        "study__v": study__v if all_resolved else (study__v or None),
        "study_country__v": study_country__v if country_rec else None,
        "site__v": site__v if site_rec else None,
        "study_name": m.get("study_name") or m.get("study"),
        "study_country_name": country_name,
        "site_name": site_rec.get("name") if site_rec else None,
        "all_resolved": all_resolved,
        "parent_validated": parent_validated,
        "scope": scope,
        "scope_reason": scope_reason,
        "snapshot_study": m.get("study"),
        "refs": {"study": study, "country": country, "site": site},
        "resolved_via": "snapshot",
        "notes": notes,
    }


def resolve(study=None, country=None, site=None):
    """Public resolver used by the resolve_record action: snapshot first, then a
    live VQL fallback if no snapshot file is present."""
    snap = resolve_from_snapshot(study=study, country=country, site=site)
    if snap is not None:
        return snap
    vql = _resolve_via_vql({"study": study, "country": country, "site": site})
    # VQL path can't judge in/out of scope the way the snapshot can; default safely.
    vql.setdefault("scope", "in_scope" if vql.get("all_resolved") else "indeterminate")
    vql.setdefault("scope_reason", None)
    vql.setdefault("refs", {"study": study, "country": country, "site": site})
    return vql


def snapshot_status() -> dict:
    """Lightweight diagnostics for which snapshots are active (no Vault calls)."""
    tax = _load_taxonomy_snapshot()
    m = _load_record_map()
    populated = False
    if m:
        sites = (m.get("sites") or {}).values()
        populated = bool(m.get("study__v")) and any(s.get("site__v") for s in sites)
    return {
        "taxonomy_snapshot": bool(tax),
        "taxonomy_count": len(tax) if tax else 0,
        "records_snapshot": bool(m),
        "records_study": (m or {}).get("study"),
        "records_sites": len((m or {}).get("sites") or {}),
        "records_ids_populated": populated,
    }
