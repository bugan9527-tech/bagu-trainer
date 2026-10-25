"""Wrap an audited production onedir, then extract/verify it WITHOUT installing.

Requires requirements-build.txt, Inno Setup 6.7.3 and innounp 2.67+.
Only version.py is authoritative. No application/config/keyring code is run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bagu_trainer.version import __version__
from scripts.build_portable import audit, source_provenance
from scripts.portable_artifacts import require_new, resolve_target

APP_ID = "{C95D2707-83B5-4D34-A88D-7F30C5AC6E19}"
PRODUCT = "AI 八股训练器"
SCRIPT = ROOT / "packaging" / "windows-installer.iss"
TOOLCHAIN = ROOT / "packaging" / "installer-toolchain.json"


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _digest(stream)


def _digest(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def safe_path(value):
    if not isinstance(value, str):
        raise ValueError("清单包含不安全的路径")
    path = PurePosixPath(value)
    if (not isinstance(value, str) or not value or path.is_absolute()
            or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in value.split("/"))
            or any(c in value for c in '\\:*?"<>|{}\r\n\x00')):
        raise ValueError("清单包含不安全的路径")
    return path


def pe_resources(path, *, production=False):
    try:
        import pefile
    except ImportError as error:
        raise RuntimeError("缺少 PE 检查依赖；请使用安装了 requirements-build.txt 的构建环境") from error
    with pefile.PE(str(path)) as pe:
        if production and pe.FILE_HEADER.Machine != 0x8664:
            raise ValueError("生产 EXE 必须为 Windows x64")
        strings = {}
        for group in getattr(pe, "FileInfo", []):
            for item in group:
                for table in getattr(item, "StringTable", []):
                    strings.update({k.decode("utf-8"): v.decode("utf-8").strip(" \x00") for k, v in table.entries.items()})
        if strings.get("ProductName") != PRODUCT or strings.get("ProductVersion") != __version__:
            raise ValueError("EXE 缺少当前产品/版本资源；请重新运行 scripts/build_portable.py")
        if production and strings.get("OriginalFilename") != "BaguTrainer.exe":
            raise ValueError("只能封装 BaguTrainer 生产 EXE")
        info = pe.VS_FIXEDFILEINFO[0]
        numeric = tuple(map(int, __version__.split("."))) + (0,)
        actual_version = (info.FileVersionMS >> 16, info.FileVersionMS & 0xFFFF,
                          info.FileVersionLS >> 16, info.FileVersionLS & 0xFFFF)
        if actual_version != numeric:
            raise ValueError("EXE 数值文件版本与 version.py 不符")
        resource_types = {entry.id for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries}
        if not {3, 14} <= resource_types:
            raise ValueError("EXE 缺少产品图标资源")
        return strings


def validate_bundle(bundle, *, current_source=True, check_exe=True):
    bundle = Path(bundle)
    if not bundle.is_dir() or bundle.is_symlink():
        raise ValueError("生产目录不存在或是链接；请先运行 scripts/build_portable.py")
    # audit runs BEFORE loading any files, rejecting sensitive paths by name.
    actual = audit(bundle)
    manifest_path = bundle / "BUILD_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("application") != "BaguTrainer" or manifest.get("version") != __version__
            or manifest.get("architecture") != "windows-x64" or manifest.get("layout") != "PyInstaller onedir"):
        raise ValueError("生产清单的产品、版本、架构或 onedir 布局不符")
    expected = {}
    for item in manifest["files"]:
        safe_path(item["path"])
        name = item["path"].casefold()
        if name in expected or name == "build_manifest.json":
            raise ValueError("生产清单包含重复路径")
        expected[name] = item
    actual_files = [item for item in actual if item["path"] != "BUILD_MANIFEST.json"]
    if len(actual_files) != len(expected) or any(expected.get(item["path"].casefold()) != item for item in actual_files):
        raise ValueError("生产目录与 BUILD_MANIFEST 不一致：存在新增、缺失或变更文件")
    names = {item["path"] for item in actual_files}
    required = {"BaguTrainer.exe", "BaguCleanup.exe", "使用说明.md", "_internal/LICENSE", "_internal/THIRD_PARTY_NOTICES.txt",
                "_internal/interview-banks/sources.json", "_internal/licenses/Python-LICENSE.txt",
                "_internal/_tcl_data/init.tcl", "_internal/_tk_data/tk.tcl"}
    if not required <= names or not any(name.startswith("_internal/") and name.endswith(".dll") for name in names):
        raise ValueError("缺少生产运行资源或许可文件")
    if current_source and manifest.get("source_tree") != source_provenance():
        raise ValueError("生产目录来自旧源码；请先重新运行 scripts/build_portable.py")
    if check_exe:
        pe_resources(bundle / "BaguTrainer.exe", production=True)
        try:
            from PyInstaller.archive.readers import CArchiveReader
            archive = CArchiveReader(str(bundle / "BaguTrainer.exe"))
            modules = set(archive.toc)
            for name, item in archive.toc.items():
                if item[-1] == "z":
                    modules.update(archive.open_embedded_archive(name).toc)
            if "start_desktop" not in modules or "bagu_trainer.desktop" not in modules:
                raise ValueError("生产 EXE 入口或运行模块不符")
            if any(re.search(r"(^|[._/])(acceptance|desktop_acceptance|tests|release_tests|pytest)([._/]|$)", name)
                   for name in modules):
                raise ValueError("生产 EXE 包含测试/验收模块")
        except ImportError as error:
            raise RuntimeError("缺少 PyInstaller；请安装 requirements-build.txt") from error
    return manifest, actual


def find_tool(explicit, name, candidates):
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_file():
            raise RuntimeError(f"找不到 {name}：{path}")
        return path
    found = shutil.which(name)
    if found:
        return Path(found).resolve()
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise RuntimeError(f"缺少 {name}。请安装构建工具，或通过 --{'iscc' if name == 'ISCC.exe' else 'innounp'} 指定路径；不会自动安装工具或应用")


def check_compiler(path):
    # ISCC /? and its PE resources do not reliably expose a release version.
    # Pin the executable from the verified official installer instead.
    tool = json.loads(TOOLCHAIN.read_text(encoding="utf-8"))["inno_setup"]
    if sha256(path) != tool["iscc_sha256"]:
        raise RuntimeError(f"ISCC 与锁定工具链不符，要求官方 {tool['version']} 的编译器")
    return tool["version"]


def check_unpacker(path):
    tool = json.loads(TOOLCHAIN.read_text(encoding="utf-8"))["inspection"]
    if sha256(path) != tool["executable_sha256"]:
        raise RuntimeError(f"innounp 与锁定检查工具不符，要求 {tool['version']}；请核对工具链文件")


def lifecycle_summary(path):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    helper = ROOT / "packaging" / "windows-installer-lifecycle.iss"
    if (report.get("passed") is not True or report.get("production_installer_executed") is not False
            or report.get("windows_credentials_accessed") is not False
            or report.get("paths_redirected_at_compile_time") is not True
            or report.get("lifecycle_sha256") != sha256(helper)
            or report.get("installer_script_sha256") != sha256(SCRIPT)):
        raise ValueError("安装生命周期验收缺失、失败或不匹配当前安装脚本")
    cases = report.get("cases", [])
    required = ("passed", "interactive_wait_dialog_resumed", "cooperative_wait_upgrade_and_uninstall",
                "mapped_image_blocks_upgrade", "same_name_portable_process_preserved",
                "data_and_synthetic_credential_preserved", "legacy_icon_log_compatible", "test_registration_removed")
    if (len(cases) != 2 or {case.get("foreign_shortcuts") for case in cases} != {False, True}
            or any(not case.get("isolated_app_id", "").startswith("BaguTrainer.Lifecycle.")
                   or any(case.get(field) is not True for field in required) for case in cases)):
        raise ValueError("安装生命周期验收场景不完整或测试注册未清理")
    return {"method": "isolated compiled fixture; shared production Pascal Script", "cases_passed": len(cases),
            "report_sha256": sha256(path), "lifecycle_sha256": report["lifecycle_sha256"],
            "production_installer_executed": False, "windows_credentials_accessed": False}


def iss_quote(value):
    return str(value).replace('"', '""').replace("{", "{{")


def write_payload_include(stage, entries, work):
    lines = ["[Files]"]
    for item in entries:
        relative = Path(item["path"])
        destination = "{app}" + ("\\" + iss_quote(str(relative.parent)) if relative.parent != Path(".") else "")
        lines.append(f'Source: "{iss_quote(stage / relative)}"; DestDir: "{destination}"; Flags: ignoreversion')
    # Install-time ownership inventory is explicit installer metadata, never user data.
    inventory = work / "BaguTrainer.install-files.txt"
    inventory.write_text("\n".join(item["path"].replace("/", "\\") for item in entries) + "\n", encoding="utf-8-sig")
    lines.append(f'Source: "{iss_quote(inventory)}"; DestDir: "{{app}}"; Flags: ignoreversion')
    include = work / "payload.iss"
    include.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return include, inventory


def inspect_installer(installer, unpacker, entries, inventory, destination):
    """innounp is an archive reader: it never executes Setup or Pascal Script."""
    subprocess.run([str(unpacker), "-x", "-m", "-a", "-y", "-b", "-d" + str(destination), str(installer)], check=True,
                   stdout=subprocess.DEVNULL)
    app = destination / "{app}"
    expected = {item["path"]: item for item in entries}
    expected[inventory.name] = {"path": inventory.name, "bytes": inventory.stat().st_size, "sha256": sha256(inventory)}
    files = {path.relative_to(app).as_posix(): path for path in app.rglob("*") if path.is_file()}
    if set(files) != set(expected):
        raise ValueError("安装器解包内容与已审计生产目录不符")
    for name, path in files.items():
        if path.stat().st_size != expected[name]["bytes"] or sha256(path) != expected[name]["sha256"]:
            raise ValueError("安装器解包文件 hash 不一致：" + name)
    # Only Inno's generated uninstaller may exist outside the application payload.
    internal = {"install_script.iss", "embedded/License.txt", "embedded/CompiledCode.bin", "embedded/WizardImage0.png",
                "embedded/WizardSmallImage0.png", "embedded/chinesesimplified.isl", "embedded/uninstall.exe"}
    extra = [path.relative_to(destination).as_posix() for path in destination.rglob("*")
             if path.is_file() and app not in path.parents and path.relative_to(destination).as_posix() not in internal]
    if extra:
        raise ValueError("安装器包含意外的非生产内容：" + ", ".join(extra))
    if sha256(destination / "embedded" / "License.txt") != sha256(ROOT / "LICENSE"):
        raise ValueError("安装向导许可协议与仓库 LICENSE 不一致")
    recovered = (destination / "install_script.iss").read_text(encoding="utf-8-sig")
    for setting in ("AppId={" + APP_ID, f"AppName={PRODUCT}", f"AppVersion={__version__}",
                    "PrivilegesRequired=lowest", "DefaultDirName={localappdata}\\Programs\\BaguTrainer"):
        if setting not in recovered:
            raise ValueError("安装器恢复脚本缺少预期设置：" + setting)
    return {"method": "innounp extract; no installation", "files_verified": len(expected),
            "all_hashes_match": True, "application_executed": False}


def build(bundle, output, iscc, unpacker, lifecycle_report=None):
    final = output.resolve() / f"BaguTrainer-{__version__}-windows-x64-Setup.exe"
    require_new(final, Path(str(final) + ".sha256"), output / "INSTALLER_MANIFEST.json")
    version = check_compiler(iscc)
    check_unpacker(unpacker)
    lifecycle = lifecycle_summary(lifecycle_report) if lifecycle_report else "not run; use isolated lifecycle acceptance"
    manifest, entries = validate_bundle(bundle)
    output = output.resolve()
    bundle = bundle.resolve()
    if output == bundle or bundle in output.parents:
        raise ValueError("安装器输出目录不能位于生产目录中")
    work_root = ROOT / "build"
    work_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="installer-", dir=work_root) as temporary:
        work = Path(temporary)
        stage = work / "payload"
        shutil.copytree(bundle, stage)
        # Freeze the audited snapshot against changes to dist during compilation.
        _, staged_entries = validate_bundle(stage)
        if staged_entries != entries:
            raise ValueError("复制期间生产目录发生变化")
        include, inventory = write_payload_include(stage, entries, work)
        generated = work / "compiled"
        generated.mkdir()
        subprocess.run([str(iscc), "/Qp", f"/DAppVersion={__version__}", f"/DPayloadInclude={include}",
                        f"/DOutputPath={generated}", str(SCRIPT)], check=True, cwd=ROOT)
        installer = generated / f"BaguTrainer-{__version__}-windows-x64-Setup.exe"
        pe_resources(installer)
        inspection = inspect_installer(installer, unpacker, entries, inventory, work / "inspection")
        if validate_bundle(bundle)[1] != entries:
            raise ValueError("编译期间生产目录发生变化")
        report = {"application": "BaguTrainer", "version": __version__, "architecture": "windows-x64",
                  "app_id": APP_ID, "source_commit": manifest["source_commit"], "source_dirty": manifest["source_dirty"],
                  "source_tree": manifest["source_tree"], "payload_manifest_sha256": sha256(stage / "BUILD_MANIFEST.json"),
                  "installer": {"name": installer.name, "sha256": sha256(installer)}, "iscc_version": version,
                  "build_inputs": {path.relative_to(ROOT).as_posix(): sha256(path) for path in
                                   (SCRIPT, ROOT / "packaging" / "windows-installer-lifecycle.iss",
                                    TOOLCHAIN, Path(__file__).resolve(), ROOT / "packaging" / "BaguTrainer.ico",
                                    ROOT / "packaging" / "languages" / "ChineseSimplified.isl", ROOT / "LICENSE")},
                  "inspection": inspection, "lifecycle_acceptance": lifecycle,
                  "signed": False}
        if lifecycle_report and sha256(lifecycle_report) != lifecycle["report_sha256"]:
            raise ValueError("构建期间安装生命周期报告发生变化")
        output.mkdir(parents=True, exist_ok=True)
        final = output / installer.name
        shutil.copyfile(installer, final)
        Path(str(final) + ".sha256").write_text(report["installer"]["sha256"] + "  " + final.name + "\n", encoding="ascii")
        (output / "INSTALLER_MANIFEST.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if lifecycle_report:
            if Path(lifecycle_report).resolve() != output / "INSTALLER_LIFECYCLE.json":
                shutil.copyfile(lifecycle_report, output / "INSTALLER_LIFECYCLE.json")
        print(final)
        print("解包审计通过；未执行安装，未启动应用，未发布。")
        return final


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--iscc", type=Path, help="Inno Setup 6.7.3 ISCC.exe")
    parser.add_argument("--innounp", type=Path, help="innounp.exe，必须支持 Inno Setup 6.7")
    parser.add_argument("--lifecycle-report", type=Path, help="同一安装脚本的隔离升级/卸载验收报告")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "win32":
            raise RuntimeError("安装器构建要求 Windows x64 构建环境")
        candidates = [Path(os.environ.get(name, "C:/")) / "Inno Setup 6" / "ISCC.exe"
                      for name in ("ProgramFiles(x86)", "ProgramFiles")]
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "C:/")) / "Programs" / "Inno Setup 6" / "ISCC.exe")
        iscc = find_tool(args.iscc, "ISCC.exe", candidates)
        unpacker = find_tool(args.innounp, "innounp.exe", [])
        target = resolve_target(args.output_dir, bundle=args.bundle)
        build(target.bundle, target.output_dir, iscc, unpacker, args.lifecycle_report)
        return 0
    except (RuntimeError, ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f"安装器构建失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
