"""A combined report must preserve failed, skipped and crashed modules."""
import xml.etree.ElementTree as ET

from scripts.run_release_tests import merge_reports


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
