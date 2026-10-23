"""Run every release module in a fresh process; merge all results, never retry.

Tk/ttkbootstrap own interpreter-global state. Module isolation prevents a dead
GUI interpreter in one module from altering another module's focus or styles.
Every module runs its unchanged assertions and every failure/error is retained.
"""
import argparse
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def merge_reports(reports, output, process_errors=()):
    result = ET.Element("testsuites")
    for path in reports:
        root = ET.parse(path).getroot()
        for suite in ([root] if root.tag == "testsuite" else root.findall("testsuite")):
            result.append(suite)
    for name, message in process_errors:
        suite = ET.SubElement(result, "testsuite", name=name, tests="1", failures="0", errors="1", skipped="0")
        case = ET.SubElement(suite, "testcase", classname=name, name="process_or_collection_error")
        ET.SubElement(case, "error", message=message).text = message
    totals = {key: sum(int(s.get(key, 0)) for s in result) for key in ("tests", "failures", "errors", "skipped")}
    result.attrib.update({key: str(value) for key, value in totals.items()})
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(result).write(output, encoding="utf-8", xml_declaration=True)
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    output = options.output.resolve()
    modules = sorted((ROOT / "release_tests").glob("test_*.py"))
    reports_dir = output.parent / (output.stem + "-modules")
    reports_dir.mkdir(parents=True, exist_ok=True)
    reports, errors = [], []
    failed_process = False
    for module in modules:
        report = reports_dir / (module.stem + ".xml")
        report.unlink(missing_ok=True)
        print("Checking " + module.name, flush=True)
        process = subprocess.run([sys.executable, "-B", "-m", "pytest", str(module), "-q", "-W", "error",
            "-p", "no:cacheprovider", "--junitxml=" + str(report)], cwd=ROOT)
        failed_process |= process.returncode != 0
        if report.exists():
            reports.append(report)
            try:
                cases = ET.parse(report).findall(".//testcase")
                if process.returncode and not any(c.find("failure") is not None or c.find("error") is not None for c in cases):
                    errors.append((module.stem, "Process failed without a JUnit failure: " + str(process.returncode)))
            except ET.ParseError:
                reports.pop()
                errors.append((module.stem, "Invalid JUnit report"))
        else:
            errors.append((module.stem, "Process produced no JUnit report: " + str(process.returncode)))
    if not modules:
        errors.append(("release_tests", "No release modules found"))
    totals = merge_reports(reports, output, errors)
    print(totals, flush=True)
    return 1 if failed_process or totals["failures"] or totals["errors"] or totals["skipped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
