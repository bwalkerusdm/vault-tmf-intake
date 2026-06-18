"""
Demo reset (Box side).

Returns the Box folders to their pre-run state so the SAME documents can be
re-filed and re-demoed cleanly:
  - every file in 2-Filed / 3-Exceptions / 0-Rejected is moved back to 1-Inbox
  - filing/holding renames are reversed (the `[filed-<id>]` suffix and the
    `[expanded-N-files]` archive prefix are stripped) so names match the originals
  - the pipeline's companion files (.WHY.txt / .eval.json) are deleted

NON-destructive to your real data: it only moves files between your own intake
folders and deletes companion files the pipeline itself generated.

Archive caveat: if you tested a .zip, expand_archive exploded it into individual
member files. A reset brings the (renamed) original zip AND those members back to
the inbox, which would double up on the next run. For a clean repeated archive
demo, re-upload the original zip fresh and remove the stray members.
"""
from __future__ import annotations
import re

from lib import box_client

_COMPANION_SUFFIXES = (".WHY.txt", ".eval.json")
_FILED_SUFFIX = re.compile(r" \[filed-[^\]]+\]$")
_EXPANDED_PREFIX = re.compile(r"^\[expanded-\d+-files\] ")


def _original_name(name: str) -> str:
    name = _FILED_SUFFIX.sub("", name)
    name = _EXPANDED_PREFIX.sub("", name)
    return name


def reset_box(token: str, folders: dict) -> dict:
    inbox = folders["inbox"]
    moved, deleted = [], []

    sources = [folders.get("filed"), folders.get("exceptions"), folders.get("rejected")]
    for src in sources:
        if not src or src == inbox:
            continue
        for it in box_client.list_folder(src, token):
            name = it["name"]
            if name.endswith(_COMPANION_SUFFIXES):
                box_client.delete_file(it["id"], token)
                deleted.append(name)
                continue
            original = _original_name(name)
            box_client.move_file(it["id"], inbox, token,
                                 new_name=(original if original != name else None))
            moved.append(original)

    # clear any stray companions left in the inbox itself
    for it in box_client.list_folder(inbox, token):
        if it["name"].endswith(_COMPANION_SUFFIXES):
            box_client.delete_file(it["id"], token)
            deleted.append(it["name"])

    return {"moved_to_inbox": moved, "moved_count": len(moved),
            "companions_deleted": deleted, "deleted_count": len(deleted)}
