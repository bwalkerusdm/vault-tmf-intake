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
**Demo reset (sandbox only):** to let `reset_demo` clear the Vault between runs, set
`ALLOW_VAULT_RESET=true` on the SANDBOX project only. Optional: `VAULT_RESET_WHERE`
(default `created_via_glean_agent__c = 'yes__c'`) and `VAULT_RESET_MAX` (default 200).
Leave `ALLOW_VAULT_RESET` unset on any real vault — without it, reset never deletes there.

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

## Re-running the same docs (rehearse / re-demo)
To run the same documents through fresh each time, call the reset before re-running:
```
POST /api/etmf/actions/reset_demo.py   {"confirm": true, "vault": true}
```
- Box side always resets: filed/held files move back to 1-Inbox (un-renamed),
  .WHY.txt/.eval.json companions are deleted.
- Vault side deletes ONLY agent-created docs, and ONLY if `ALLOW_VAULT_RESET=true`
  is set on the project (so re-filing doesn't pile up duplicates).
- Archive caveat: a tested `.zip` comes back alongside its extracted members; for a
  clean repeated archive demo, re-upload the original zip fresh and remove the members.

## Notes / future
- Content via Glean's Box index handles PDFs, Office docs, images (OCR), email, multiple
  languages. Pre-staging covers indexing latency. A direct fetch+extract action is the
  alternative if you need instant just-dropped processing (heavier; needs OCR for scans).
- `.zip` / `.tar` / `.gz` bundles are unpacked by the `expand_archive` action into
  individual inbox documents (each filed separately); nested archives are not recursed.
  `.eml` is treated as a single file for now.
- To add a durable audit trail + metrics later: stand up Supabase, set
  `TMF_AUDIT_SINK=supabase` + `SUPABASE_*`, and add the read endpoints back (additive).

---

## Indexed snapshots (taxonomy + study records) — no live Vault call

The taxonomy and the demo study's study/country/site IDs are served from committed
JSON in `data/`, so the agent and the gate don't hit Vault per document (faster, and
off Vault's API limits during a batch/demo).

**Two agent actions read these:**
- `get_taxonomy` (`/api/etmf/actions/get_taxonomy.py`) → valid classification triples
- `resolve_record` (`/api/etmf/actions/resolve_record.py`) → study/country/site → Vault IDs

Point the agent at these (in `setup/glean_action_spec.yaml`) instead of the live
vault-backend equivalents. The gate (`file_or_flag`) already validates classification
membership against the same snapshot — the anti-hallucination backstop.

**Populate / refresh the snapshots (run locally, then commit + push):**
```
export VAULT_BASE_URL=... VAULT_CLIENT_ID=... VAULT_USERNAME=... VAULT_PASSWORD=...
python setup/sync_snapshot.py
```
This writes `data/taxonomy_snapshot.json` and fills the `__v` IDs in
`data/study_records.json`. You can also paste the four site IDs by hand. **Until the
IDs are filled, every document holds on resolution** (fail-safe) — that's expected.

**Verify after deploy:** call `get_taxonomy`; the response shows `"source":"snapshot"`
when the snapshot is live (vs `"vault_live"` fallback). `vercel.json` already ships
`data/**` in the bundle.

> Re-run `sync_snapshot.py` before the demo so the snapshot stays authoritative.

---

## Out-of-scope / misrouted documents (doesn't belong to this study)

The CRO may seed documents that reference another study, a site not defined for this
study, or a conflicting country. The gate **re-resolves the raw references itself**
(it doesn't trust the agent's chosen IDs), so a document can't be force-fit onto the
nearest site. When it affirmatively belongs elsewhere it gets its own verdict:

- `status: out_of_scope` with a specific reason — `wrong_study`, `unrecognized_site`, or `country_mismatch`.
- Held in a dedicated **4-OutOfScope** Box folder if `BOX_OUTOFSCOPE_FOLDER_ID` is set, otherwise in 3-Exceptions tagged `disposition_kind: out_of_scope`.
- A `.WHY.txt` that says re-route/return — NOT "fix and re-file" (it isn't a remediable in-study exception).

A document with **no** resolvable references (no study/site at all) is *not* out-of-scope —
it's a normal `unresolved` exception for manual association.

For this to work, the agent must pass the **raw references it read** in the
`record_resolution` finding: `study_ref`, `country_ref`, `site_ref`. The gate resolves
those against the snapshot to decide scope. (Optional env: `BOX_OUTOFSCOPE_FOLDER_ID`.)
