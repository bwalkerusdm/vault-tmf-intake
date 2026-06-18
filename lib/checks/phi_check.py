"""
PHI / PII VALIDATOR.

Glean-only architecture: the Glean agent detects identifiers and classifies whose
they are (subject vs. personnel/institutional), per the rule that subject
identifiers are findings but study-personnel / institutional identifiers are
appropriate in a TMF. This function validates the agent's finding and enforces the
fail-safe.

Agent finding shape: {status: "clear"|"findings",
                       findings: [{identifier_type, belongs_to, location}]}

Fail-safe: anything other than an explicit, well-formed "clear" -> status "findings"
(held). Uncertainty never auto-files.
"""
from __future__ import annotations
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate(finding: dict) -> dict:
    r = finding if isinstance(finding, dict) else {}
    status = r.get("status")
    findings = r.get("findings") if isinstance(r.get("findings"), list) else []
    # keep only subject-owned identifiers as findings (personnel/institutional are OK)
    subject_findings = [
        {"identifier_type": f.get("identifier_type", "unspecified"),
         "belongs_to": f.get("belongs_to", "subject"),
         "location": f.get("location", "n/a")}
        for f in findings if isinstance(f, dict) and f.get("belongs_to") == "subject"
    ]
    # fail-safe: only an explicit "clear" with no subject findings is clear
    if status == "clear" and not subject_findings:
        out_status = "clear"
    elif status == "findings":
        out_status = "findings"
    elif subject_findings:
        out_status = "findings"
    else:
        # malformed / missing / unknown status -> fail-safe to findings
        out_status = "findings"
        if not subject_findings:
            subject_findings = [{"identifier_type": "uncertain", "belongs_to": "subject",
                                 "location": "n/a",
                                 "note": "PHI finding missing/unparseable - held (fail-safe)"}]
    return {"check": "phi_pii", "status": out_status, "findings": subject_findings,
            "detector": "glean_agent", "ran_at": _now()}
