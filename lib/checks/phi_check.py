"""
PHI / PII check service.

THOROUGHNESS REQUIREMENT: detect all 18 HIPAA identifiers, AND distinguish
whose identifier it is -- subject identifiers (research participant) are
findings; study-personnel / institutional identifiers (PI, nurse, IRB contact,
sponsor) are appropriate in a TMF and are NOT findings.

PIPELINE-FIRST IMPLEMENTATION:
  Stage 1 (detect):   currently a focused LLM extraction. HARDEN -> swap for a
                      purpose-built detector (AWS Comprehend Medical, Google DLP,
                      or Presidio). Keep the SAME return shape so nothing downstream
                      changes.
  Stage 2 (classify): subject vs. personnel/institutional. Stays as focused logic
                      (LLM or rules) even after hardening -- this is the TMF-specific
                      part a generic detector does not do.

Fail-safe: any uncertainty -> findings (route to exceptions, never auto-file).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from lib import grounding

# The 18 HIPAA identifier categories, for reference / prompt grounding.
HIPAA_IDENTIFIERS = [
    "name", "geographic_subdivision_smaller_than_state",
    "dates_related_to_individual", "phone_number", "fax_number",
    "email_address", "ssn", "medical_record_number",
    "health_plan_beneficiary_number", "account_number",
    "certificate_or_license_number", "vehicle_identifier",
    "device_identifier_or_serial", "url", "ip_address",
    "biometric_identifier", "full_face_photo", "other_unique_identifier",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _detect_identifiers(text: str, llm_call) -> list[dict]:
    """STAGE 1 -- detect every potential identifier with type + location.

    HARDEN: replace the body with a Comprehend Medical / DLP / Presidio call.
    Must return: [{identifier_type, location, excerpt_redacted, raw_excerpt}]
    (raw_excerpt is used only by stage 2 to judge ownership; it is NOT persisted.)
    """
    prompt = (
        "You are a PHI/PII detector for clinical trial documents. "
        "Find EVERY occurrence of any of these HIPAA identifier types: "
        + ", ".join(HIPAA_IDENTIFIERS) + ". "
        "For each, return type, a page/line location if determinable, and the "
        "exact text. Respond ONLY with a JSON array of objects with keys "
        "identifier_type, location, raw_excerpt. No prose, no markdown.\n\n"
        + grounding.INJECTION_GUARD_INSTRUCTION + "\n\n"
        + grounding.wrap_untrusted(text, limit=18000)
    )
    raw = llm_call(prompt)
    try:
        items = json.loads(raw.strip().strip("`").replace("json", "", 1).strip())
    except Exception:
        # fail-safe: if we cannot parse detection, treat as uncertain (handled upstream)
        return [{"identifier_type": "unparseable_detection", "location": "n/a",
                 "raw_excerpt": "", "excerpt_redacted": ""}]
    for it in items:
        it.setdefault("location", "n/a")
        it["excerpt_redacted"] = _redact(it.get("raw_excerpt", ""))
    return items


def _redact(s: str) -> str:
    if not s:
        return ""
    # keep length/shape, hide content -- never persist raw PHI
    return "".join("█" if ch.isalnum() else ch for ch in s)[:40]


def _classify_ownership(identifiers: list[dict], text: str, llm_call) -> list[dict]:
    """STAGE 2 -- for each detected identifier decide subject vs personnel/institutional.

    This stays as focused logic even after hardening the detector, because a
    generic detector does not know that the PI's name is fine but a subject's is not.
    """
    if not identifiers:
        return []
    catalog = [{"i": i, "identifier_type": it["identifier_type"],
                "raw_excerpt": it.get("raw_excerpt", ""), "location": it["location"]}
               for i, it in enumerate(identifiers)]
    prompt = (
        "In a clinical trial document, classify each identifier by WHOSE it is:\n"
        "  'subject'        = the research participant (a finding -- must be redacted)\n"
        "  'personnel'      = study staff: PI, sub-I, coordinator, nurse, monitor\n"
        "  'institutional'  = IRB/IEC, sponsor, site, vendor contact info\n"
        "Subjects referenced ONLY by a subject number are NOT a finding.\n"
        "Personnel and institutional identifiers are APPROPRIATE in a TMF.\n"
        "If uncertain about an identifier, classify it 'subject' (fail-safe).\n"
        "Respond ONLY with a JSON array of {i, belongs_to}.\n\n"
        + grounding.INJECTION_GUARD_INSTRUCTION + "\n\n"
        f"IDENTIFIERS:\n{json.dumps(catalog)}\n\n"
        + grounding.wrap_untrusted(text, limit=8000)
    )
    raw = llm_call(prompt)
    try:
        verdicts = {v["i"]: v["belongs_to"] for v in
                    json.loads(raw.strip().strip("`").replace("json", "", 1).strip())}
    except Exception:
        verdicts = {i: "subject" for i in range(len(identifiers))}  # fail-safe
    for i, it in enumerate(identifiers):
        it["belongs_to"] = verdicts.get(i, "subject")
    return identifiers


def run(document: dict, llm_call) -> dict:
    """Entry point. `document` has at least {text}. `llm_call(prompt)->str`.

    Returns the structured phi_pii result. status='findings' is blocking upstream.
    """
    text = document.get("text", "") or ""
    detected = _detect_identifiers(text, llm_call)

    # parse failure or detector uncertainty -> fail-safe to findings
    if any(d["identifier_type"] in ("unparseable_detection",) for d in detected):
        return {"check": "phi_pii", "status": "findings",
                "findings": [{"identifier_type": "uncertain", "belongs_to": "subject",
                              "location": "n/a", "excerpt_redacted": "",
                              "note": "detection uncertain - routed to review (fail-safe)"}],
                "detector": "llm_stub", "detector_version": "pipeline-proof",
                "ran_at": _now()}

    classified = _classify_ownership(detected, text, llm_call)
    findings = [
        {"identifier_type": d["identifier_type"], "belongs_to": d["belongs_to"],
         "location": d["location"], "excerpt_redacted": d["excerpt_redacted"]}
        for d in classified if d.get("belongs_to") == "subject"
    ]
    return {
        "check": "phi_pii",
        "status": "findings" if findings else "clear",
        "findings": findings,
        "detector": "llm_stub",            # HARDEN -> 'comprehend_medical' etc.
        "detector_version": "pipeline-proof",
        "ran_at": _now(),
    }
