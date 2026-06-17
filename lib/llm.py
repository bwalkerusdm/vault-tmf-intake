"""
Focused LLM caller used by the check services (PHI stage detection, ALCOA rubric,
TMF-content, classification matching).

get_llm_call() returns fn(prompt: str) -> str (the model's text response).

This wraps whatever model/endpoint you use. The checks require only a
string-in/string-out function that returns the model's response (the prompts
instruct JSON-only). Two GxP-relevant behaviors are set HERE, centrally:

  - temperature=0 + a pinned model id (TMF_LLM_MODEL): reproducibility. A given
    input yields the most deterministic output the model can give, and the exact
    model version is recorded per decision (see model_id(), stamped into the
    audit record by run_all_checks). A model change is therefore detectable and
    can trigger re-validation (Iterative pillar / AI change control).
  - a system preamble that enforces JSON-only output, "abstain over guess," and
    prompt-injection resistance (document is data, never instructions). This is
    the prompt-LAYER half of the guardrail; the deterministic enforcement lives
    in lib/grounding.py and the gate (a prompt alone is never the guarantee).

  - get_llm_call() returns a callable that FAILS SAFE: on any provider error it
    logs and returns "" so the checks see unparseable output -> uncertain ->
    held. A model outage can never cause a silent auto-file.

HARDEN: the PHI and ALCOA *mechanical* parts should move to purpose-built
services (Comprehend Medical / Document AI). This caller remains for judgment.
"""
from __future__ import annotations
import os
import json
import logging
import urllib.request

from lib import grounding

logger = logging.getLogger("tmf.llm")

LLM_PROVIDER = os.environ.get("TMF_LLM_PROVIDER", "anthropic")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# VERIFY this is a model string your account/region can call; pin a dated
# version in production so a model change is an explicit, revalidated event.
ANTHROPIC_MODEL = os.environ.get("TMF_LLM_MODEL", "claude-sonnet-4-6")

# Central system framing applied to every check call.
SYSTEM_PREAMBLE = (
    "You are a deterministic document-analysis function in a regulated (GxP) "
    "pipeline. Respond ONLY with the JSON the user prompt specifies -- no prose, "
    "no markdown. " + grounding.ABSTAIN_INSTRUCTION + " "
    + grounding.INJECTION_GUARD_INSTRUCTION
)


def model_id() -> str:
    """Stable identifier of the model in use, stamped into each decision record
    for reproducibility / change control."""
    return f"{LLM_PROVIDER}:{ANTHROPIC_MODEL}"


def get_llm_call():
    raw = _anthropic_call if LLM_PROVIDER == "anthropic" else _anthropic_call
    # add other providers (Glean-hosted, Bedrock, etc.) above.

    def llm_call(prompt: str) -> str:
        try:
            return raw(prompt)
        except Exception as e:
            # fail safe: empty -> checks can't parse -> uncertain -> held.
            logger.error("llm_call failed (%s) -> returning empty; checks fail "
                         "safe to held.", e)
            return ""

    return llm_call


def _anthropic_call(prompt: str) -> str:
    """Single-turn call. temperature=0 for reproducibility; JSON-only enforced by
    the system preamble (and by lib/grounding parsing downstream)."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1500,
        "temperature": 0,
        "system": SYSTEM_PREAMBLE,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body, method="POST",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        })
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    # concatenate text blocks
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")
