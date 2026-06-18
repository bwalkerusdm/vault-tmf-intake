"""
gate_from_findings -- the deterministic gate for the Glean-only architecture.

The Glean agent does the reasoning and sends one structured finding per check.
This module does NOT call any model. It:
  1. validates/normalizes each finding (fail-safe defaults),
  2. enforces taxonomy membership on the proposed classification (anti-hallucination),
  3. runs the DETERMINISTIC GATE against configurable thresholds.

The gate is the entire barrier protecting Vault, so it is fail-safe: PASS only when
every check is clean AND confident AND resolved. Anything else -> exception. The
agent can recommend anything; only a grounded, above-threshold, PHI-clean,
ALCOA-passing, fully-resolved document files.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os

from lib.checks import phi_check, alcoa_check, content_checks

# Bump when the agent's instructions/output contract change -- stamped into each
# decision so a decision is reconstructable and an agent change is explicit.
GATE_VERSION = "tmf-gate-2.0-glean"

# Identifies the reasoning source in provenance (the Glean agent + version).
REASONING_SOURCE = os.environ.get("GLEAN_AGENT_ID", "glean_agent")

# ---- Quality policy (thresholds). For GxP these must be documented + QA-owned. ----
DEFAULT_THRESHOLDS = {
    "min_classification_confidence":
        int(os.environ.get("TMF_MIN_CLASSIFICATION_CONFIDENCE", "85")),
    "min_tmf_content_confidence":
        int(os.environ.get("TMF_MIN_TMF_CONTENT_CONFIDENCE", "80")),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checks_hash(checks: dict) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(checks, sort_keys=True).encode()).hexdigest()


def gate_from_findings(findings, taxonomy, document=None, thresholds=None):
    """
    findings : the agent's structured findings, a dict with keys
               {tmf_content, classification, record_resolution, phi_pii, alcoa_plus}
    taxonomy : live list from the get_taxonomy action / vault_reference
    document : optional {filename, box_file_id}
    Returns the aggregated record incl. status/flags/fileable -- same shape the
    disposition path consumes.
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    f = findings if isinstance(findings, dict) else {}

    checks = {
        "tmf_content": content_checks.validate_tmf(f.get("tmf_content")),
        "classification": content_checks.validate_classification(f.get("classification"), taxonomy),
        "record_resolution": content_checks.validate_resolution(f.get("record_resolution")),
        "phi_pii": phi_check.validate(f.get("phi_pii")),
        "alcoa_plus": alcoa_check.validate(f.get("alcoa_plus")),
    }

    status, flags = _derive_gate(checks, th)
    document = document or {}

    return {
        "filename": document.get("filename"),
        "box_file_id": document.get("box_file_id"),
        "checks": checks,
        "status": status,                        # clean | flagged | not_tmf | error
        "flags": flags,                          # list of {rule, detail, severity}
        "fileable": status == "clean",
        "checks_hash": _checks_hash(checks),
        "evaluated_at": _now(),
        # decision provenance -- reconstructable, carried into the audit event.
        # checks_hash stays the binding tie for the human QC attestation; this
        # documents the reasoning source + the deterministic policy that gated it.
        "provenance": {
            "thresholds": th,
            "reasoning_source": REASONING_SOURCE,     # the Glean agent (+version)
            "validated_by": "deterministic_gate",
            "gate_version": GATE_VERSION,
            "grounding": {
                "classification_taxonomy_member":
                    checks["classification"].get("grounding", {}).get("taxonomy_member"),
            },
        },
    }


def record_for_unprocessable(filename, box_file_id, reason):
    """Build a minimal record for a document the AGENT could not process at all
    (couldn't read/extract, timed out, unsupported/corrupt format). This is NOT a
    gate failure -- there were no findings to gate -- but it must still become a
    documented exception, never a silent skip. Held in Exceptions with a reason."""
    return {
        "filename": filename,
        "box_file_id": box_file_id,
        "checks": {},
        "status": "unprocessable",
        "flags": [{"rule": "processing_error",
                   "detail": (reason or "agent could not process this document"),
                   "severity": "blocking"}],
        "fileable": False,
        "checks_hash": "n/a:unprocessable",
        "evaluated_at": _now(),
        "provenance": {"reasoning_source": REASONING_SOURCE,
                       "validated_by": "deterministic_gate",
                       "gate_version": GATE_VERSION,
                       "note": "document could not be evaluated; held for manual handling"},
    }


