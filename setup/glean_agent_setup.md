# Glean agent setup (Glean-only build)

The reasoning lives in a Glean agent; this backend exposes the deterministic actions.

## Actions to register on the agent
| Action | Endpoint | Purpose |
|---|---|---|
| `list_inbox` | `…/api/etmf/actions/list_inbox` (this repo) | list Box 1-Inbox docs |
| `file_or_flag` | `…/api/etmf/actions/file_or_flag` (this repo) | the gate: file clean / hold flagged |
| `get_taxonomy` | your `classifications.py` | the closed classification set |
| `resolve_record` | your `query.py` | resolve study/country/site IDs |
| `expand_archive` | `…/api/etmf/actions/expand_archive` (this repo) | unpack a .zip/.tar/.gz into individual inbox docs |
| `report_unprocessable` | `…/api/etmf/actions/report_unprocessable` (this repo) | hold a doc the agent can't read/process, with a reason |

All authenticate with `Authorization: Bearer <GLEAN_BEARER_TOKEN>`.

## Agent instructions (outline)
1. Call `list_inbox`. For each document:
   - If `is_archive` is true, call `expand_archive` on it (do NOT try to file it). Its
     member files reappear in the inbox; re-list and process each individually.
2. Read its content (Glean Box connector).
3. Call `get_taxonomy`; classify ONLY within the returned set — never invent a triple.
4. Call `resolve_record` for study/country/site.
5. Produce the five findings (tmf_content, classification, phi_pii, alcoa_plus,
   record_resolution) in the documented shapes.
6. Call `file_or_flag` with `{box_file_id, filename, findings}`. Report the disposition.
7. If anything is uncertain, say so and let the gate hold it. You cannot file directly —
   only `file_or_flag` files, and only when the deterministic gate passes.

## Loop (decision B)
Simplest/most predictable for a demo: one document per agent run, invoked per file.
Alternative: have the agent iterate the `list_inbox` results itself — validate that your
agent builder handles multi-item iteration reliably before relying on it live.

---

## Handling documents that don't belong to this study (out-of-scope)

Some inbox documents will reference a different study, a site that isn't part of
this study, or a conflicting country. Do **not** map them onto the closest site.

For every document, when you call `resolve_record`, pass the identifiers exactly as
written on the document, and then pass them through to `file_or_flag` inside
`record_resolution` as `study_ref`, `country_ref`, `site_ref` (in addition to any
IDs you resolved). The gate re-resolves those itself — that's the authority.

- If `resolve_record` returns `scope: "out_of_scope"`, tell the user the document
  appears **misrouted** and name the reason (`wrong_study`, `unrecognized_site`, or
  `country_mismatch`) and the offending value. Then call `file_or_flag` — it will set
  the document aside as out-of-scope (its own queue), not as a fixable exception.
- If `scope: "indeterminate"` (no study/site references at all), treat it as a normal
  unresolved exception — it may belong here but can't be attributed yet.
- Never invent or "best-guess" a site to make a document resolve. A held document is
  the correct outcome; a misfiled one is not.
