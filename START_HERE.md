# START HERE — first deploy (new standalone project, Hobby, no Supabase)

Box → checks → file clean / hold flagged. Dispositions log to Vercel logs. No
database to set up.

Accounts: **Vercel** (new project, Hobby), **Box** (inbox/filed/exceptions folders +
token), **Veeva Vault**, **Anthropic** (LLM key), **Glean** (calls the endpoints).

## 1. New repo + new Vercel project
Put these files in a **new** git repo (keep the paths). Connect it to a **new**
Vercel project, separate from `vault-backend`. Each `api/**/*.py` becomes an
endpoint; `vercel.json` caps them at 60s and ships `lib/` with them.

## 2. Environment variables (NO Supabase)
**Required:**
- `VAULT_BASE_URL`, `VAULT_CLIENT_ID`, `VAULT_USERNAME`, `VAULT_PASSWORD`
- `BOX_TOKEN` (or `BOX_DEVELOPER_TOKEN`), `BOX_INBOX_FOLDER_ID`,
  `BOX_FILED_FOLDER_ID`, `BOX_EXCEPTIONS_FOLDER_ID`
- `ANTHROPIC_API_KEY`
- `GLEAN_BEARER_TOKEN` — you invent this shared secret; reuse it in the Glean action

**Defaulted (override as needed):**
- `INTAKE_MAX_SECONDS` = `50` (Hobby)
- `BULK_DELEGATOR_USER_ID` = your Vault user id (default is the demo's)
- `TMF_LLM_MODEL` = `claude-sonnet-4-6`, `TMF_LLM_PROVIDER` = `anthropic`
- `TMF_MIN_CLASSIFICATION_CONFIDENCE` = `85`, `TMF_MIN_TMF_CONTENT_CONFIDENCE` = `80`
- `TMF_STUDY_OBJECT` / `TMF_STUDY_COUNTRY_OBJECT` / `TMF_SITE_OBJECT`, `VAULT_API_VERSION`, `VAULT_ID`
- Leave `TMF_AUDIT_SINK` **unset** → dispositions log to stdout (no DB).

## 3. Deploy
Push to the repo; Vercel builds. No build config needed beyond `requirements.txt`.

## 4. Wire the Vault seams
In `lib/vault_reference.py`, confirm the taxonomy fetch + VQL resolver + field names
match your Vault (mirror your proven `query.py` / `classifications.py`). Set
`BULK_DELEGATOR_USER_ID` to your Vault user id.

## 5. Run the demo
1. Drop ~10 documents in Box **1-Inbox**.
2. POST the trigger (repeat until `remaining` is 0 — it's idempotent; small chunks
   just mean a few more calls):
   ```
   curl -X POST https://<new-app>.vercel.app/api/etmf/batch/run_intake_batch \
     -H "Authorization: Bearer <GLEAN_BEARER_TOKEN>" \
     -H "Content-Type: application/json" -d '{"chunk":6}'
   ```
3. Result: clean docs in Vault **In Progress** and moved to **2-Filed**; flagged docs
   in **3-Exceptions** with a `.WHY.txt`. The JSON response summarizes filed vs held
   and the hold reasons.
4. See a structured audit event per disposition in the **Vercel function logs**.

## Adding the audit trail + metrics later
Stand up Supabase, run the audit-table SQL, set `TMF_AUDIT_SINK=supabase` +
`SUPABASE_*`, and add back `lib/audit_read.py` plus the `api/etmf/audit/*` endpoints
(query / drift / sponsor). The core lane doesn't change.
