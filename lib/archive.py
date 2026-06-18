"""
Archive expansion (standard library only).

A .zip / .tar(.gz/.bz2) / .gz / .bz2 in the inbox is almost always a *bundle* of
separate TMF artifacts that each belong in a different place. So we unpack it into
its member files and let each be classified, checked, and filed on its own, rather
than treating the container as one document.

`extract_members` is a pure function (no Box I/O) so it is unit-testable. Guards:
- skips directories, dotfiles, and __MACOSX junk
- skips empty and oversized members (default 64 MB, matching Glean's limit)
- does NOT recurse into nested archives -- it flags them as skipped instead
"""
from __future__ import annotations
import io
import os
import zipfile
import tarfile
import gzip
import bz2

ARCHIVE_EXTS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".gz", ".bz2")
DEFAULT_MAX_FILES = 50
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024  # 64 MB


def is_archive(name: str) -> bool:
    n = (name or "").lower()
    return any(n.endswith(e) for e in ARCHIVE_EXTS)


def _skip_name(base: str) -> bool:
    return (not base) or base.startswith(".") or base.upper().startswith("__MACOSX")


def extract_members(raw: bytes, filename: str, *,
                    max_files: int = DEFAULT_MAX_FILES,
                    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> dict:
    """Return {"members": [(basename, bytes), ...], "skipped": [(name, reason), ...]}."""
    members: list = []
    skipped: list = []
    n = (filename or "").lower()

    def add(base: str, data: bytes):
        base = os.path.basename(base or "")
        if _skip_name(base):
            return
        if len(members) >= max_files:
            skipped.append((base, "exceeds max_files cap")); return
        if len(data) == 0:
            skipped.append((base, "empty")); return
        if len(data) > max_file_bytes:
            skipped.append((base, "exceeds max_file_bytes")); return
        if is_archive(base):
            skipped.append((base, "nested archive (not recursed)")); return
        members.append((base, data))

    try:
        if n.endswith(".zip"):
            zf = zipfile.ZipFile(io.BytesIO(raw))
            for info in zf.infolist():
                if info.is_dir():
                    continue
                add(info.filename, zf.read(info))
        elif n.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2")):
            tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                f = tf.extractfile(m)
                add(m.name, f.read() if f else b"")
        elif n.endswith(".gz"):
            add(os.path.basename(filename)[:-3] or "extracted", gzip.decompress(raw))
        elif n.endswith(".bz2"):
            add(os.path.basename(filename)[:-4] or "extracted", bz2.decompress(raw))
        else:
            raise ValueError(f"unsupported archive type: {filename}")
    except Exception as e:
        raise ValueError(f"could not read archive {filename}: {e}")

    return {"members": members, "skipped": skipped}
