"""Exact repository identities carried from bank snapshots into learning data."""
import json
import re
from pathlib import PurePosixPath


def normalize(entries):
    result = set()
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        repo, commit = entry.get("repository", ""), entry.get("commit", "")
        if (isinstance(repo, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repo)
                and isinstance(commit, str) and re.fullmatch(r"[a-fA-F0-9]{40}", commit)):
            result.add((repo.casefold(), commit.lower()))
    return [{"repository": repo, "commit": commit} for repo, commit in sorted(result)]


def question_sources(question, entries):
    explicit = normalize(question.get("sources"))
    if explicit:
        return explicit
    directories = {entry.get("directory"): entry for entry in entries}
    sources = []
    for name in question.get("source_paths", ()):
        parts = PurePosixPath(str(name).replace("\\", "/")).parts
        if parts and parts[0] in directories:
            sources.append(directories[parts[0]])
    return normalize(sources)


class ExactSourceIndex(dict):
    def __init__(self, identities, paths):
        # Sequential legacy IDs can collide across repositories. Such IDs are
        # ambiguous, not multi-source evidence for an individual old snapshot.
        super().__init__({key: normalize([s for values in variants for s in values])
            for key, variants in identities.items()
            if len({tuple(sorted(s["repository"] for s in values)) for values in variants}) == 1})
        self.paths = {key: normalize([s for values in variants for s in values]) for key, variants in paths.items()
            if len({tuple(sorted(s["repository"] for s in values)) for values in variants}) == 1}

    def resolve(self, kp_id, snapshot):
        values = []
        names = snapshot.get("source_paths") or []
        reference = snapshot.get("excerpt_source")
        if isinstance(reference, dict) and isinstance(reference.get("repo"), str) and isinstance(reference.get("path"), str):
            names = [*names, reference["repo"] + "/" + reference["path"]]
        for name in names:
            if isinstance(name, str) and name in self.paths:
                values.extend(self.paths[name])
        return normalize(values) or self.get(kp_id, [])


def bank_index(versions):
    """Legacy recovery uses exact stable IDs and exact source path components."""
    result, paths = {}, {}
    for directory in versions:
        record, questions = directory / "sources.json", directory / "questions.json"
        if not record.is_file() or not questions.is_file():
            continue
        entries = json.loads(record.read_text(encoding="utf-8"))["sources"]
        for question in json.loads(questions.read_text(encoding="utf-8")):
            values = question_sources(question, entries)
            if values:
                result.setdefault(question["id"], []).append(values)
                for name in question.get("source_paths", []):
                    paths.setdefault(name, []).append(values)
    return ExactSourceIndex(result, paths)
