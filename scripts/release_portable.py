"""Validate both Windows artifacts and evidence; create a draft-only Release."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bagu_trainer.version import __version__
from scripts.build_installer import APP_ID, SCRIPT, TOOLCHAIN, lifecycle_summary, safe_path, sha256
from scripts.build_portable import source_provenance
from scripts.acceptance.contract import complete_milestones, REQUESTS


def verify_checkout(target):
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() != target:
        raise ValueError("Release checkout must match the exact target commit")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True).strip():
        raise ValueError("Release source checkout must be clean")
    # Downloaded release files are intentionally untracked in the draft job;
    # every runtime/build source fingerprint must nevertheless be Git-owned.
    source_files = set(source_provenance()["files"])
    tracked = set(subprocess.check_output(["git", "ls-files", "--", *sorted(source_files)], cwd=ROOT, text=True).splitlines())
    if source_files != tracked:
        raise ValueError("Release source contains untracked runtime/build files")


def validate_release(directory, tag, target):
    if tag != "v" + __version__:
        raise ValueError("Tag must match bagu_trainer/version.py: v" + __version__)
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise ValueError("Release target must be a full commit SHA")
    archive = Path(directory) / f"BaguTrainer-{__version__}-windows-x64.zip"
    checksum = Path(str(archive) + ".sha256")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if checksum.read_text(encoding="ascii").strip() != digest + "  " + archive.name:
        raise ValueError("SHA-256 file does not match ZIP")
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("BaguTrainer/BUILD_MANIFEST.json"))
        if (manifest.get("source_commit") != target or manifest.get("source_dirty") is not False
                or manifest.get("version") != __version__ or manifest.get("architecture") != "windows-x64"):
            raise ValueError("ZIP provenance/version/architecture does not match the clean source commit")
        expected = {"BaguTrainer/" + item["path"]: item for item in manifest["files"]}
        for item in manifest["files"]:
            safe_path(item["path"])
        if len(expected) != len(manifest["files"]):
            raise ValueError("ZIP manifest contains duplicate inventory entries")
        names = [name for name in bundle.namelist() if not name.endswith("/")]
        if len(names) != len(set(names)) or set(names) != set(expected) | {"BaguTrainer/BUILD_MANIFEST.json"}:
            raise ValueError("ZIP inventory differs from BUILD_MANIFEST")
        for name, item in expected.items():
            value = bundle.read(name)
            if len(value) != item["bytes"] or hashlib.sha256(value).hexdigest() != item["sha256"]:
                raise ValueError("ZIP file checksum mismatch: " + name)
    return archive, checksum, digest


def render_notes(repository, tag, target, archive, digest, setup, setup_digest, *, tests=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    base = f"https://github.com/{repository}"
    download = base + "/releases/download/" + quote(tag, safe="") + "/" + archive.name
    install = base + "/releases/download/" + quote(tag, safe="") + "/" + setup.name
    validation = ("本次自动测试报告随附件提供；仅代表 Windows runner 上的隔离验证。" if tests is None else
                  f"本次自动测试：{tests['passed']} 项通过，{tests['skipped']} 项跳过。报告随附件提供。")
    return f"""# AI 八股训练器 {tag} · Windows x64

**[下载安装版 Setup.exe（推荐）]({install})** · **[安装版 SHA-256]({install}.sha256)**

另一个下载选项：**[便携 ZIP]({download})** · **[便携版 SHA-256]({download}.sha256)**

这是草稿 Release，仅仓库有权限的用户可见。正式发布后，上面的固定下载链接才对公众生效；
审阅草稿时请直接使用下方 Assets 附件下载。不要下载 GitHub 自动附带的 Source code ZIP 来代替应用。

## 首次使用

1. 双击 `Setup.exe`，按中文向导为当前用户安装，默认无需管理员权限。
   默认程序目录为 `%LOCALAPPDATA%\\Programs\\BaguTrainer`；桌面快捷方式可选。
   安装完成后从开始菜单启动。不需要安装 Python、Git 或配置 MCP。
   若选择便携版，完整解压 ZIP 后双击 `BaguTrainer.exe`，保留旁边的 `_internal`。
2. 在中文向导中选择题库来源，下载指定版本并等待构建成功；完整第三方题库不随程序分发。
   下载失败时检查网络或代理后点击重试，取消或失败不会覆盖旧题库。
