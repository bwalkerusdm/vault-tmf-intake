# data/ — indexed snapshots (served at runtime, no live Vault call)

Two files let the agent and the gate resolve against committed data instead of
hitting the Vault API per document. This removes latency and keeps the pipeline
off Vault's burst/session limits during a batch or a live demo.

| file | what | how it's filled |
|------|------|-----------------|
| `study_records.json` | study / country / site → Vault record IDs for the demo study | `setup/sync_snapshot.py`, or hand-edit the `__v` fields |
| `taxonomy_snapshot.json` | flat list of valid type/subtype/classification triples | `setup/sync_snapshot.py` (generated; not committed by hand) |

## How resolution uses these
- `get_taxonomy()` returns `taxonomy_snapshot.json` if present, else live-fetches from Vault (24h cache). The **gate** validates the agent's classification against whatever this returns — so a stale snapshot weakens the anti-hallucination check. Keep it fresh.
- `resolve()` / the `resolve_record` action resolve against `study_records.json`. A study/site/country mismatch, a missing record, or unpopulated `__v` IDs → `all_resolved=false` → the gate **holds** the document (fail-safe). This is what catches the deliberate mismatch cases (wrong project number, site/country conflict).

## Refresh discipline
The snapshot is authoritative **as of its last sync**. Re-run `setup/sync_snapshot.py` whenever the taxonomy changes or records are added, and always right before a demo. If a file is absent, the code falls back to the live Vault path automatically — nothing breaks, it's just slower.

> Until `study_records.json` has real `__v` IDs, every document holds on resolution. That's intentional: better to hold than to file against a guessed ID.
