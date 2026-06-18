"""
TMF-content, classification, and record-resolution VALIDATORS.

Glean-only architecture: the Glean agent does the *reasoning* and sends a
structured finding for each check. These functions do NOT call any model -- they
*validate* what the agent proposed and apply the deterministic enforcement that
must never live in the agent:

- validate_tmf:            normalize is_tmf / confidence (fail-safe: not TMF).
- validate_classification: VERIFY the agent's triple is a real member of the live
                           Vault taxonomy (grounding.in_taxonomy). A hallucinated /
                           invented triple is forced to confidence 0 so the gate
                           holds it. The agent can propose anything; this is what
                           makes the proposal safe.
- validate_resolution:     require study/country/site fully resolved + parent
                           validated (fail-safe: unresolved).

Fail-safe everywhere: a missing or malformed finding normalizes to the unsafe-side
default so the gate holds the document. Uncertainty never auto-files.
"""
from __future__ import annotations
from datetime import datetime, timezone

from lib import grounding


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d(finding) -> dict:
    return finding if isinstance(finding, dict) else {}


def validate_tmf(finding: dict) -> dict:
    """Agent finding: {is_tmf, zone, artifact, reason, confidence}."""
    r = _d(finding)
    return {"check": "tmf_content",
            "is_tmf": bool(r.get("is_tmf")),                 # fail-safe: missing -> False
            "zone": r.get("zone"), "artifact": r.get("artifact"),
            "reason": r.get("reason", "") or "",
            "confidence": int(r.get("confidence", 0) or 0),  # fail-safe: missing -> 0
            "ran_at": _now()}


def validate_classification(finding: dict, taxonomy: list) -> dict:
    """Agent finding: {type__v, subtype__v, classification__v, tmf_rm_v3, confidence,
    alternatives}. The triple MUST be a real taxonomy member or confidence is forced
    to 0 (ungrounded -> the gate holds it). This is the anti-hallucination guarantee:
    enforced here, deterministically, regardless of what the agent claimed."""
    r = _d(finding)
    triple = {"type__v": r.get("type__v"), "subtype__v": r.get("subtype__v"),
              "classification__v": r.get("classification__v")}
    member = grounding.in_taxonomy(triple, taxonomy)
    confidence = int(r.get("confidence", 0) or 0)
    if not member:
        confidence = 0  # ungrounded/hallucinated triple cannot file (gate holds it)
    return {"check": "classification",
            "type__v": triple["type__v"], "subtype__v": triple["subtype__v"],
            "classification__v": triple["classification__v"],
            "tmf_rm_v3": r.get("tmf_rm_v3"),
            "confidence": confidence,
            "alternatives": r.get("alternatives", []) or [],
            "grounding": {"taxonomy_member": member},     # explicit, audited
            "method": "agent_proposal + taxonomy_membership_check",
            "ran_at": _now()}


def validate_resolution(finding: dict) -> dict:
    """Agent finding (obtained by calling the resolve_record action against Vault):
    {study__v, study_country__v, site__v, *_name, all_resolved, parent_validated}.
    Completeness required; partial resolution -> held.

    HARDEN: re-query Vault here to confirm the IDs actually exist and the parent
    relationship holds, rather than trusting the agent's flags."""
    r = _d(finding)
    return {"check": "record_resolution",
            "study__v": r.get("study__v"), "study_country__v": r.get("study_country__v"),
            "site__v": r.get("site__v"),
            "study_name": r.get("study_name"), "study_country_name": r.get("study_country_name"),
            "site_name": r.get("site_name"),
            "parent_validated": bool(r.get("parent_validated")),   # fail-safe -> False
            "all_resolved": bool(r.get("all_resolved")),           # fail-safe -> False
            "resolved_via": r.get("resolved_via", "glean_agent + resolve_record"),
            "ran_at": _now()}
