# Vault TMF Bulk Intake — Glean-only build

A **Glean agent** orchestrates TMF intake end to end; this repo provides the thin
**custom actions** it calls. There is **no Anthropic key and no model call in the
backend** — the agent does the reasoning, and a deterministic gate enforces the rules.

Flow: the agent reads each document in Box **1-Inbox** (via Glean), proposes the five
findings, and calls **`file_or_flag`**, which re-validates everything deterministically
and either files clean docs to Vault eTMF (In Progress, delegated) + moves them to
**2-Filed**, or holds flagged docs in **3-Exceptions** (or **0-Rejected** for non-TMF)
with a `.WHY.txt`. Every disposition is logged to stdout / Vercel logs.

**Backbone:** AI *recommends* → a deterministic gate *enforces* → the human renders the
binding QC verdict and e-signature **in Vault**. The agent can recommend anything;
only a grounded, above-threshold, PHI-clean, ALCOA-passing, fully-resolved document
files.

## Architecture
```
Glean agent (reasoning)              Custom actions (deterministic I/O + gate)
──────────────────────               ─────────────────────────────────────────
reads each Inbox doc      ─calls─▶    list_inbox      (Box: what's waiting)        [this repo]
proposes the 5 findings   ─calls─▶    get_taxonomy    (Vault: the closed set)      [reuse classifications.py]
                          ─calls─▶    resolve_record  (Vault: study/site IDs)      [reuse query.py]
hands findings to gate    ─calls─▶    file_or_flag    (THE GATE)                   [this repo]
                                          ├─ clean  → Vault In Progress + 2-Filed
                                          └─ flagged→ 3-Exceptions / 0-Rejected + .WHY.txt
```

## The five checks — split
The agent *proposes* each finding; `file_or_flag` *enforces* deterministically:
- **tmf_content** — agent: is_tmf/zone/confidence; gate: not-TMF → held (`not_tmf`).
- **classification** — agent: type/subtype/classification (from `get_taxonomy`) + confidence;
  gate: triple must be a live-taxonomy member (else confidence forced to 0 → held) and ≥ threshold.
- **phi_pii** — agent: subject vs personnel/institutional identifiers; gate: any subject finding → held.
- **alcoa_plus** — agent: per-attribute pass/flag + basis; gate: any flag → held.
- **record_resolution** — agent (via `resolve_record`): study/country/site IDs; gate: must be fully resolved + parent-validated.

Fail-safe throughout: a missing/malformed finding normalizes to the unsafe-side default → held.

## Endpoints (this repo) — POST, bearer = `GLEAN_BEARER_TOKEN`
- `/api/etmf/actions/list_inbox`  `{}` → `{documents:[{box_file_id, filename}], count}`
- `/api/etmf/actions/file_or_flag`  `{box_file_id, filename, findings:{...}}` →
  `{disposition, status, checks_hash, vault_document_id? | reasons[]+flag_rules[]}`
- `/api/etmf/actions/expand_archive`  `{box_file_id, filename}` → unpacks a
  .zip/.tar/.gz bundle and re-uploads each member into 1-Inbox as its own document
  (so each is filed separately); moves the archive out. `list_inbox` flags archives
  with `is_archive` so the agent knows to expand before filing.

`get_taxonomy` and `resolve_record` are your existing `classifications.py` / `query.py`
actions — point the agent at those; no need to rebuild them here.

## Structure
```
lib/   run_all_checks.py (gate_from_findings + the deterministic gate)
       checks/  validators of the agent's findings (phi, alcoa, content)
       grounding.py (taxonomy-membership enforcement), vault_reference.py (taxonomy)
       vault_filing.py, box_client.py, disposition.py, audit.py (stdout), archive.py
api/etmf/actions/  list_inbox.py, file_or_flag.py, expand_archive.py   (deployed functions)
```

## Provenance / audit
Each decision records `reasoning_source` (the Glean agent + version, via `GLEAN_AGENT_ID`),
`validated_by: deterministic_gate`, the thresholds, the grounding result, and the
`checks_hash` the human signs against in Vault.

## Verify-against-config seams
`lib/vault_reference.py`: the taxonomy fetch shape (used by `file_or_flag` to enforce
classification membership). Your proven `classifications.py` is the reference. Set
`BULK_DELEGATOR_USER_ID`. Record resolution is done by the agent via your `query.py`.

See **START_HERE.md** for deployment + building the Glean agent, and
**GLEAN_AGENT_DESIGN.md** for the full mapping.
