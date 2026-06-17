"""
run_all_checks -- the shared core for BOTH the bulk pipeline and (later) the
interactive Evaluate Document action.

Runs the five check services on one document, aggregates into one structured
record, then applies the DETERMINISTIC GATE against configurable thresholds.

The gate is the entire barrier protecting Vault, so it is fail-safe:
PASS only when every check is clean AND confident AND resolved. Anything else
-> exception (stays in Box with the reason). Uncertainty never auto-files.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json

from lib.checks import phi_check, alcoa_check, content_checks
from lib.llm import model_id

# Bump when any check prompt changes -- stamped into each decision so a decision
# is reconstructable and a prompt change is an explicit, revalidated event.
PROMPT_VERSION = "tmf-checks-1.0"

# ---- Quality policy (thresholds). For GxP these must be documented + QA-owned. ----
# Three ways to set, in increasing precedence:
#   1. these defaults
#   2. env vars (operational tuning without a code change)
#   3. a `thresholds` dict passed to run_all_checks (per-run override)
import os

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


def run_all_checks(document, llm_call, taxonomy, resolver, thresholds=None):
    """
    document : {filename, text, box_file_id, ...}
    llm_call : fn(prompt)->str   (focused LLM used by the stubbed checks)
    taxonomy : live list from classifications.py
    resolver : fn(text)->record dict (your index / VQL logic)
    Returns the aggregated record incl. status/flags/fileable.
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    checks = {
        "tmf_content": content_checks.tmf_content(document, llm_call),
        "classification": content_checks.classify(document, taxonomy, llm_call),
        "record_resolution": content_checks.resolve_records(document, resolver),
        "phi_pii": phi_check.run(document, llm_call),
        "alcoa_plus": alcoa_check.run(document, llm_call),
    }

    status, flags = _derive_gate(checks, th)

    return {
        "filename": document.get("filename"),
        "box_file_id": document.get("box_file_id"),
        "checks": checks,
        "status": status,                       # clean | flagged | not_tmf | error
        "flags": flags,                          # list of {rule, detail, severity}
        "fileable": status == "clean",
        "checks_hash": _checks_hash(checks),
        "evaluated_at": _now(),
        # --- decision provenance: makes the gate decision reconstructable and is
        # carried into the audit event (Transparent + Traceable). checks_hash
        # stays the binding tie for the human QC attestation; this documents the
        # policy/model context that produced the decision. ---
        "provenance": {
            "thresholds": th,
            "model": model_id(),
            "prompt_version": PROMPT_VERSION,
            "grounding": {
                "classification_taxonomy_member":
                    checks["classification"].get("grounding", {}).get("taxonomy_member"),
            },
        },
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
    if not rec.get("all_resolved"):
        flags.append({"rule": "unresolved_records",
                      "detail": "study/country/site not fully resolved",
                      "severity": "blocking"})
    elif not rec.get("parent_validated"):
        flags.append({"rule": "parent_unvalidated",
                      "detail": "site/study parent relationship not validated",
                      "severity": "blocking"})

    # status from flags
    if any(f["rule"] == "not_tmf_content" for f in flags):
        status = "not_tmf"
    elif any(f["severity"] == "blocking" for f in flags):
        status = "flagged"
    elif flags:
        status = "flagged"
    else:
        status = "clean"
    return status, flags


def summary_for_exception(record) -> dict:
    """Human + machine readable 'why it stayed in Box', written next to the file."""
    lines = [f"TMF intake evaluation - {record['filename']}",
             f"Evaluated: {record['evaluated_at']}",
             f"Status: {record['status'].upper()}", "", "Reasons it was not filed:"]
    for fl in record["flags"]:
        lines.append(f"  - [{fl['severity']}] {fl['detail']}")
    if not record["flags"]:
        lines.append("  - (no flags) ")
    lines += ["", "Fix the issue(s) above and move this file back to 1-Inbox to re-run."]
    return {"human_readable": "\n".join(lines),
            "machine": {"status": record["status"], "flags": record["flags"],
                        "checks_hash": record["checks_hash"],
                        "evaluated_at": record["evaluated_at"]}}
