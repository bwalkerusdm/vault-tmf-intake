#!/usr/bin/env python3
"""
sync_snapshot.py — refresh the indexed snapshots from Vault.

Run LOCALLY (or in CI) with the Vault env vars set, then commit the two JSON files
and redeploy. This is the only step that talks to Vault for reference data; at
runtime the agent and the gate read the committed snapshots, no live call.

    export VAULT_BASE_URL=...      VAULT_CLIENT_ID=...
    export VAULT_USERNAME=...      VAULT_PASSWORD=...
    python setup/sync_snapshot.py

What it does:
  1. Pulls the full classification taxonomy  -> data/taxonomy_snapshot.json
  2. Resolves the demo study's study/country/site record IDs and MERGES them into
     the existing data/study_records.json (keeping your curated names/aliases),
     filling only the __v fields.

Fail-safe: any record it can't resolve to exactly one match is left null and
warned about — so resolution will HOLD those documents rather than file against a
guess. Fix the alias or the Vault record, then re-run.

VERIFY against your Vault config: the object API names and the match fields used
below (name__v, study_number__v, site_number__v, study_country__v, study__v).
Override the object names with TMF_STUDY_OBJECT / TMF_STUDY_COUNTRY_OBJECT /
TMF_SITE_OBJECT if yours differ.
"""
from __future__ import annotations
import os
import sys
import json
import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib import vault_reference as vr

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
TAX_PATH = os.path.join(DATA_DIR, "taxonomy_snapshot.json")
REC_PATH = os.path.join(DATA_DIR, "study_records.json")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sync_taxonomy() -> int:
    print("→ pulling taxonomy from Vault …")
    flat = vr.get_taxonomy(force=True)          # force = live fetch
    payload = {"_synced_at": _now(), "count": len(flat), "taxonomy": flat}
    with open(TAX_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"  ✓ wrote {len(flat)} triples -> {TAX_PATH}")
    return len(flat)


def _first_match(obj, candidates, fields, parent=None):
    """Try each candidate hint until exactly one record matches; return {id,name} or None."""
    for hint in candidates:
        if not hint:
            continue
        rec = vr._resolve_one(obj, hint, fields, parent=parent)
        if rec:
            return rec
    return None


def sync_records() -> dict:
    print("→ resolving study/country/site IDs from Vault …")
    m = vr._load_json(REC_PATH)
    if not m:
        raise SystemExit(f"missing template {REC_PATH} — create it first")

    warnings = []

    # study
    study_cands = [m.get("study"), m.get("project_code")] + list(m.get("study_aliases") or [])
    study = _first_match(vr._STUDY_OBJECT, study_cands, ["name__v", "study_number__v"])
    if study:
        m["study__v"] = study["id"]
        print(f"  ✓ study {m.get('study')} -> {study['id']}")
    else:
        m["study__v"] = None
        warnings.append(f"study {m.get('study')!r} not resolved")

    parent_study = ("study__v", study["id"]) if study else None

    # countries
    for name, crec in (m.get("countries") or {}).items():
        cands = [name] + list(crec.get("aliases") or [])
        country = _first_match(vr._STUDY_COUNTRY_OBJECT, cands, ["name__v"], parent=parent_study)
        crec["study_country__v"] = country["id"] if country else None
        if country:
            print(f"  ✓ country {name} -> {country['id']}")
        else:
            warnings.append(f"country {name!r} not resolved")

    # sites (scoped to their country)
    for num, srec in (m.get("sites") or {}).items():
        country_name = srec.get("country")
        country_id = (m.get("countries", {}).get(country_name) or {}).get("study_country__v")
        parent = ("study_country__v", country_id) if country_id else parent_study
        cands = [num] + list(srec.get("aliases") or []) + [srec.get("name")]
        site = _first_match(vr._SITE_OBJECT, cands, ["name__v", "site_number__v"], parent=parent)
        srec["site__v"] = site["id"] if site else None
        if site:
            print(f"  ✓ site {num} ({country_name}) -> {site['id']}")
        else:
            warnings.append(f"site {num!r} not resolved")

    m["_synced_at"] = _now()
    with open(REC_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
    print(f"  ✓ wrote record map -> {REC_PATH}")
    return {"warnings": warnings}


def main():
    sync_taxonomy()
    result = sync_records()
    print()
    if result["warnings"]:
        print("⚠  unresolved (left null -> those docs will HOLD until fixed):")
        for w in result["warnings"]:
            print("   -", w)
        print("\nFix the alias in study_records.json or the Vault record, then re-run.")
    else:
        print("✓ all records resolved.")
    print("\nNext: commit data/*.json and redeploy (push to GitHub).")


if __name__ == "__main__":
    main()
