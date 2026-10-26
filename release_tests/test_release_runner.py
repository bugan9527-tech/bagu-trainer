"""A combined report must preserve failed, skipped and crashed modules."""
import xml.etree.ElementTree as ET
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts.run_release_tests import merge_reports
from scripts import run_release_tests as runner


def test_merge_retains_every_case_and_process_failure(tmp_path):
    first, second = tmp_path / "one.xml", tmp_path / "two.xml"
    first.write_text('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="pass"/></testsuite></testsuites>')
    second.write_text('<testsuites><testsuite tests="2" failures="1" errors="0" skipped="1"><testcase name="fail"><failure/></testcase><testcase name="skip"><skipped/></testcase></testsuite></testsuites>')
    output = tmp_path / "combined.xml"
    totals = merge_reports([first, second], output, [("crashed", "No report")])
    assert totals == {"tests": 4, "failures": 1, "errors": 1, "skipped": 1}
    root = ET.parse(output).getroot()
    assert len(root.findall(".//testcase")) == 4
    assert root.find(".//error").text == "No report"


def test_old_bundle_is_rejected_before_any_test_process(tmp_path, monkeypatch):
    bundle = tmp_path / "BaguTrainer"
    bundle.mkdir()
    (bundle / "BUILD_MANIFEST.json").write_text(json.dumps({"version": "0.3.0"}), encoding="utf-8")
    monkeypatch.setenv("BAGU_PORTABLE_BUNDLE", str(bundle))
    monkeypatch.setenv("BAGU_PORTABLE_ARCHIVE", str(tmp_path / "old.zip"))
    monkeypatch.setattr(runner.sys, "argv", ["run_release_tests.py", "--module", "test_release_runner",
                                           "--output", str(tmp_path / "report.xml")])
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: pytest.fail("Old bundle reached test processes"))
    assert runner.main() == 1
    root = ET.parse(tmp_path / "report.xml").getroot()
    assert root.get("errors") == "1"
    assert "0.3.0" in root.find(".//error").text


@pytest.mark.parametrize("module", ["test_ui_shell", "test_ui_display", "test_ui_layout", "test_ui_theme"])
def test_native_ui_cases_run_once_in_separate_processes_and_keep_failures(tmp_path, monkeypatch, module):
    source = tmp_path / "release_tests"
    source.mkdir()
    (source / (module + ".py")).write_text("# synthetic collection only\n")
    output = tmp_path / "report.xml"
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner.sys, "argv", ["run_release_tests.py", "--module", module,
                                           "--output", str(output)])
    target = SimpleNamespace(bundle=tmp_path / "bundle", archive=tmp_path / "bundle.zip", describe=lambda: {})
    monkeypatch.setattr(runner, "resolve_target", lambda *_: target)
    monkeypatch.setattr(runner, "validate_target", lambda *_: {"passed": True})
    cases = ["release_tests/" + module + ".py::test_layout[100]",
             "release_tests/" + module + ".py::test_layout[150]"]
    executed = []
    def run(command, **_kwargs):
        if "--collect-only" in command:
            return SimpleNamespace(returncode=0, stdout="\n".join(cases), stderr="")
        executed.append(command[5])
        assert "-W" in command and command[command.index("-W") + 1] == "error"
        report = next(part.split("=", 1)[1] for part in command if part.startswith("--junitxml="))
        failed = len(executed) == 1
        Path(report).write_text('<testsuites><testsuite tests="1" failures="' + str(int(failed)) +
                               '" errors="0" skipped="0"><testcase name="layout">' +
                               ('<failure message="synthetic failure"/>' if failed else '') +
                               '</testcase></testsuite></testsuites>')
        return SimpleNamespace(returncode=int(failed))
    monkeypatch.setattr(runner.subprocess, "run", run)
    assert runner.main() == 1
    assert executed == cases
    assert ET.parse(output).getroot().get("failures") == "1"
