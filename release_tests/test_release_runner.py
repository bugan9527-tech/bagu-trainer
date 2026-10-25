"""A combined report must preserve failed, skipped and crashed modules."""
import xml.etree.ElementTree as ET
import json

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
