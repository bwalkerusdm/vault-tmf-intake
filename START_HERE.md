# START HERE — Glean-only deploy (new project, Hobby, no Anthropic)

Glean agent reads each doc and proposes findings → `file_or_flag` enforces the gate
and files/holds. No database, **no Anthropic key**.

Accounts: **Vercel** (new project, Hobby), **Box** (folders + token), **Veeva Vault**,
**Glean** (the agent + actions). No Anthropic.

## 1. New repo + new Vercel project
Put these files in a new git repo (keep paths); import as a new Vercel project,
separate from `vault-backend`. Each `api/**/*.py` becomes an endpoint; `vercel.json`
caps them at 60s and ships `lib/`.

## 2. Environment variables (NO Anthropic, NO Supabase)
**Required:** `VAULT_BASE_URL`, `VAULT_CLIENT_ID`, `VAULT_USERNAME`, `VAULT_PASSWORD`;
`BOX_TOKEN` (or `BOX_DEVELOPER_TOKEN`), `BOX_INBOX_FOLDER_ID`, `BOX_FILED_FOLDER_ID`,
`BOX_EXCEPTIONS_FOLDER_ID`; `GLEAN_BEARER_TOKEN` (shared secret; reuse in the agent's actions).
**Defaulted / optional:** `BULK_DELEGATOR_USER_ID` (your Vault user id), thresholds
(`TMF_MIN_CLASSIFICATION_CONFIDENCE`=85, `TMF_MIN_TMF_CONTENT_CONFIDENCE`=80),
`VAULT_API_VERSION`, `VAULT_ID`, `GLEAN_AGENT_ID` (stamped into provenance),
`BOX_REJECTED_FOLDER_ID` (optional 0-Rejected folder for non-TMF docs).
**Removed vs. earlier builds:** `ANTHROPIC_API_KEY`, `TMF_LLM_*`, `SUPABASE_*`.

## 3. Deploy
Push; Vercel builds. No build config beyond `requirements.txt` (just `requests`).

## 4. Build the Glean agent (in Glean's agent builder)
Give the agent four actions:
- `list_inbox`  → `https://<this-app>.vercel.app/api/etmf/actions/list_inbox`
- `file_or_flag`→ `https://<this-app>.vercel.app/api/etmf/actions/file_or_flag`
- `get_taxonomy`→ your existing `classifications.py`
- `resolve_record` → your existing `query.py`

Instructions: call `list_inbox`; for each doc, read its content (Glean's Box connector),
call `get_taxonomy` and classify only within it, call `resolve_record` for study/site,
produce the five findings, then call `file_or_flag` with `{box_file_id, filename, findings}`.
If uncertain, say so and let the gate hold it — never force a confident answer.

## 5. Wire the Vault seam
Confirm `lib/vault_reference.py`'s taxonomy fetch matches your Vault (mirror your proven
`classifications.py`). Set `BULK_DELEGATOR_USER_ID`.

## 6. Run the demo
1. **Pre-stage**: drop ~10 docs in Box 1-Inbox a few minutes ahead so Glean indexes them.
2. Run the agent ("process the TMF inbox"). Watch three surfaces: the agent narrating,
   Box files moving Inbox → 2-Filed / 3-Exceptions, and Vault showing clean docs In Progress.
3. Result: clean docs filed + moved; flagged docs held with a `.WHY.txt`; non-TMF docs
   rejected with a "remove from intake" note. Per-disposition audit in the Vercel logs.

**Test the gate without the agent** (sanity check): POST `file_or_flag` with a sample
`findings` object and a real `box_file_id` + `filename`, bearer `GLEAN_BEARER_TOKEN`.

## Notes / future
- Content via Glean's Box index handles PDFs, Office docs, images (OCR), email, multiple
  languages. Pre-staging covers indexing latency. A direct fetch+extract action is the
  alternative if you need instant just-dropped processing (heavier; needs OCR for scans).
- `.zip` / `.tar` / `.gz` bundles are unpacked by the `expand_archive` action into
  individual inbox documents (each filed separately); nested archives are not recursed.
  `.eml` is treated as a single file for now.
- To add a durable audit trail + metrics later: stand up Supabase, set
  `TMF_AUDIT_SINK=supabase` + `SUPABASE_*`, and add the read endpoints back (additive).
