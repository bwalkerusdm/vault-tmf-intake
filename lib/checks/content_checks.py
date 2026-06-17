"""
TMF-content, classification, and record-resolution check services.

- tmf_content: is this a TMF artifact at all? -> zone / not-TMF
- classify:    the Vault type/subtype/classification triple + confidence + alternatives
               (HARDEN/INTEGRATE: uses the live taxonomy from classifications.py;
                here it accepts a `taxonomy` lookup and an llm_call to match)
- resolve_records: study/country/site IDs with parent-relationship validation
               (INTEGRATE: uses the Glean index or live VQL via `resolver`)

Fail-safe everywhere: uncertainty -> low confidence / unresolved -> routed to review.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from lib import grounding


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_json(raw: str, default):
    try:
        return json.loads(raw.strip().strip("`").replace("json", "", 1).strip())
    except Exception:
        return default


def tmf_content(document: dict, llm_call) -> dict:
    text = document.get("text", "") or ""
    prompt = (
        "Decide whether this document is a Trial Master File (TMF) artifact per "
        "the TMF Reference Model. If yes, name the zone and artifact. If it is "
        "out-of-scope (expense report, personal email, marketing, junk, duplicate), "
        "say so with a reason. Respond ONLY with JSON: "
        "{is_tmf: bool, zone: str|null, artifact: str|null, reason: str, confidence: int(0-100)}.\n\n"
        + grounding.INJECTION_GUARD_INSTRUCTION + "\n\n"
        + grounding.wrap_untrusted(text)
    )
    r = grounding.safe_json(llm_call(prompt),
                    {"is_tmf": False, "zone": None, "artifact": None,
                     "reason": "could not determine (fail-safe)", "confidence": 0})
    if not isinstance(r, dict):
        r = {"is_tmf": False, "confidence": 0,
             "reason": "unparseable model output (fail-safe)"}
    return {"check": "tmf_content", "is_tmf": bool(r.get("is_tmf")),
            "zone": r.get("zone"), "artifact": r.get("artifact"),
            "reason": r.get("reason", ""), "confidence": int(r.get("confidence", 0) or 0),
            "ran_at": _now()}


def classify(document: dict, taxonomy: list[dict], llm_call) -> dict:
    """`taxonomy` = live list of valid {type__v, subtype__v, classification__v, label,
    tmf_rm_v3} from vault_reference. The match is constrained to real entries, and
    -- crucially -- the model's returned triple is VERIFIED to be a real taxonomy
    member here (grounding.in_taxonomy). A hallucinated/invented triple is forced to
    confidence 0 and flagged not-grounded, so the deterministic gate holds it. The
    prompt only *requests* "do not invent"; this code *enforces* it."""
    text = document.get("text", "") or ""
    # keep the prompt grounded in the real taxonomy so it cannot invent triples
    catalog = json.dumps(taxonomy[:400])  # cap for prompt size; full list is cached upstream
    prompt = (
        "Choose the single best matching classification for this document from the "
        "provided Vault taxonomy ONLY (do not invent values). Give your confidence "
        "(0-100) and up to 2 alternatives with their confidence. Respond ONLY with "
        "JSON: {type__v, subtype__v, classification__v, tmf_rm_v3, confidence, "
        "alternatives:[{classification__v, confidence}]}.\n\n"
        + grounding.ABSTAIN_INSTRUCTION + "\n"
        + grounding.INJECTION_GUARD_INSTRUCTION + "\n\n"
        f"TAXONOMY:\n{catalog}\n\n"
        + grounding.wrap_untrusted(text)
    )
    r = grounding.safe_json(llm_call(prompt), {}) or {}
    if not isinstance(r, dict):
        r = {}
    triple = {"type__v": r.get("type__v"), "subtype__v": r.get("subtype__v"),
              "classification__v": r.get("classification__v")}
    # GROUNDING ENFORCEMENT: the returned triple must be a real taxonomy member.
    member = grounding.in_taxonomy(triple, taxonomy)
    confidence = int(r.get("confidence", 0) or 0)
    if not member:
        confidence = 0  # ungrounded/hallucinated triple cannot file (gate holds it)
    return {"check": "classification",
            "type__v": triple["type__v"], "subtype__v": triple["subtype__v"],
            "classification__v": triple["classification__v"],
            "tmf_rm_v3": r.get("tmf_rm_v3"),
            "confidence": confidence,                    # 0 if unparsed or ungrounded -> fails gate
            "alternatives": r.get("alternatives", []),
            "grounding": {"taxonomy_member": member},    # explicit, audited
            "method": "live_taxonomy + llm_match + taxonomy_membership_check",
            "ran_at": _now()}


def resolve_records(document: dict, resolver) -> dict:
    """`resolver(text)->dict` encapsulates Glean-index / live-VQL lookup and returns
    {study__v, study_country__v, site__v, *_name, parent_validated, all_resolved}.
    Kept as an injected dependency so it reuses your query.py / index logic unchanged."""
    text = document.get("text", "") or ""
    try:
        r = resolver(text) or {}
    except Exception as e:
        r = {"all_resolved": False, "error": str(e)[:120]}
    return {"check": "record_resolution",
            "study__v": r.get("study__v"), "study_country__v": r.get("study_country__v"),
            "site__v": r.get("site__v"),
            "study_name": r.get("study_name"), "study_country_name": r.get("study_country_name"),
            "site_name": r.get("site_name"),
            "parent_validated": bool(r.get("parent_validated")),
            "all_resolved": bool(r.get("all_resolved")),
            "resolved_via": r.get("resolved_via", "unknown"),
            "ran_at": _now()}
