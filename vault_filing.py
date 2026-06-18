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
    """Agent finding from the resolve_record action. Two modes:

    AUTHORITATIVE (preferred): if the agent passes the RAW references it read
    (study_ref / country_ref / site_ref), the gate RE-RESOLVES them against the
    snapshot itself and ignores the agent's claimed IDs/flags. This is what stops
    a doc being force-fit onto the nearest site, and it determines `scope`
    (in_scope / out_of_scope / indeterminate): an out-of-scope doc (wrong study, or
    a site/country not in this study) is held under its OWN disposition, not mixed
    into the fixable-exception queue.

    BACKWARD-COMPATIBLE: with no raw refs, trust the agent's all_resolved /
    parent_validated flags and infer scope. Either way fail-safe: not fully
    resolved -> held.
    """
    r = _d(finding)
    study_ref = r.get("study_ref") or r.get("study")
    country_ref = r.get("country_ref") or r.get("country")
    site_ref = r.get("site_ref") or r.get("site")

    if study_ref or country_ref or site_ref:
        from lib import vault_reference
        res = vault_reference.resolve(study=study_ref, country=country_ref, site=site_ref) or {}
        return {"check": "record_resolution",
                "study__v": res.get("study__v"), "study_country__v": res.get("study_country__v"),
                "site__v": res.get("site__v"),
                "study_name": res.get("study_name"),
                "study_country_name": res.get("study_country_name"),
                "site_name": res.get("site_name"),
                "parent_validated": bool(res.get("parent_validated")),
                "all_resolved": bool(res.get("all_resolved")),
                "scope": res.get("scope", "indeterminate"),
                "scope_reason": res.get("scope_reason"),
                "snapshot_study": res.get("snapshot_study"),
                "refs": {"study": study_ref, "country": country_ref, "site": site_ref},
                "notes": res.get("notes", []),
                "resolved_via": "gate_reresolved:" + str(res.get("resolved_via", "snapshot")),
                "ran_at": _now()}

    all_resolved = bool(r.get("all_resolved"))
    return {"check": "record_resolution",
            "study__v": r.get("study__v"), "study_country__v": r.get("study_country__v"),
            "site__v": r.get("site__v"),
            "study_name": r.get("study_name"), "study_country_name": r.get("study_country_name"),
            "site_name": r.get("site_name"),
            "parent_validated": bool(r.get("parent_validated")),   # fail-safe -> False
            "all_resolved": all_resolved,                          # fail-safe -> False
            "scope": r.get("scope") or ("in_scope" if all_resolved else "indeterminate"),
            "scope_reason": r.get("scope_reason"),
            "refs": {"study": r.get("study"), "country": r.get("country"), "site": r.get("site")},
            "resolved_via": r.get("resolved_via", "glean_agent + resolve_record"),
            "ran_at": _now()}