def _derive_gate(checks, th):
    """DETERMINISTIC gate. Every non-clean outcome produces a flag with a reason."""
    flags = []

    tmf = checks["tmf_content"]
    if not tmf.get("is_tmf"):
        flags.append({"rule": "not_tmf_content",
                      "detail": tmf.get("reason", "not a TMF artifact"),
                      "severity": "blocking"})
    elif tmf.get("confidence", 0) < th["min_tmf_content_confidence"]:
        flags.append({"rule": "low_tmf_confidence",
                      "detail": f'TMF-content confidence {tmf.get("confidence")}% '
                                f'< {th["min_tmf_content_confidence"]}%',
                      "severity": "review"})

    phi = checks["phi_pii"]
    if phi.get("status") != "clear":
        for f in phi.get("findings", []):
            flags.append({"rule": "phi_finding",
                          "detail": f'{f.get("identifier_type")} ({f.get("belongs_to")}) '
                                    f'at {f.get("location")}',
                          "severity": "blocking"})

    alcoa = checks["alcoa_plus"]
    for attr in alcoa.get("flagged_attributes", []):
        basis = alcoa["attributes"].get(attr, {}).get("basis", "")
        flags.append({"rule": "alcoa_attribute_failed",
                      "detail": f'ALCOA {attr}: {basis}', "severity": "blocking"})

    cls = checks["classification"]
    if cls.get("grounding", {}).get("taxonomy_member") is False:
        flags.append({"rule": "classification_not_grounded",
                      "detail": "classification triple is not a member of the live "
                                "Vault taxonomy (rejected as possible hallucination)",
                      "severity": "blocking"})
    if cls.get("confidence", 0) < th["min_classification_confidence"]:
        flags.append({"rule": "low_classification_confidence",
                      "detail": f'classification confidence {cls.get("confidence")}% '
                                f'< {th["min_classification_confidence"]}%',
                      "severity": "blocking"})

    rec = checks["record_resolution"]
    if rec.get("scope") == "out_of_scope":
        flags.append({"rule": "out_of_scope_record",
                      "detail": _scope_detail(rec),
                      "severity": "blocking"})
    elif not rec.get("all_resolved"):
        flags.append({"rule": "unresolved_records",
                      "detail": "study/country/site not fully resolved",
                      "severity": "blocking"})
    elif not rec.get("parent_validated"):
        flags.append({"rule": "parent_unvalidated",
                      "detail": "site/study parent relationship not validated",
                      "severity": "blocking"})

    if any(f["rule"] == "not_tmf_content" for f in flags):
        status = "not_tmf"
    elif any(f["rule"] == "out_of_scope_record" for f in flags):
        status = "out_of_scope"
    elif any(f["severity"] == "blocking" for f in flags):
        status = "flagged"
    elif flags:
        status = "flagged"
    else:
        status = "clean"
    return status, flags


def _scope_detail(rec) -> str:
    """Human reason for an out-of-scope document, naming what didn't belong."""
    reason = rec.get("scope_reason")
    refs = rec.get("refs") or {}
    study = refs.get("study"); site = refs.get("site"); country = refs.get("country")
    this_study = rec.get("snapshot_study") or "this study"
    if reason == "wrong_study":
        return (f"document references study '{study}', which is not {this_study} "
                f"-- it appears to belong to a different study (likely misrouted)")
    if reason == "unrecognized_site":
        return (f"document references site '{site}', which is not a defined site of "
                f"{this_study} -- it does not map to any site in this trial")
    if reason == "country_mismatch":
        return (f"document's stated country '{country}' conflicts with the site's "
                f"country for {this_study} -- the record references don't line up")
    return "document references a study/site/country that is not part of this trial"


def summary_for_exception(record) -> dict:
    """Human + machine readable 'why it stayed in Box', written next to the file."""
    lines = [f"TMF intake evaluation - {record['filename']}",
             f"Evaluated: {record['evaluated_at']}",
             f"Status: {record['status'].upper()}", "", "Reasons it was not filed:"]
    for fl in record["flags"]:
        lines.append(f"  - [{fl['severity']}] {fl['detail']}")
    if not record["flags"]:
        lines.append("  - (no flags) ")
    if record["status"] == "not_tmf":
        lines += ["",
                  "This was assessed as NOT a TMF artifact, so it was not filed.",
                  "Action: REMOVE it from intake. Do not move it back to 1-Inbox -- it will "
                  "be rejected again. If you believe it IS a TMF document, correct it so it is "
                  "identifiable as a TMF artifact before re-submitting."]
    elif record["status"] == "out_of_scope":
        lines += ["",
                  "This document references a study, site, or country that is NOT part of this",
                  "trial, so it was NOT filed and has been set aside as OUT OF SCOPE (most likely",
                  "misrouted from another study).",
                  "Action: do NOT move it back to 1-Inbox -- it does not belong to this study. "
                  "Route it to the correct study or return it to the sender. If the site genuinely "
                  "belongs to this study, create the site record in Vault first, then re-submit."]
    elif record["status"] == "unprocessable":
        lines += ["",
                  "This document could NOT be processed automatically (see reason above) -- "
                  "e.g. it could not be read/extracted, timed out, or is an unsupported/corrupt "
                  "format.",
                  "Action: a person should review and handle it manually. If it's a fixable "
                  "issue (e.g. enable OCR, convert the format, re-export the file), correct it "
                  "and move the file back to 1-Inbox to retry."]
    else:
        lines += ["",
                  "Action: fix the issue(s) above and move this file back to 1-Inbox to re-run."]
    return {"human_readable": "\n".join(lines),
            "machine": {"status": record["status"], "flags": record["flags"],
                        "checks_hash": record["checks_hash"],
                        "evaluated_at": record["evaluated_at"]}}
