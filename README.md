# Vault TMF Bulk Intake — lean demo build

Box **1-Inbox** → 5 checks → deterministic gate → **clean** docs filed to Vault eTMF
(In Progress, staged pre-QC, via delegated session) and moved to **2-Filed**;
**flagged** docs held in **3-Exceptions** with a `.WHY.txt` explaining why. Every
disposition emits a structured audit event to **stdout / Vercel function logs** —
no database required for this build.

**Core principle:** AI *recommends*, the human renders the binding QC verdict and
e-signature **in Vault**. Nothing here auto-approves — clean docs land in In Progress
for human QC, exactly as a person would stage them.

This is the standalone **bulk lane**. The single-document **interactive** flow lives
in the separate `vault-backend` project and is unaffected — both stay available.

## Not in this build (addable later)
The Supabase audit trail and the drift / sponsor **metrics** endpoints are
intentionally omitted to keep the demo lean. Dispositions still emit an audit event
to stdout (Vercel logs). To add the durable trail + metrics later: stand up Supabase,
set `TMF_AUDIT_SINK=supabase` + `SUPABASE_*`, and add back `lib/audit_read.py` and the
`api/etmf/audit/*` endpoints.

## Structure
```
lib/                 shared code — NOT deployed as functions
  run_all_checks.py  the gate: runs the 5 checks, aggregates, stamps provenance
  checks/            phi_check, alcoa_check, content_checks
  vault_filing.py    delegated Vault filing (auth + create)
  vault_reference.py cached taxonomy + study/site resolver  ← verify against your Vault
  llm.py             pinned-model, temperature-0 LLM caller (fail-safe → held)
  grounding.py       deterministic anti-hallucination layer (abstain-over-guess)
  box_client.py      Box list / download / move / upload-companion
  disposition.py     the ONE file/hold/metadata/audit path
  audit.py           audit event builder + sink (stdout by default)
api/etmf/batch/      the deployed endpoints (one function per file)
  evaluate_chunk.py    evaluate a chunk of Inbox docs (writes .eval.json companions)
  file_chunk.py        file/hold a chunk of evaluated docs
  run_intake_batch.py  "process now": evaluate + file/hold in one bounded pass
```

## Endpoints (all POST, bearer auth = `GLEAN_BEARER_TOKEN`)
- `/api/etmf/batch/run_intake_batch`  `{"chunk":6}`  ← simplest single trigger
- `/api/etmf/batch/evaluate_chunk`    `{"chunk":3}`
- `/api/etmf/batch/file_chunk`        `{"chunk":6}`

## Deploy config
- `vercel.json` — Fluid Compute on; every function 60s (Hobby ceiling); `includeFiles`
  ships `lib/` inside each function bundle. Endpoints also add the repo root to
  `sys.path` at import so `from lib import …` resolves on Vercel.
- `requirements.txt` — `requests` only (everything else is standard library).
- Hobby: set `INTAKE_MAX_SECONDS=50`; keep chunks small (`evaluate_chunk` ~3,
  `file_chunk` ~6). Small chunks = more iterations, not lost work. Pro later:
  `maxDuration` 300 + `INTAKE_MAX_SECONDS` ~280.

## Verify-against-config seams (the only environment-specific knobs)
In `lib/vault_reference.py`: the taxonomy fetch shape, the study/site VQL resolver
(`_extract_hints` + object/field names via `TMF_*_OBJECT`), and the `tmf_rm_v3`
mapping. Classification and record resolution won't return real results until these
match your Vault — your proven `query.py` / `classifications.py` in the other repo
are the reference for what those calls should look like. Also confirm `TMF_LLM_MODEL`.

See **START_HERE.md** for the step-by-step first deploy.
