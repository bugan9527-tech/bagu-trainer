from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-build.txt",
)


def _requirement_lines(path: Path) -> list[str]:
    content = path.read_bytes()
    assert content.endswith(b"\n"), f"{path.name} must end with a newline"
    text = content.decode("utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("filename", REQUIREMENTS_FILES)
def test_requirements_can_be_parsed(filename: str) -> None:
    lines = _requirement_lines(PROJECT_ROOT / filename)

    assert lines, f"{filename} must declare at least one dependency"
    for line in lines:
        if line.startswith("-r "):
            included = (PROJECT_ROOT / line[3:].strip()).resolve()
            assert included.is_relative_to(PROJECT_ROOT), "Requirement include escapes the project"
            assert included.is_file(), f"Missing included requirements: {included.name}"
            assert _requirement_lines(included)
            continue
        try:
            parsed = Requirement(line)
        except InvalidRequirement as exc:
            pytest.fail(f"{filename} contains invalid requirement {line!r}: {exc}")
        assert parsed.name
