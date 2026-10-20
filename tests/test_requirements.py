from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-demo.txt",
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
        try:
            parsed = Requirement(line)
        except InvalidRequirement as exc:
            pytest.fail(f"{filename} contains invalid requirement {line!r}: {exc}")
        assert parsed.name
