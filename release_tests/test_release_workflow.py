"""Release boundaries: artifact integrity, provenance and draft-only mutations."""
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from scripts import release_portable as release
from bagu_trainer import secrets


SHA = "a" * 40


def artifact(tmp_path, *, dirty=False, source=SHA):
    archive = tmp_path / f"BaguTrainer-{release.__version__}-windows-x64.zip"
    binary = b"MZ mock binary"
    manifest = {"version": release.__version__, "architecture": "windows-x64", "source_commit": source,
                "source_dirty": dirty, "files": [{"path": "BaguTrainer.exe", "bytes": len(binary),
                    "sha256": hashlib.sha256(binary).hexdigest()}]}
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("BaguTrainer/BaguTrainer.exe", binary)
        bundle.writestr("BaguTrainer/BUILD_MANIFEST.json", json.dumps(manifest))
    checksum = Path(str(archive) + ".sha256")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(digest + "  " + archive.name + "\n", encoding="ascii")
    return archive, checksum, digest


def test_notes_have_direct_assets_and_chinese_first_use(tmp_path):
    archive, checksum, digest = artifact(tmp_path)
    assert release.validate_release(tmp_path, "v" + release.__version__, SHA) == (archive, checksum, digest)
    notes = release.render_notes("owner/repo", "v" + release.__version__, SHA, archive, digest,
                                 tmp_path / f"BaguTrainer-{release.__version__}-windows-x64-Setup.exe", "b" * 64,
                                 tests={"passed": 100, "skipped": 0})
    assert f"/releases/download/v{release.__version__}/{archive.name}" in notes
    assert archive.name + ".sha256" in notes
    for text in ("草稿", "双击", "Python", "Git", "MCP", "题库", "模型", "项目", "15 秒", "凭据", "LOCALAPPDATA", digest):
        assert text in notes


@pytest.mark.parametrize("dirty,source", [(True, SHA), (False, "b" * 40)])
def test_dirty_or_wrong_commit_is_rejected(tmp_path, dirty, source):
    artifact(tmp_path, dirty=dirty, source=source)
    with pytest.raises(ValueError, match="provenance"):
        release.validate_release(tmp_path, "v" + release.__version__, SHA)


def test_mismatched_checksum_and_version_fail_before_upload(tmp_path):
    _, checksum, _ = artifact(tmp_path)
    with pytest.raises(ValueError, match="Tag"):
        release.validate_release(tmp_path, "unrelated", SHA)
    checksum.write_text("0" * 64, encoding="ascii")
    with pytest.raises(ValueError, match="SHA-256"):
        release.validate_release(tmp_path, "v" + release.__version__, SHA)


@pytest.mark.parametrize("child", ["<failure/>", "<error/>", "<skipped/>"])
def test_failed_or_skipped_tests_block_release(tmp_path, child):
    report = tmp_path / "tests.xml"
    report.write_text(f"<testsuites><testsuite><testcase>{child}</testcase></testsuite></testsuites>")
    with pytest.raises(ValueError, match="without skips"):
        release.test_summary(report)


@pytest.mark.parametrize("draft,target,ref", [(False, SHA, None), (True, "b" * 40, None),
                                             (True, SHA, "b" * 40)])
def test_existing_published_release_or_other_commit_is_untouched(monkeypatch, draft, target, ref):
    def read(*args):
        if "matching-refs" in args[1]:
            return [{"ref": "refs/tags/v0.1.0", "object": {"type": "commit", "sha": ref}}] if ref else []
        return [[{"tag_name": "v0.1.0", "draft": draft, "target_commitish": target}]]
    monkeypatch.setattr(release, "gh_json", read)
    monkeypatch.setattr(release.subprocess, "run", lambda *_a, **_k: pytest.fail("must not mutate GitHub"))
    with pytest.raises(ValueError):
        release.publish_draft("owner/repo", "v0.1.0", SHA, Path("notes.md"), [])


def test_new_release_is_always_draft_and_rerun_updates_draft_only(tmp_path, monkeypatch):
    calls, existing = [], []
    def read(*args):
        if args[:2] == ("release", "view"):
            return {"isDraft": True, "url": "https://github.com/owner/repo/releases/tag/v0.1.0"}
        return [] if "matching-refs" in args[1] else [existing]
    monkeypatch.setattr(release, "gh_json", read)
    monkeypatch.setattr(release.subprocess, "run", lambda command, **_: calls.append(command))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    release.publish_draft("owner/repo", "v0.1.0", SHA, tmp_path / "notes.md", [tmp_path / "app.zip"])
    assert len(calls) == 1 and calls[0][1:3] == ["release", "create"] and "--draft" in calls[0]
    assert "--target" in calls[0] and SHA in calls[0]
    existing.append({"tag_name": "v0.1.0", "draft": True, "target_commitish": SHA})
    calls.clear()
    release.publish_draft("owner/repo", "v0.1.0", SHA, tmp_path / "notes.md", [tmp_path / "app.zip"])
    assert calls[0][2] == "upload" and "--clobber" in calls[0]
    assert calls[1][2] == "edit" and "--draft=true" in calls[1]


def test_explicit_profile_isolates_credentials_and_is_stable(tmp_path, monkeypatch):
    monkeypatch.delenv("BAGU_USER_DATA_DIR", raising=False)
    assert secrets.credential_service() == "bagu-trainer"
    monkeypatch.setenv("BAGU_USER_DATA_DIR", str(tmp_path / "测试 甲"))
    first = secrets.credential_service()
    assert first.startswith("bagu-trainer.profile.")
    assert secrets.credential_service() == first
    monkeypatch.setenv("BAGU_USER_DATA_DIR", str(tmp_path / "测试 乙"))
    assert secrets.credential_service() != first


@pytest.mark.parametrize("change", [{"frozen": False}, {"source_dirty": True}, {"production_dirty": True},
                                    {"production_commit": "b" * 40}, {"production_exe": {}}])
def test_incomplete_or_stale_frozen_acceptance_blocks_upload(tmp_path, change):
    report = {"passed": True, "frozen": True, "source_commit": SHA, "source_dirty": False,
              "production_commit": SHA, "production_dirty": False,
              "first": {"passed": True}, "restart": {"passed": True},
              "production_exe": {"first_start": "running", "upgraded_start": "running", "history_retained": True}}
    report.update(change)
    path = tmp_path / "ACCEPTANCE.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        release.acceptance_summary(path, SHA)
