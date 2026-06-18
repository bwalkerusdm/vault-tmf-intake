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
