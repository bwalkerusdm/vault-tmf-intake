> STATUS: implemented in this repo (lib/run_all_checks.py gate_from_findings + lib/checks validators + api/etmf/actions/{list_inbox,file_or_flag}). This document is the design rationale.

# Glean-only architecture — design mapping

Goal: present the bulk lane as **Glean orchestrating end to end**, with no separate
Anthropic key. The Glean agent does the *reasoning*; thin custom actions do the
deterministic *I/O and the gate*. The backbone is unchanged: **AI recommends → a
deterministic gate enforces the rules → the human renders the binding QC verdict in
Vault.**

The key insight from the existing code: every check already has two halves —

1. an **LLM judgment** (a prompt → a raw finding), and
2. a **deterministic enforcement** (fail-safe defaults, the taxonomy-membership check,
   the thresholds, the gate, the disposition).

Half (1) moves into the Glean agent. Half (2) stays in a custom action. So the
anti-hallucination guardrails and the gate do **not** move into the agent — they stay
as deterministic Python that re-validates whatever the agent proposes.

---

## Architecture at a glance

```
Glean agent (the brain)                     Custom actions on Vercel (the hands)
─────────────────────────                   ────────────────────────────────────
reads each Inbox doc            ── calls ──▶ list_inbox        (Box: what's waiting)
proposes the 5 findings         ── calls ──▶ get_taxonomy      (Vault: the closed set)
                                ── calls ──▶ resolve_record    (Vault: study/site IDs)
hands findings to the gate      ── calls ──▶ file_or_flag      (THE GATE — deterministic)
                                                  │
                                                  ├─ clean  → file to Vault In Progress
                                                  │           + move to 2-Filed
                                                  └─ flagged→ hold in 3-Exceptions + .WHY.txt
```

No `ANTHROPIC_API_KEY`. No model calls anywhere in the backend. Glean calls *into*
the actions, so the backend doesn't need a Glean API token either — only the shared
`GLEAN_BEARER_TOKEN` to authenticate the incoming action calls, plus Vault and Box.

---

## The split — what the agent decides vs. what the action enforces

| Check | Agent produces (the recommendation) | Action enforces deterministically (the gate) |
|---|---|---|
| **tmf_content** | `{is_tmf, zone, artifact, reason, confidence}` | `is_tmf == false` → **hold** (`not_tmf`); `confidence < 80` → review flag |
| **classification** | `{type__v, subtype__v, classification__v, tmf_rm_v3, confidence, alternatives}` chosen **only** from `get_taxonomy` | `in_taxonomy(triple)` must be true — if not, confidence is forced to **0** and held as *not grounded* (kills hallucinated classes); `confidence < 85` → **hold** |
| **phi_pii** | `{status, findings:[{identifier_type, belongs_to, location}]}` | `status != "clear"` (any subject-owned identifier) → **blocking hold** |
| **alcoa_plus** | `{overall, attributes:{name:{verdict, basis}}, flagged_attributes}` | any `flagged_attributes` → **blocking hold** |
| **record_resolution** | calls `resolve_record` → study/country/site IDs | `all_resolved` must be true **and** `parent_validated` true → else **hold** |

**Fail-safe is preserved in the action.** If the agent omits or malforms any finding,
the action applies that check's fail-safe default — missing/!member classification →
confidence 0 → hold; missing alcoa → all attributes flag; missing phi → treat as
findings; missing tmf_content → `is_tmf=false`. Uncertainty never auto-files, exactly
as today. The aggregation rule is the existing `_derive_gate`: **clean only if every
check passes; anything else is held with a reason.**

---

## Custom actions (the agent's tools) — contracts

All are `POST`, authenticated with `Authorization: Bearer <GLEAN_BEARER_TOKEN>`.

**`list_inbox`** — *Box: what's waiting*
- in: `{}`  out: `{documents:[{box_file_id, filename}], count}`

