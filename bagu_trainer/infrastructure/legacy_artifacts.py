"""Recognize application-produced legacy backups; partition by exact provenance."""
from dataclasses import dataclass
import hashlib
import json
import re

from bagu_trainer.infrastructure.provenance import question_sources


def legacy_name(name):
    return bool(re.fullmatch(r"questions_backup_pre_(?:source_excerpt|knowledge_tags)(?:_[A-Za-z0-9_]+)?\.json", name)
        or re.fullmatch(r"question_concepts_backup_pre_concept_rebuild_[A-Za-z0-9_]+\.json", name)
        or name in {"fallback_quizzes.json", "knowledge_tag_report.csv"})


@dataclass(frozen=True)
class LegacyRewrite:
    relative: str
    kept: str  # Empty means delete the entire file after the maintenance commit.
    unknown: int
    digest: str


def plan_legacy(data, repositories, index, sources, excluded=()):
    from bagu_trainer.infrastructure.cleanup_manifest import safe_path
    selected = {repo.casefold() for repo in repositories}
    # A directory reused by different repositories is ambiguous.
    directories = {}
    for source in sources:
        directories.setdefault(source.directory, []).append({"repository": source.repository, "commit": source.commit,
            "directory": source.directory})
    entries = [values[0] for values in directories.values() if len({v["repository"].casefold() for v in values}) == 1]
    result = []
    for path in sorted(data.iterdir() if data.exists() else []):
        if path.name in excluded or not legacy_name(path.name):
            continue
        safe_path(path)
        if not path.is_file():
            raise ValueError(f"旧式题库产物不是普通文件：{path}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            result.append(LegacyRewrite(path.name, "", 1, digest))
            continue
        unknown, removed = 0, 0
        if isinstance(value, list):
            keep = []
            for question in value:
                if not isinstance(question, dict):
                    unknown += 1
                    continue
                associations = question_sources(question, entries) or (
                    index.resolve(question.get("id", ""), question) if hasattr(index, "resolve") else index.get(question.get("id", ""), []))
                if not associations:
                    unknown += 1
                elif selected.intersection(s["repository"] for s in associations):
                    removed += 1
                else:
                    keep.append(question)
        elif isinstance(value, dict) and isinstance(value.get("profiles"), dict):
            keep = {}
            for kp_id, profile in value["profiles"].items():
                associations = index.get(kp_id, [])
                if not associations:
                    unknown += 1
                elif selected.intersection(s["repository"] for s in associations):
                    removed += 1
                else:
                    keep[kp_id] = profile
            keep = {"schema_version": value.get("schema_version", 1), "profiles": keep} if keep else None
        else:
            keep, unknown = None, 1
        if removed or unknown:
            result.append(LegacyRewrite(path.name, json.dumps(keep, ensure_ascii=False, sort_keys=True) if keep else "", unknown, digest))
    return tuple(result)
