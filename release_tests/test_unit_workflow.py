"""The source unit gate must block artifact transfer and preserve failed reports.

The unit step also runs against synthetic passing/failing tests in temporary
directories. No Actions dispatch, installer, credentials or uploads are used.
actionlint validates YAML syntax.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/windows-portable.yml"


def step(workflow, name):
    marker = "      - name: " + name + "\n"
    lines = []
    for line in workflow.split(marker, 1)[1].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) < 8:
            break
        lines.append(line)
    return "\n".join(lines)


def test_unit_changes_trigger_the_main_pull_request_workflow():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    filters = workflow.split("  pull_request:\n", 1)[1].split("  workflow_dispatch:", 1)[0]
    assert "      - 'tests/**'" in filters
    assert "      - 'release_tests/**'" in filters


def test_unit_gate_is_isolated_strict_and_precedes_every_release_acceptance():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    unit = step(workflow, "Run isolated unit tests before release acceptance")
    assert "python -B -m pytest tests -q -W error --junitxml=validation/unit-tests.xml" in unit
    assert "exit $LASTEXITCODE" in unit
    assert "BAGU_USER_DATA_DIR: ${{ runner.temp }}/bagu-unit-tests-${{ github.run_id }}-${{ github.run_attempt }}" in unit
    assert "BAGU_TEST_INSTANCE: ci-unit-tests" in unit
    assert "PYTHON_KEYRING_BACKEND: keyring.backends.null.Keyring" in unit
    assert "BAGU_TEST_BANK_ROOT: ''" in unit
    assert "continue-on-error" not in workflow
    assert "if:" not in unit  # default success(): prerequisites must pass
    unit_position = workflow.index("id: unit_tests")
    for command in ("build_portable.py --require-clean", "run_release_tests.py",
                    "build_acceptance.py --verify --memory-credentials",
                    "installer_lifecycle.py", "build_installer.py", "installed_release.py"):
        assert unit_position < workflow.index(command)
    assert "needs: build-and-test" in workflow
    for name in ("Transfer verified release files", "Preserve clean Windows acceptance kit separately"):
        assert "if:" not in step(workflow, name)


def test_failed_units_still_upload_the_report_without_bypassing_the_gate():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    upload = step(workflow, "Preserve unit test report and runtime diagnostics")
    assert "if: always()" in upload
    assert "uses: actions/upload-artifact@" in upload
    assert "validation/unit-tests.xml" in upload
    assert "validation/unit-runtime.txt" in upload
    assert "name: windows-unit-tests-${{ github.run_id }}-${{ github.run_attempt }}" in upload
    assert workflow.index("id: unit_tests") < workflow.index("Preserve unit test report")
    assert workflow.index("Preserve unit test report") < workflow.index("build_portable.py --require-clean")


def test_windows_runtime_is_checked_by_creating_a_real_tk_window_before_units():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    runtime = step(workflow, "Verify Windows Tcl/Tk and Git for unit tests")
    assert "runs-on: windows-2022" in workflow
    assert "python-version: '3.10.11'" in workflow
    assert "git --version" in runtime  # missing Git must fail, not skip its tests
    for operation in ("create_root()", "root.update()", "root.destroy()"):
        assert operation in runtime
    assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in runtime
    assert workflow.index("Verify Windows Tcl/Tk") < workflow.index("id: unit_tests")


@pytest.mark.parametrize("fails", [False, True])
def test_real_unit_step_preserves_exit_status_and_junit_on_failure(tmp_path, fails):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    body = step(workflow, "Run isolated unit tests before release acceptance")
    command = textwrap.dedent(body.split("        run: |\n", 1)[1])
    shell = shutil.which("pwsh")
    assert shell, "The Windows workflow requires PowerShell Core"
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_gate.py").write_text("def test_gate():\n    assert " + str(not fails) + "\n")
    environment = dict(os.environ, BAGU_USER_DATA_DIR=str(tmp_path / "isolated-profile"),
                       BAGU_TEST_INSTANCE="gate-exit-status",
                       PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
                       BAGU_TEST_BANK_ROOT="",
                       PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == (1 if fails else 0), result.stdout + result.stderr
    report = ET.parse(tmp_path / "validation/unit-tests.xml")
    cases = report.findall(".//testcase")
    assert len(cases) == 1
    assert (cases[0].find("failure") is not None) == fails
