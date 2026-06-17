# Manual "Process Now" Trigger — Glean Agent Action

Lets a user run the TMF intake on demand instead of waiting for the threshold.
Same pipeline, same gate, same audit — only the trigger differs.

## How it works

```
User: "Process the TMF intake folder now."
        │
        ▼
Glean agent  ──calls action──>  POST /api/etmf/batch/run_intake_batch
                                  { "max_seconds": 40, "chunk": 5 }
        │                              │
        │                              ▼  evaluate + file/hold in bounded passes
        │                       returns { done, more_remaining, summary, message }
        ▼
Agent shows: "Processed 23: 19 filed, 4 held (2 PHI, 1 ALCOA, 1 low-confidence)."
   If more_remaining > 0, agent calls again -> "Processed 20 more... done."
```

The endpoint processes as many chunks as it can within `max_seconds` (safely under
the Vercel timeout), then reports whether more remain. The agent loops the call
until `done: true`, giving a live "processed X… Y… done" progress feel without any
request timing out.

## Glean custom action definition

```yaml
name: run_tmf_intake_now
description: >
  Manually run the TMF document intake pipeline now: evaluate documents waiting
  in the Box intake folder, auto-file the ones that pass all checks to Vault
  (In Progress, for QC), and hold the ones with issues in Box with a reason.
  Use when the user asks to process, run, or kick off the intake/filing now
  rather than waiting for the automatic threshold.
method: POST
url: https://vault-backend-pi.vercel.app/api/etmf/batch/run_intake_batch
headers:
  Authorization: "Bearer {{GLEAN_BEARER_TOKEN}}"
  Content-Type: application/json
body:
  type: object
  properties:
    max_seconds: { type: integer, description: "max processing time per call (default 40)" }
    chunk:       { type: integer, description: "documents per chunk (default 5)" }
# Response: { done, more_remaining, elapsed_seconds, summary, message }
```

## Conversation starters

- `Process the TMF intake folder now`
- `Run the document filing`
- `Kick off TMF intake`

## Agent instructions (the loop)

Add to the agent's prompt for this action:

> When the user asks to run/process the intake, call `run_tmf_intake_now`.
> Report the `message` field to the user. If `more_remaining` is greater than 0,
> call `run_tmf_intake_now` again and report progress, repeating until `done` is
> true. Then give a final summary: total filed, total held, and the breakdown of
> held reasons. Do NOT claim anything was filed beyond what the summaries report.

## Both triggers coexist

| Trigger | What calls the pipeline | When |
|---|---|---|
| **Manual** | this agent action (`run_tmf_intake_now`) | user asks; demos, ad-hoc catch-up |
| **Threshold/scheduled** | a watcher (Inbox count ≥ N, or a timer) calling the same endpoints | steady-state, unattended |

Safe to run both: the Box folder-state machine (Inbox → Filed/Exceptions) makes
processing idempotent, so a manual run and a scheduled run can't double-file a
document even if they overlap. Both use the identical gate, audit, and
human-QC-in-Vault — only the trigger differs.

## Note

The orchestrator currently files to In Progress; the QC kickoff (assign reviewer
role + start workflow) is the same pending piece as the chunked endpoints — wired
in once your QC workflow's entry requirements are confirmed.
