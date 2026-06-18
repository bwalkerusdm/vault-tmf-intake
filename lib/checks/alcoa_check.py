"""
ALCOA+ VALIDATOR.

Glean-only architecture: the Glean agent assesses each ALCOA+ attribute and returns
a per-attribute {verdict, basis}. This function validates that finding and enforces
the fail-safe: any attribute that is missing or not an explicit "pass" -> "flag".

Agent finding shape: {attributes: {attribute_name: {verdict: "pass"|"flag", basis}}}
(or the attribute map at top level).
"""
from __future__ import annotations
from datetime import datetime, timezone

ATTRIBUTES = ["attributable", "legible", "contemporaneous", "original", "accurate",
              "complete", "consistent", "enduring", "available"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate(finding: dict) -> dict:
    r = finding if isinstance(finding, dict) else {}
    src = r.get("attributes") if isinstance(r.get("attributes"), dict) else r
    attributes = {}
    for name in ATTRIBUTES:
        entry = src.get(name) if isinstance(src, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        verdict = entry.get("verdict", "flag")          # fail-safe: missing -> flag
        if verdict not in ("pass", "flag"):
            verdict = "flag"
        attributes[name] = {"verdict": verdict,
                            "basis": entry.get("basis", "not assessed (fail-safe flag)")}
    flagged = [n for n, a in attributes.items() if a["verdict"] == "flag"]
    return {"check": "alcoa_plus",
            "overall": "flagged" if flagged else "pass",
            "attributes": attributes, "flagged_attributes": flagged,
            "method": "glean_agent", "ran_at": _now()}