**`get_taxonomy`** — *Vault: the closed classification set* (this is your existing
`classifications.py`; reuse it — point the agent's action at that endpoint)
- in: `{query?, type__v?, limit?}`  out: the taxonomy entries the agent must pick from

**`resolve_record`** — *Vault: resolve study/country/site by name* (this is your
existing `query.py`; reuse it)
- in: `{object, filters, fields}`  out: matching records with Vault IDs

**`file_or_flag`** — *THE GATE (deterministic)*
- in: `{box_file_id, filename, findings:{tmf_content, classification, phi_pii, alcoa_plus, record_resolution}}`
- does: apply `grounding.in_taxonomy` to the proposed triple → apply thresholds + fail-safe
  → `_derive_gate` → **clean**: file to Vault In Progress via delegated session, stamp the
  `glean_*` fields + `checks_hash` + provenance, move file to 2-Filed; **flagged**: write
  `.WHY.txt`, move file to 3-Exceptions. Log the disposition (stdout).
- out: `{disposition: "filed"|"held", status, flags, vault_document_id?, checks_hash}`

---

## The Glean agent (built in Glean's agent builder — not repo code)

Instructions outline:

1. Call `list_inbox`. For each document:
2. Read the document's content (see open decision A).
3. Call `get_taxonomy`; classify **only** within the returned set; never invent a triple.
4. Produce the five findings in the exact shapes above. For record_resolution, call
   `resolve_record` to get the Vault IDs.
5. Call `file_or_flag` with `box_file_id` + the findings. Report the disposition.
6. If anything is uncertain, say so honestly and let the gate hold it — do **not**
   force a confident answer.

The agent is the "AI recommends" layer. It cannot file directly — only `file_or_flag`
files, and only when the deterministic gate passes.

---

## Provenance / audit (what changes)

The audit event and `checks_hash` stay. The provenance block changes to reflect the
new reasoning source: `model: claude-…, temp 0` becomes
`reasoning: glean_agent <agent_id @ version>, validated_by: deterministic_gate <ver>`.
The `checks_hash` (over the findings) remains the binding tie for the human QC
attestation in Vault. So you can still show, per document, *what was recommended, by
which agent version, which deterministic rules validated it, and the hash the human
signs against.*

> Honest note for the regulated audience: an agent's reasoning is less reproducible
> than a pinned model at temp 0. The mitigation is that the **gate and the
> taxonomy-membership enforcement are deterministic and reproducible** — the agent can
> recommend anything, but only a grounded, above-threshold, PHI-clean, ALCOA-passing,
> fully-resolved document files. That's the line to draw in the demo.

---

## Env var changes

- **Remove:** `ANTHROPIC_API_KEY`, `TMF_LLM_MODEL`, `TMF_LLM_PROVIDER`.
- **Keep:** `VAULT_*`, `BOX_*` (token + 3 folder IDs), `GLEAN_BEARER_TOKEN`,
  `BULK_DELEGATOR_USER_ID`, thresholds, `TMF_*_OBJECT`.
- **Not needed:** Glean API token (Glean calls in; the backend never calls Glean).

---

## Repo changes I'll make

- **Drop** `lib/llm.py`.
- **Transform** `lib/run_all_checks.py` + `lib/checks/*` from *LLM callers* into
  *validators*: accept the agent's structured findings, apply the fail-safe
  normalization + `grounding.in_taxonomy`, and run the existing `_derive_gate`. (The
  prompts go away; the enforcement stays.)
- **Replace** `api/etmf/batch/*` with `api/etmf/actions/list_inbox.py` and
  `api/etmf/actions/file_or_flag.py`.
- **Keep** `lib/box_client.py`, `lib/vault_filing.py`, `lib/disposition.py`,
  `lib/grounding.py`, `lib/audit.py`, `lib/vault_reference.py`.
- **Reuse** your existing `classifications.py` / `query.py` as the `get_taxonomy` /
  `resolve_record` actions (no need to rebuild them).

---

## Two open decisions (my recommendations)

**A. How does the agent read each document's content?**
- *Recommended:* use Glean's **Box connector** — Glean already indexes Box, so the
  agent reads the doc content natively. Most "Glean-only," no extraction code. Needs
  the Inbox folder indexed in Glean and the agent able to target a specific file.
- *Fallback:* a `get_document` action that fetches the file from Box and returns
  extracted text. Reliable, but adds a PDF-text-extraction dependency to the backend.

**B. Where does the per-document loop live?**
- *Recommended for the demo:* the agent processes **one document per run**, invoked
  per file — simplest and most predictable to show.
- *Slicker option:* the agent iterates the `list_inbox` results itself. Worth
  validating how reliably your Glean agent builder handles multi-item iteration before
  relying on it in a live demo.

Confirm A and B (or accept the recommendations) and I'll rebuild the repo to match.
