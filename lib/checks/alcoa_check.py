"""
ALCOA+ check service.

THOROUGHNESS REQUIREMENT: assess each attribute against an EXPLICIT test and
return a per-attribute verdict + basis (the 'why'). No vague 'is this good'.

Mechanical attributes (legible, complete, contemporaneous, attributable, available)
SHOULD be deterministic once hardened (Document AI / OCR confidence / field +
signature detection / page-count). For pipeline-proof they use a focused LLM
with a strict rubric. Judgment attributes (accurate, consistent, enduring) stay
LLM-assessed with the rubric.

HARDEN: route the mechanical attributes to Document AI / Textract / Azure DI
(OCR confidence -> legible; signature+author detection -> attributable; date
extraction -> contemporaneous; page-count/section presence -> complete) and keep
the SAME per-attribute return shape.

Fail-safe: if an attribute cannot be assessed, verdict='flag' (route to review).
"""
from __future__ import annotations
import json

from lib import grounding
from datetime import datetime, timezone

ATTRIBUTES = [
    ("attributable",   "Is the author/originator identifiable? Are required signatures present with name, role, and date?"),
    ("legible",        "Is the document fully readable? Any corrupted, illegible, or low-quality regions?"),
    ("contemporaneous","Is it dated, and does the document/signature date align with the event recorded (not back-dated)?"),
    ("original",       "Is this an original or true certified copy? Is a document control ID / version present? Not an unverified derivative?"),
    ("accurate",       "Is the content internally consistent with no contradictory values, and complete where completeness is expected?"),
    ("complete",       "Are all required sections/pages present (no 'page X of Y' gaps) and required fields populated?"),
    ("consistent",     "Are dates, identifiers, and terminology consistent throughout the document?"),
    ("enduring",       "Is it recorded on a durable medium (a finalized document, not a transient note/draft)?"),
    ("available",      "Is the file retrievable, openable, and not corrupted?"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(document: dict, llm_call) -> dict:
    """`document` has {text, (optional) ocr_confidence, page_info...}.
    Returns the structured alcoa_plus result. overall='flagged' is blocking upstream.
    """
    text = document.get("text", "") or ""
    rubric = "\n".join(f"- {name}: {test}" for name, test in ATTRIBUTES)
    prompt = (
        "Assess this clinical trial document against ALCOA+ data-integrity "
        "attributes. For EACH attribute give a verdict ('pass' or 'flag') and a "
        "one-sentence basis citing specific evidence from the document. If an "
        "attribute cannot be assessed, verdict='flag' with basis explaining why. "
        "Respond ONLY with a JSON object mapping each attribute name to "
        "{verdict, basis}. No prose, no markdown.\n\n"
        + grounding.INJECTION_GUARD_INSTRUCTION + "\n\n"
        f"ATTRIBUTES:\n{rubric}\n\n"
        + grounding.wrap_untrusted(text, limit=16000)
    )
    raw = llm_call(prompt)
    try:
        parsed = json.loads(raw.strip().strip("`").replace("json", "", 1).strip())
    except Exception:
        parsed = {}

    attributes = {}
    for name, _ in ATTRIBUTES:
        entry = parsed.get(name) or {}
        verdict = entry.get("verdict", "flag")          # fail-safe: missing -> flag
        if verdict not in ("pass", "flag"):
            verdict = "flag"
        attributes[name] = {"verdict": verdict,
                            "basis": entry.get("basis", "not assessed (fail-safe flag)")}

    flagged = [n for n, a in attributes.items() if a["verdict"] == "flag"]
    return {
        "check": "alcoa_plus",
        "overall": "flagged" if flagged else "pass",
        "attributes": attributes,
        "flagged_attributes": flagged,
        "method": "llm_rubric_stub",   # HARDEN -> 'doc_ai_rules + llm' for judgment attrs
        "ran_at": _now(),
    }
