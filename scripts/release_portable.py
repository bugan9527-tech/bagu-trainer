"""Validate the shipped ZIP and create an explicitly draft-only GitHub Release."""
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
        names = [name for name in bundle.namelist() if not name.endswith("/")]
        if len(names) != len(set(names)) or set(names) != set(expected) | {"BaguTrainer/BUILD_MANIFEST.json"}:
            raise ValueError("ZIP inventory differs from BUILD_MANIFEST")
        for name, item in expected.items():
            value = bundle.read(name)
            if len(value) != item["bytes"] or hashlib.sha256(value).hexdigest() != item["sha256"]:
                raise ValueError("ZIP file checksum mismatch: " + name)
    return archive, checksum, digest


def render_notes(repository, tag, target, archive, digest, *, tests=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    base = f"https://github.com/{repository}"
    download = base + "/releases/download/" + quote(tag, safe="") + "/" + archive.name
    validation = ("本次自动测试报告随附件提供；仅代表 Windows runner 上的隔离验证。" if tests is None else
                  f"本次自动测试：{tests['passed']} 项通过，{tests['skipped']} 项跳过。报告随附件提供。")
    return f"""# AI 八股训练器 {tag} · Windows x64 便携版

**[直接下载 Windows x64 ZIP]({download})** · **[下载 SHA-256 校验文件]({download}.sha256)**

这是草稿 Release，仅仓库有权限的用户可见。正式发布后，上面的固定下载链接才对公众生效；
审阅草稿时请直接使用下方 Assets 附件下载。不要下载 GitHub 自动附带的 Source code ZIP 来代替应用。

## 首次使用

1. 将整个 ZIP 解压到本地目录，双击 `BaguTrainer.exe`；保留 EXE 旁的 `_internal`。
   不需要安装 Python、Git 或配置 MCP。
2. 在中文向导中选择题库来源，下载指定版本并等待构建成功；完整第三方题库不随程序分发。
   下载失败时检查网络或代理后点击重试，取消或失败不会覆盖旧题库。
3. 填写模型基础地址、模型名和 API Key，主动测试连接，成功后保存。
   Key 存入 Windows 凭据管理器；模型接口和首次下载需要网络。
4. 选择监听项目，进入主窗口。建立监听基线后修改代码，稳定约 15 秒且匹配知识点时会弹题；
   作答后显示判分和解析，学习记录自动保存。未匹配或模型不可用时允许没有题目。

配置、题库、日志和学习库都在 `%LOCALAPPDATA%\\BaguTrainer\\`。退出后用新版本的完整程序目录
替换旧程序，保留用户数据即可恢复学习记录；移动程序后重新启用登录自启动。
旧源码旁的数据使用向导“迁移旧数据…”显式复制，旧数据保留，目标已有数据时拒绝覆盖。

## 校验与验证范围

ZIP SHA-256：`{digest}`

构建提交：[{target}]({base}/commit/{target})。{validation}
模型链路自动化使用本地模拟 HTTP 接口，不调用真实模型或真实 Key。
GitHub Windows runner 自带开发工具，禁用 PATH 的测试不等同于全新系统上卸载 Python/Git。
干净 Windows 实机验收、真实来源网络可用性、系统重启登录和未覆盖项以
[验证记录]({base}/blob/{target}/VALIDATION.md) 为准；此包尚未代码签名。
"""


def test_summary(path):
    root = ET.parse(path).getroot()
    cases = root.findall(".//testcase")
    failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    if not cases or failed or skipped:
        raise ValueError("Release tests must all pass without skips")
    return {"passed": len(cases) - skipped, "skipped": skipped}


def acceptance_summary(path, target):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    if (report.get("passed") is not True or report.get("frozen") is not True
            or report.get("source_commit") != target or report.get("source_dirty") is not False
            or report.get("production_commit") != target or report.get("production_dirty") is not False
            or report.get("production_exe") != {"first_start": "running", "upgraded_start": "running", "history_retained": True}
            or not report.get("first", {}).get("passed") or not report.get("restart", {}).get("passed")):
        raise ValueError("Frozen acceptance/provenance/upgrade verification is incomplete")
    return report


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
    parser.add_argument("--output", type=Path, default=ROOT / "validation" / "RELEASE_NOTES.md")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "bugan9527-tech/bagu-trainer"))
    parser.add_argument("--target", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--publish-draft", action="store_true")
    options = parser.parse_args()
    target = options.target or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    archive, checksum, digest = validate_release(options.dist, options.tag, target)
    junit = options.output.parent / "release-tests.xml"
    tests = test_summary(junit)
    acceptance = options.output.parent / "ACCEPTANCE.json"
    acceptance_summary(acceptance, target)
    notes = render_notes(options.repository, options.tag, target, archive, digest, tests=tests)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(notes, encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"tag={options.tag}\n")
    if options.publish_draft:
        publish_draft(options.repository, options.tag, target, options.output, [archive, checksum, junit, acceptance])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