3. 填写模型基础地址、模型名和 API Key，主动测试连接，成功后保存。
   Key 存入 Windows 凭据管理器；模型接口和首次下载需要网络。
4. 选择监听项目，进入主窗口。建立监听基线后修改代码，稳定约 15 秒且匹配知识点时更新练习中心待答提醒；
   在练习中心作答后显示判分和解析，学习记录自动保存。未匹配或模型不可用时允许没有题目。

配置、题库、日志和学习库都在 `%LOCALAPPDATA%\\BaguTrainer\\`，安装版与便携版共用。
升级安装版前正常退出训练器，再运行新 Setup；卸载默认保留个人数据和 Windows 凭据。
便携版退出后替换完整程序目录，移动程序后重新启用登录自启动。
旧源码旁的数据使用向导“迁移旧数据…”显式复制，旧数据保留，目标已有数据时拒绝覆盖。

## 校验与验证范围

ZIP SHA-256：`{digest}`

Setup SHA-256：`{setup_digest}`

构建提交：[{target}]({base}/commit/{target})。{validation}
模型链路自动化使用本地模拟 HTTP 接口，不调用真实模型或真实 Key。
安装门禁包括隔离夹具的跨版本升级/入口归属，以及最终 Setup 在临时 runner 上的
安装、快捷方式、同版覆盖、已安装 EXE 冻结验收、数据保留和卸载；脱敏报告随附件提供。
GitHub Windows runner 自带开发工具，禁用 PATH 的测试不等同于全新系统上卸载 Python/Git。
干净 Windows 实机验收、真实来源网络可用性、系统重启登录和未覆盖项以
[验证记录]({base}/blob/{target}/VALIDATION.md) 为准；此包尚未代码签名。
"""


def test_summary(path):
    root = ET.parse(path).getroot()
    cases = root.findall(".//testcase")
    failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    suites = [root, *root.findall(".//testsuite")]
    bad_totals = any(int(suite.get(key, "0")) for suite in suites for key in ("failures", "errors", "skipped"))
    if not cases or failed or skipped or bad_totals:
        raise ValueError("Release tests must all pass without skips")
    return {"passed": len(cases) - skipped, "skipped": skipped}


def acceptance_summary(path, target):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    if (report.get("passed") is not True or report.get("frozen") is not True
            or report.get("source_commit") != target or report.get("source_dirty") is not False
            or report.get("production_commit") != target or report.get("production_dirty") is not False
            or report.get("production_exe") != {"first_start": "running", "upgraded_start": "running", "history_retained": True}
            or any(report.get(phase, {}).get("passed") is not True or
                   not complete_milestones(phase, report.get(phase, {}).get("milestones")) for phase in ("first", "restart"))
            or report.get("requests") != REQUESTS):
        raise ValueError("Frozen acceptance/provenance/upgrade verification is incomplete")
    return report


def same_source(report, manifest, target):
    if (report.get("version") != __version__ or report.get("source_commit") != target
            or report.get("source_dirty") is not False
            or report.get("source_tree_sha256") != manifest["source_tree"]["sha256"]):
        raise ValueError("Installer evidence provenance/version does not match the clean payload")


def privacy_summary(path, target, tree, artifacts, evidence, metadata):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    names = {"release-tests.xml", "ACCEPTANCE.json", "INSTALLED_ACCEPTANCE.json", "INSTALLER_MANIFEST.json", "INSTALLER_LIFECYCLE.json"}
    expected = {name: sha256((evidence if (evidence / name).is_file() else metadata) / name) for name in names}
    if (report.get("passed") is not True or report.get("findings") != [] or report.get("source_only") is not False
            or report.get("credentials_accessed") is not False or report.get("findings_include_matched_values") is not False
            or report.get("version") != __version__ or report.get("source_commit") != target
            or report.get("source_tree_sha256") != tree or report.get("artifacts") != artifacts
            or report.get("attachment_files") != expected or report.get("code_objects_scanned", 0) < 1):
        raise ValueError("Privacy audit is missing, unsafe, incomplete or mismatched")
    return report


def validate_dual_release(directory, tag, target, evidence, metadata=None, *, require_privacy=True):
    archive, checksum, digest = validate_release(directory, tag, target)
    directory, evidence = Path(directory), Path(evidence)
    metadata = Path(metadata) if metadata is not None else directory
    with zipfile.ZipFile(archive) as bundle:
        raw = bundle.read("BaguTrainer/BUILD_MANIFEST.json")
    portable = json.loads(raw)
    if portable.get("source_tree") != source_provenance():
        raise ValueError("ZIP source fingerprint differs from checked-out release code")
    setup = directory / f"BaguTrainer-{__version__}-windows-x64-Setup.exe"
    setup_checksum = Path(str(setup) + ".sha256")
    setup_digest = sha256(setup)
    if setup_checksum.read_text(encoding="ascii").strip() != setup_digest + "  " + setup.name:
        raise ValueError("SHA-256 file does not match Setup")
    installer = json.loads((metadata / "INSTALLER_MANIFEST.json").read_text(encoding="utf-8"))
    expected_inputs = (SCRIPT, ROOT / "packaging/windows-installer-lifecycle.iss", TOOLCHAIN,
                       ROOT / "scripts/build_installer.py", ROOT / "packaging/BaguTrainer.ico",
                       ROOT / "packaging/languages/ChineseSimplified.isl", ROOT / "LICENSE")
    if (installer.get("application") != "BaguTrainer" or installer.get("architecture") != "windows-x64"
            or installer.get("app_id") != APP_ID or installer.get("version") != __version__
            or installer.get("source_commit") != target or installer.get("source_dirty") is not False
            or installer.get("source_tree") != portable["source_tree"]
            or installer.get("payload_manifest_sha256") != hashlib.sha256(raw).hexdigest()
            or installer.get("installer") != {"name": setup.name, "sha256": setup_digest}
            or installer.get("iscc_version") != json.loads(TOOLCHAIN.read_text(encoding="utf-8"))["inno_setup"]["version"]
            or installer.get("build_inputs") != {p.relative_to(ROOT).as_posix(): sha256(p) for p in expected_inputs}
            or installer.get("inspection") != {"method": "innounp extract; no installation",
                "files_verified": len(portable["files"]) + 2, "all_hashes_match": True, "application_executed": False}):
        raise ValueError("Setup provenance/payload/inspection differs from ZIP and current release code")
    lifecycle_path = metadata / "INSTALLER_LIFECYCLE.json"
    lifecycle = lifecycle_summary(lifecycle_path)
    same_source(json.loads(lifecycle_path.read_text(encoding="utf-8")), portable, target)
    if installer.get("lifecycle_acceptance") != lifecycle:
        raise ValueError("Setup lifecycle evidence digest differs")
    installed = json.loads((evidence / "INSTALLED_ACCEPTANCE.json").read_text(encoding="utf-8"))
    same_source(installed, portable, target)
    required = ("passed", "production_installer_executed", "personal_data_isolated", "installation", "registration",
                "shortcuts", "installed_frozen_acceptance", "same_version_overwrite", "enabled_startup_repaired",
                "installed_runtime", "uninstall", "personal_data_preserved", "registration_removed",
                "pristine_first_use", "no_preset_projects_history_or_key", "repeat_install", "running_upgrade_blocked", "running_uninstall_blocked",
                "default_install_path")
    if (any(installed.get(name) is not True for name in required)
            or installed.get("runner") != "github-hosted Windows"
            or installed.get("windows_credentials_accessed") is not False
            or installed.get("real_model_requests") is not False
            or installed.get("credential_backend") != "memory fixtures / null diagnostic backend"
            or installed.get("installer_sha256") != setup_digest
            or installed.get("payload_manifest_sha256") != installer["payload_manifest_sha256"]):
        raise ValueError("Final production Setup acceptance is incomplete, unsafe or mismatched")
    frozen = acceptance_summary(evidence / "ACCEPTANCE.json", target)
    if (frozen.get("production_source_tree") != portable["source_tree"]["sha256"]
            or frozen.get("source_tree", {}).get("sha256") != portable["source_tree"]["sha256"]
            or any(frozen.get(phase, {}).get("credential_backend") != "memory fixture" for phase in ("first", "restart"))):
        raise ValueError("Frozen acceptance source/isolated credentials differ from payload")
    tests = test_summary(evidence / "release-tests.xml")
    privacy = privacy_summary(evidence / "PRIVACY_AUDIT.json", target, portable["source_tree"]["sha256"],
                              {archive.name: digest, setup.name: setup_digest}, evidence, metadata) if require_privacy else None
    return {"archive": archive, "checksum": checksum, "digest": digest, "setup": setup,
            "setup_checksum": setup_checksum, "setup_digest": setup_digest, "tests": tests,
            "frozen": frozen, "installed": installed, "installer": installer, "lifecycle": lifecycle,
            "privacy": privacy,
            "lifecycle_report": json.loads(lifecycle_path.read_text(encoding="utf-8"))}


def write_public_evidence(result, source, output, target):
    """Allowlist report fields; raw diagnostics and test stdout stay in CI only."""
    output.mkdir(parents=True, exist_ok=True)
    raw = ET.parse(source / "release-tests.xml").getroot()
    public = ET.Element("testsuites", tests=str(result["tests"]["passed"]), failures="0", errors="0", skipped="0")
    suite = ET.SubElement(public, "testsuite", tests=str(result["tests"]["passed"]), failures="0", errors="0", skipped="0")
    for case in raw.findall(".//testcase"):
        attributes = {key: case.get(key, "") for key in ("classname", "name", "time")}
        # pytest parameter IDs can embed test credentials or private paths.
        # Keep every result and its function identity, but omit parameter values.
        attributes["name"] = attributes["name"].split("[", 1)[0]
        ET.SubElement(suite, "testcase", attributes)
    ET.ElementTree(public).write(output / "release-tests.xml", encoding="utf-8", xml_declaration=True)
    frozen = result["frozen"]
    safe = {key: frozen[key] for key in ("passed", "frozen", "source_commit", "source_dirty", "production_commit",
                                        "production_dirty", "production_source_tree", "production_exe")}
    safe["source_tree"] = {"sha256": frozen["source_tree"]["sha256"]}
    safe.update({phase: {key: frozen[phase][key] for key in ("passed", "credential_backend", "milestones")} for phase in ("first", "restart")})
    safe["requests"] = frozen["requests"]
    installed_keys = ("passed", "version", "source_commit", "source_dirty", "source_tree_sha256", "installer_sha256",
        "payload_manifest_sha256", "production_installer_executed", "runner", "credential_backend", "real_model_requests",
        "personal_data_isolated", "windows_credentials_accessed", "installation", "registration", "shortcuts",
        "installed_frozen_acceptance", "same_version_overwrite", "enabled_startup_repaired", "installed_runtime",
        "uninstall", "personal_data_preserved", "registration_removed", "pristine_first_use", "no_preset_projects_history_or_key",
        "repeat_install", "running_upgrade_blocked", "running_uninstall_blocked", "default_install_path")
    summary = {"passed": result["privacy"] is not None, "privacy_scan": "passed" if result["privacy"] else "pending",
               "version": __version__, "source_commit": target, "source_dirty": False,
               "tests": result["tests"], "zip_sha256": result["digest"], "setup_sha256": result["setup_digest"],
               "lifecycle_cases": result["lifecycle"]["cases_passed"], "production_setup_tested": True}
    for filename, value in (("ACCEPTANCE.json", safe), ("INSTALLED_ACCEPTANCE.json", {key: result["installed"][key] for key in installed_keys}),
                            ("RELEASE_VALIDATION.json", summary)):
        (output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raw_lifecycle = result["lifecycle_report"]
    lifecycle_keys = ("passed", "production_installer_executed", "windows_credentials_accessed", "paths_redirected_at_compile_time",
                      "version", "source_commit", "source_dirty", "source_tree_sha256", "lifecycle_sha256", "installer_script_sha256")
    case_keys = ("passed", "isolated_app_id", "foreign_shortcuts", "interactive_wait_dialog_resumed", "cooperative_wait_upgrade_and_uninstall",
                 "mapped_image_blocks_upgrade", "same_name_portable_process_preserved", "data_and_synthetic_credential_preserved",
                 "legacy_icon_log_compatible", "test_registration_removed")
    safe_lifecycle = {key: raw_lifecycle[key] for key in lifecycle_keys}
    safe_lifecycle["cases"] = [{key: case[key] for key in case_keys} for case in raw_lifecycle["cases"]]
    lifecycle_path = output / "INSTALLER_LIFECYCLE.json"
    lifecycle_path.write_text(json.dumps(safe_lifecycle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    installer_keys = ("application", "version", "architecture", "app_id", "source_commit", "source_dirty", "source_tree",
                      "payload_manifest_sha256", "installer", "iscc_version", "build_inputs", "inspection")
    safe_installer = {key: result["installer"][key] for key in installer_keys}
    safe_installer["lifecycle_acceptance"] = lifecycle_summary(lifecycle_path)
    (output / "INSTALLER_MANIFEST.json").write_text(json.dumps(safe_installer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if result["privacy"]:
        keys = ("schema_version", "version", "source_commit", "source_tree_sha256", "source_files", "source_only",
                "credentials_accessed", "findings_include_matched_values", "artifacts", "files_and_constants_scanned",
                "code_objects_scanned", "reviewed_synthetic_fixture_matches", "findings", "passed")
        safe_privacy = {key: result["privacy"][key] for key in keys}
        safe_privacy["attachment_files"] = {name: sha256(output / name) for name in result["privacy"]["attachment_files"]}
        (output / "PRIVACY_AUDIT.json").write_text(json.dumps(safe_privacy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def gh_json(*args):
    result = subprocess.run(["gh", *args], check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(result.stdout) if result.stdout.strip() else None


def publish_draft(repository, tag, target, notes, assets):
    # Check existing Git tags too: never attach a new binary to an unrelated tag.
    refs = gh_json("api", f"repos/{repository}/git/matching-refs/tags/{tag}")
    matching = [item for item in refs if item["ref"] == "refs/tags/" + tag]
    if matching:
        obj = matching[0]["object"]
        while obj["type"] == "tag":
            obj = gh_json("api", f"repos/{repository}/git/tags/{obj['sha']}")["object"]
        if obj["type"] != "commit" or obj["sha"] != target:
            raise ValueError("Existing tag points to a different commit; choose a new version")
    pages = gh_json("api", f"repos/{repository}/releases?per_page=100", "--paginate", "--slurp")
    existing = next((item for page in pages for item in page if item["tag_name"] == tag), None)
    if existing and not existing["draft"]:
        raise ValueError("Refusing to change an already published Release")
    if existing and existing["target_commitish"] != target:
        raise ValueError("Existing draft belongs to a different commit; choose a new version")
    command = ["gh", "release"]
    if existing:
        subprocess.run([*command, "upload", tag, *map(str, assets), "--repo", repository, "--clobber"], check=True)
        subprocess.run([*command, "edit", tag, "--repo", repository, "--draft=true", "--notes-file", str(notes)], check=True)
    else:
        subprocess.run([*command, "create", tag, *map(str, assets), "--repo", repository, "--target", target,
                        "--draft", "--title", f"BaguTrainer {tag} · Windows x64", "--notes-file", str(notes)], check=True)
    result = gh_json("release", "view", tag, "--repo", repository, "--json", "isDraft,url")
    if not result["isDraft"]:
        raise RuntimeError("Release is unexpectedly public")
    print(result["url"])
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write(f"[查看草稿 Release]({result['url']})\n\n正式发布仍需维护者在 GitHub 手动确认。\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--output", type=Path, default=ROOT / "validation" / "public" / "RELEASE_NOTES.md")
    parser.add_argument("--evidence", type=Path, default=ROOT / "validation", help="Raw or already sanitized acceptance reports")
    parser.add_argument("--metadata", type=Path, help="Installer build/lifecycle reports; defaults to --dist")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "bugan9527-tech/bagu-trainer"))
    parser.add_argument("--target", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--publish-draft", action="store_true")
    parser.add_argument("--prepare-evidence", action="store_true", help="Prepare sanitized evidence before privacy scan; never upload")
    options = parser.parse_args()
    target = options.target or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    verify_checkout(target)
    if options.prepare_evidence and options.publish_draft:
        parser.error("--prepare-evidence cannot publish a draft")
    result = validate_dual_release(options.dist, options.tag, target, options.evidence, options.metadata,
                                   require_privacy=not options.prepare_evidence)
    notes = render_notes(options.repository, options.tag, target, result["archive"], result["digest"],
                         result["setup"], result["setup_digest"], tests=result["tests"])
    write_public_evidence(result, options.evidence, options.output.parent, target)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(notes, encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT") and not options.prepare_evidence:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"tag={options.tag}\n")
    if options.publish_draft:
        assets = [result[key] for key in ("setup", "setup_checksum", "archive", "checksum")]
        assets.extend(options.output.parent / name for name in
                      ("release-tests.xml", "ACCEPTANCE.json", "INSTALLED_ACCEPTANCE.json", "RELEASE_VALIDATION.json",
                       "INSTALLER_MANIFEST.json", "INSTALLER_LIFECYCLE.json"))
        assets.append(options.output.parent / "PRIVACY_AUDIT.json")
        publish_draft(options.repository, options.tag, target, options.output, assets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
