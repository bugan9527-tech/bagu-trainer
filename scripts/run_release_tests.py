"""Run every release module in a fresh process; merge all results, never retry.

Tk/ttkbootstrap own interpreter-global state. Module isolation prevents a dead
GUI interpreter in one module from altering another module's focus or styles.
Every module runs its unchanged assertions and every failure/error is retained.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.portable_artifacts import require_new, resolve_target, validate_target


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
    parser.add_argument("--output-dir", type=Path, help="Production build output, shared with build_portable.py")
    parser.add_argument("--module", action="append", help="Run specified test_*.py modules, each in its own process")
    options = parser.parse_args()
    output = options.output.resolve()
    artifact_report = output.with_suffix(".artifact.json")
    reports_dir = output.parent / (output.stem + "-modules")
    require_new(output, artifact_report, reports_dir)
    modules = sorted((ROOT / "release_tests").glob("test_*.py"))
    if options.module:
        requested = set(options.module)
        modules = [module for module in modules if module.stem in requested or module.name in requested]
        if {m.stem for m in modules} != {Path(name).stem for name in requested}:
            parser.error("Unknown release test module")
    target = resolve_target(options.output_dir)
    record = target.describe()
    try:
        record = validate_target(target)
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile) as error:
        record.update(passed=False, error=str(error))
        merge_reports([], output, [("artifact_preflight", str(error))])
        artifact_report.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Artifact preflight failed: " + str(error), flush=True)
        return 1
    artifact_report.parent.mkdir(parents=True, exist_ok=True)
    artifact_report.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    # Child pytest imports must resolve the exact target that passed preflight.
    environment = dict(os.environ, BAGU_PORTABLE_BUNDLE=str(target.bundle), BAGU_PORTABLE_ARCHIVE=str(target.archive))
    reports_dir.mkdir(parents=True, exist_ok=True)
    reports, errors = [], []
    failed_process = False
    targets = []
    for module in modules:
        relative = module.relative_to(ROOT).as_posix()
        if module.stem in {"test_ui_shell", "test_ui_display"}:
            # Native window state survives Tk destruction on Windows. Each
            # shell scenario (including each scale) needs a fresh GUI process.
            collected = subprocess.run([sys.executable, "-B", "-m", "pytest", relative,
                "--collect-only", "-q", "-p", "no:cacheprovider"], cwd=ROOT, env=environment, capture_output=True, text=True)
            cases = [line.strip() for line in collected.stdout.splitlines() if line.startswith(relative + "::")]
            if collected.returncode or not cases:
                errors.append((module.stem, "Shell test collection failed: " + collected.stdout + collected.stderr))
            targets.extend((module.stem + "-" + str(index), case) for index, case in enumerate(cases))
        else:
            targets.append((module.stem, relative))
    for name, target in targets:
        report = reports_dir / (name + ".xml")
        report.unlink(missing_ok=True)
        print("Checking " + target, flush=True)
        try:
            process = subprocess.run([sys.executable, "-u", "-B", "-m", "pytest", target, "-vv", "-W", "error",
                "-o", "faulthandler_timeout=90", "-p", "no:cacheprovider", "--junitxml=" + str(report)], cwd=ROOT, env=environment, timeout=240)
        except subprocess.TimeoutExpired:
            errors.append((name, "Test process timed out after 240 seconds: " + target))
            failed_process = True
            continue
        failed_process |= process.returncode != 0
        if report.exists():
            reports.append(report)
            try:
                cases = ET.parse(report).findall(".//testcase")
                if process.returncode and not any(c.find("failure") is not None or c.find("error") is not None for c in cases):
                    errors.append((name, "Process failed without a JUnit failure: " + str(process.returncode)))
            except ET.ParseError:
                reports.pop()
                errors.append((name, "Invalid JUnit report"))
        else:
            errors.append((name, "Process produced no JUnit report: " + str(process.returncode)))
    if not modules:
        errors.append(("release_tests", "No release modules found"))
    totals = merge_reports(reports, output, errors)
    print(totals, flush=True)
    return 1 if failed_process or totals["failures"] or totals["errors"] or totals["skipped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
