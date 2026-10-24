"""Persistent verified archives; callers hold the data root's build lock.

Legacy repos/ snapshots have no complete file inventory and are never adopted.
Only an archive that passed safe_extract may be published here.
"""
import hashlib
import json
import os
import re

from ..bank_storage import atomic_json, replace_with_sharing_wait


def cache_directory(root, source):
    # Compact disk names also work on Windows without long-path opt-in. Full
    # repository/commit/digest values are always checked in the record.
    identity = source.repository.casefold() + "\0" + source.commit
    return root / hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def load_archive(root, source, control, limits):
    directory = cache_directory(root, source)
    control.phase("cache", f"校验本地来源缓存 {source.repository}")
    try:
        record = json.loads((directory / "complete.json").read_text(encoding="utf-8"))
        metadata = record["source"]
        digest = metadata["archive_sha256"]
        size = metadata["download_bytes"]
        if (record["schema_version"] != 1
                or metadata["repository"].casefold() != source.repository.casefold()
                or metadata["commit"] != source.commit
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or type(size) is not int or size <= 0):
            return None
        archive = directory / (digest[:32] + ".zip")
        if archive.stat().st_size != size:
            return None
        # A stricter caller limit must still apply to a locally cached archive.
        if size > limits.download_bytes:
            from .control import BuildError
            raise BuildError("cache", "ZIP 下载大小超出限制。")
        actual = hashlib.sha256()
        with archive.open("rb") as incoming:
            while True:
                control.check()
                block = incoming.read(64 * 1024)
                if not block:
                    break
                actual.update(block)
        if actual.hexdigest() != digest:
            return None
        return archive, metadata
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def publish_archive(root, source, archive, metadata, control):
    control.check()
    directory = cache_directory(root, source)
    directory.mkdir(parents=True, exist_ok=True)
    # Immutable content-addressed blobs precede the atomic completion record.
    # An interrupted forced refresh leaves the previous record/blob usable.
    replace_with_sharing_wait(archive, directory / (metadata["archive_sha256"][:32] + ".zip"), control)
    atomic_json(directory / "complete.json", {"schema_version": 1, "source": metadata})
