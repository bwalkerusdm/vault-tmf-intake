"""
Audit trail for the TMF intake pipeline.

REGULATORY BASIS (verified against 21 CFR Part 11 / ALCOA+ guidance):
  "All AI-relevant actions (data ingestion, model runs, results generation, and
   any human interventions) should be auditable ... maintain an unbroken chain of
   input<->output metadata."
And critically, because FAILED documents never reach Vault, the decision to
NOT file must itself be a documented, auditable event -- "filtered before Vault"
must read as "remediated upstream WITH A RECORD," not "silently excluded."

This logger records one append-only event per document disposition, capturing:
  - WHAT was ingested (source, file id, hash of bytes)
  - WHAT each check returned (the full structured results)
  - WHAT the gate decided and WHY (status + flags)
  - WHAT happened to it (filed to Vault w/ doc id, or held in Box exceptions)
  - WHEN, and under WHOSE identity (the filing delegator / the process)
  - the checks_hash that ties a later human QC attestation to what was evaluated

ALCOA+ alignment of the audit record itself:
  Attributable - records delegator/process identity + check detector+version
  Legible      - structured JSON
  Contemporaneous - event written at disposition time, server timestamp
  Original / Accurate - append-only; never mutate a prior event
  Complete     - every document gets an event, including 'not filed'
  Enduring / Available - persisted to durable storage (see _persist)

STORAGE: for the demo this appends JSONL to a file / stdout. HARDEN -> write to
an append-only, access-controlled store (e.g. a dedicated audit table, S3 with
object-lock, or a write-once log service). The store must be tamper-evident for GxP.
"""
from __future__ import annotations
import os
import json
import hashlib
import logging
from datetime import datetime, timezone

logger = logging.getLogger("tmf.audit")

# Sink options:
#   'stdout'        -> print only (transient; default, dev)
#   'supabase'      -> durable, queryable table (demo/POC persistence)  <-- recommended
#   <path>.jsonl    -> local append (ephemeral on serverless)
# NOTE: none of these are tamper-evident. For PRODUCTION the GxP audit store must
# graduate to an append-only/WORM or cryptographically-verifiable store (e.g. S3
# Object Lock, or immudb) AND be validated. That is a documented production step,
# intentionally out of scope for the POC.
AUDIT_SINK = os.environ.get("TMF_AUDIT_SINK", "stdout")
PIPELINE_VERSION = os.environ.get("TMF_PIPELINE_VERSION", "pipeline-proof-0.1")

# Supabase config (only needed if AUDIT_SINK == 'supabase')
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_AUDIT_TABLE = os.environ.get("SUPABASE_AUDIT_TABLE", "tmf_audit_events")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_bytes_hash(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def build_event(record: dict, disposition: dict, *,
                source: str, file_id: str, bytes_hash: str | None,
                actor: dict, ingested_at: str | None = None) -> dict:
    """Compose one audit event for a document's disposition.

    record       : the run_all_checks output (checks, status, flags, checks_hash)
    disposition  : {action: 'filed'|'held'|'error',
                    vault_document_id?, vault_link?, exception_folder?, error?}
    source       : e.g. 'box:1-Inbox'
    file_id      : Box file id (the ingested artifact)
    bytes_hash   : sha256 of the source bytes (input integrity)
    actor        : {type:'delegated'|'integration'|'process',
                    delegator_userid?, service_account?}
    """
    return {
        "event": "tmf_intake_disposition",
        "event_id": _event_id(file_id, record.get("checks_hash", "")),
        "pipeline_version": PIPELINE_VERSION,
        "occurred_at": _now(),                       # contemporaneous
        # --- input (data ingestion) ---
        "source": source,
        "source_file_id": file_id,
        "source_bytes_hash": bytes_hash,             # input integrity
        "filename": record.get("filename"),
        # --- timing (cycle-time / timeliness metrics) ---
        "ingested_at": ingested_at,                  # when the doc entered the inbox (best-effort)
        "evaluated_at": record.get("evaluated_at"),  # when the checks ran
        #   (disposition time is occurred_at, above)
        # --- AI results (model runs / results generation) ---
        "checks": record.get("checks"),              # full structured results
        "checks_hash": record.get("checks_hash"),    # ties to QC attestation later
        "gate_status": record.get("status"),
        "flags": record.get("flags"),                # WHY, with severities
        # --- disposition (what happened) ---
        "disposition": disposition,                  # filed / held / error + details
        # --- attribution ---
        "actor": actor,
        # --- provenance of the judgment ---
        "detectors": _detector_provenance(record.get("checks", {})),
        "provenance": record.get("provenance"),      # thresholds / model / prompt_version / grounding
        # --- record identifiers surfaced for querying (sponsor-scoped reads,
        #     study-level TMF health checks). Derived from record_resolution. ---
        "study__v": _rr(record).get("study__v"),
        "study_country__v": _rr(record).get("study_country__v"),
        "site__v": _rr(record).get("site__v"),
    }


def _rr(record: dict) -> dict:
    return (record.get("checks") or {}).get("record_resolution") or {}


def _event_id(file_id, checks_hash) -> str:
    return hashlib.sha256(f"{file_id}|{checks_hash}|{_now()}".encode()).hexdigest()[:24]


def _detector_provenance(checks: dict) -> dict:
    return {
        "phi": (checks.get("phi_pii") or {}).get("detector"),
        "phi_version": (checks.get("phi_pii") or {}).get("detector_version"),
        "alcoa_method": (checks.get("alcoa_plus") or {}).get("method"),
        "classification_method": (checks.get("classification") or {}).get("method"),
    }


def record_event(event: dict) -> None:
    """Append the event to the audit sink. Append-only; never update/delete."""
    line = json.dumps(event, sort_keys=True)
    if AUDIT_SINK == "supabase":
        _write_supabase(event, line)
    elif AUDIT_SINK == "stdout":
        logger.info("AUDIT %s", line)
        print("AUDIT " + line)
    else:
        # local append -- ephemeral on serverless; dev only, NOT tamper-evident.
        with open(AUDIT_SINK, "a") as f:
            f.write(line + "\n")


def _write_supabase(event: dict, line: str) -> None:
    """INSERT one audit row into Supabase via the REST API. Insert-only by design
    (the table grants/RLS should forbid UPDATE/DELETE -- see setup SQL). Durable +
    queryable for the POC; NOT tamper-evident (production hardening is separate)."""
    import urllib.request
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_AUDIT_TABLE}"
    body = json.dumps({
        "event_id": event["event_id"],
        "occurred_at": event["occurred_at"],
        "filename": event.get("filename"),
        "source_file_id": event.get("source_file_id"),
        "gate_status": event.get("gate_status"),
        "disposition_action": (event.get("disposition") or {}).get("action"),
        "study__v": event.get("study__v"),
        "study_country__v": event.get("study_country__v"),
        "site__v": event.get("site__v"),
        "event": event,                       # full event as JSONB
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        })
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        # Audit write failure must be visible, not swallowed. Log loudly; in
        # production this should also alert -- a missing audit event is a finding.
        logger.error("AUDIT WRITE FAILED (event still printed below): %s", e)
        print("AUDIT_WRITE_FAILED " + line)


def log_disposition(record, disposition, *, source, file_id, bytes_hash, actor,
                    ingested_at=None):
    """Build + persist in one call. Returns the event (e.g. to echo in a response)."""
    ev = build_event(record, disposition, source=source, file_id=file_id,
                     bytes_hash=bytes_hash, actor=actor, ingested_at=ingested_at)
    record_event(ev)
    return ev
