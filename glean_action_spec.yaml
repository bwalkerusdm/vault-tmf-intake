"""
Shared per-document disposition -- the ONE implementation of "file a clean doc /
hold a flagged doc, and write the audit event," used by BOTH the chunked endpoint
(file_chunk) and the bounded-pass orchestrator (run_intake_batch).

Consolidated here because this is the most consequential code in the system
(filing to Vault + writing the GxP audit record): one validated path is far safer
than two that can drift -- and they HAD drifted (two different
glean_evaluation_summary__c builders). One source now.

Design preserved from the originals:
  - Vault filing reuses lib.vault_filing (delegated create; reuse_session caches
    the delegated session across a batch under one delegator).
  - Box folder moves are NON-FATAL bookkeeping: a doc filed to Vault but not moved
    is still correctly filed.
  - QC starts NATIVELY in Vault (entry action + Dynamic Access Control keyed off
    study/country/site set in the metadata). No backend QC call here.
  - The audit event is written on BOTH paths (filed AND held): the decision NOT to
    file must itself be a documented event, never a silent exclusion.

`folders` is a dict {inbox, filed, exceptions} of Box folder ids, passed in so this
module reads no env and is unit-testable.
"""
from __future__ import annotations
import json

from lib import box_client, vault_filing, audit

EVAL_SUFFIX = ".eval.json"
LIFECYCLE = "clinical_doc_lifecycle__c"   # lands In Progress (pre-QC)


def eval_summary_text(record: dict) -> str:
    """Canonical human-readable glean_evaluation_summary__c. (Previously duplicated
    as _summary_text / _eval_text in the two endpoints, and they had diverged --
    this is now the single source, the richer of the two.)"""
    c = record["checks"]
    parts = [f'PHI: {c["phi_pii"]["status"]}',
             f'ALCOA+: {c["alcoa_plus"]["overall"]}',
             f'Confidence: {c["classification"]["confidence"]}%',
             f'TMF: {c["tmf_content"].get("zone") or c["tmf_content"].get("reason")}']
    flagged = c["alcoa_plus"].get("flagged_attributes")
    if flagged:
        parts.append("ALCOA flags: " + ", ".join(flagged))
    return " | ".join(parts)


def build_vault_metadata(record: dict) -> dict:
    """Single source of the Vault create metadata + glean_* evaluation fields."""
    checks = record["checks"]
    cls = checks["classification"]
    rec = checks["record_resolution"]
    return {
        "name__v": record["filename"].rsplit(".", 1)[0],
        "lifecycle__v": LIFECYCLE,
        "type__v": cls["type__v"], "subtype__v": cls["subtype__v"],
        "classification__v": cls["classification__v"],
        "study__v": rec["study__v"], "study_country__v": rec["study_country__v"],
        "site__v": rec["site__v"],
        "created_via_glean_agent__c": "yes__c",
        "glean_phi_status__c": checks["phi_pii"]["status"],
        "glean_alcoa_overall__c": checks["alcoa_plus"]["overall"],
        "glean_classification_confidence__c": cls["confidence"],
        "glean_evaluation_summary__c": eval_summary_text(record),
        "glean_checks_hash__c": record["checks_hash"],
    }


def process_one(item, eval_item, record, token, *, delegator_userid, folders) -> dict:
    """Dispatch one evaluated document. Returns a normalized outcome dict the
    caller adapts to its response shape / run summary:
      filed -> {action:"filed", filename, vault_document_id, vault_link}
      held  -> {action:"held",  filename, status, reasons[], flag_rules[]}
    """
    if record.get("status") == "clean":
        return file_clean(item, record, token,
                          delegator_userid=delegator_userid, folders=folders)
    return hold(item, eval_item, record, token, folders=folders)


def file_clean(item, record, token, *, delegator_userid, folders) -> dict:
    metadata = build_vault_metadata(record)
    file_bytes = box_client.download_file(item["id"], token)
    bytes_hash = audit.file_bytes_hash(file_bytes)
    doc_id = vault_filing.file_one_document(
        file_bytes, item["name"], metadata,
        delegator_userid=delegator_userid, reuse_session=True)
    # QC auto-starts in Vault (entry action + DAC off study/country/site). No call here.
    _safe_move(item["id"], folders["filed"], token,
               new_name=f'{item["name"]} [filed-{doc_id}]')
    _safe_move_companion(item["name"], folders["inbox"], folders["filed"], token)
    audit.log_disposition(
        record,
        {"action": "filed", "vault_document_id": doc_id,
         "vault_link": vault_filing.vault_link(doc_id),
         "lifecycle_state": "in_progress", "qc_kickoff": "vault_native"},
        source="box:1-Inbox", file_id=item["id"], bytes_hash=bytes_hash,
        actor={"type": "delegated", "delegator_userid": delegator_userid},
        ingested_at=item.get("created_at"))
    return {"action": "filed", "filename": item["name"],
            "vault_document_id": doc_id,
            "vault_link": vault_filing.vault_link(doc_id)}


def hold(item, eval_item, record, token, *, folders) -> dict:
    # local import avoids a lib->api top-level dependency / any import-order risk
    from lib.run_all_checks import summary_for_exception
    s = summary_for_exception(record)
    # not_tmf = wrong door (remove from intake); out_of_scope = right door, wrong
    # study/site (re-route, don't remediate here); flagged = fixable (fix + re-file).
    # Each routes to a dedicated folder IF configured; otherwise everything non-clean
    # lands in exceptions (backwards-compatible default).
    status = record.get("status")
    if status == "not_tmf" and folders.get("rejected"):
        dest, dest_label, kind = folders["rejected"], "box:0-Rejected", "rejected_not_tmf"
    elif status == "out_of_scope" and folders.get("out_of_scope"):
        dest, dest_label, kind = folders["out_of_scope"], "box:4-OutOfScope", "out_of_scope"
    else:
        dest, dest_label = folders["exceptions"], "box:3-Exceptions"
        kind = {"not_tmf": "rejected_not_tmf", "out_of_scope": "out_of_scope",
                "unprocessable": "unprocessable"}.get(status, "exception")
    box_client.upload_companion(dest, f'{item["name"]}.WHY.txt',
                                s["human_readable"], token)
    _safe_move(item["id"], dest, token)
    if eval_item:
        _safe_move(eval_item["id"], dest, token)
    reasons = [f["detail"] for f in record["flags"]]
    flag_rules = [f["rule"] for f in record["flags"]]
    audit.log_disposition(
        record,
        {"action": "held", "disposition_kind": kind,
         "exception_folder": dest_label, "reasons": reasons},
        source="box:1-Inbox", file_id=item["id"], bytes_hash=None,
        actor={"type": "process", "service_account": "glean_intake"},
        ingested_at=item.get("created_at"))
    return {"action": "held", "filename": item["name"], "disposition_kind": kind,
            "status": record["status"], "reasons": reasons, "flag_rules": flag_rules}


def _safe_move(file_id, dest, token, new_name=None):
    try:
        box_client.move_file(file_id, dest, token, new_name=new_name)
    except Exception:
        pass  # non-fatal: Vault filing already succeeded


def _safe_move_companion(doc_name, src, dest, token):
    try:
        for it in box_client.list_folder(src, token):
            if it["name"] == f"{doc_name}{EVAL_SUFFIX}":
                box_client.move_file(it["id"], dest, token)
    except Exception:
        pass
