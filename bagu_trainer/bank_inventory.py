"""Read-only disk inventory. Run scanning and ZIP verification off the Tk thread."""
import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from .bank_build.control import BuildCancelled, BuildControl, BuildError, BuildTimeout
from .bank_build.downloads import ArchiveLimits, safe_extract
from .bank_build.source_cache import cache_directory, load_archive
from .bank_build.service import _build_lock
from .bank_build.source_snapshot import SnapshotVerifier
from .bank_storage import active_bank_dir


CATEGORIES = ("cache", "current", "history", "temporary")
LOCAL_LABELS = {"verified": "完整缓存·可复用", "missing": "未下载", "legacy": "仅版本快照·未校验",
                "incomplete": "缓存不完整", "invalid": "缓存损坏", "unknown": "无法校验"}


def format_bytes(value):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024


def _json(path):
    if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("元信息不是可读取的普通小文件")
    return json.loads(path.read_text(encoding="utf-8"))


def _component(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", value) and not value.endswith(".")


@dataclass
class SourceInventory:
    repository: str
    commit: str
    local_state: str = "missing"
    used: str = "未使用"
    bytes_by_category: dict = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))
    reason: str = ""
    remote_bytes: int | None = None  # A local archive is not a remote size estimate.
    downloaded: bool = False

    @property
    def local_bytes(self):
        return sum(self.bytes_by_category.values())


@dataclass
class DiskInventory:
    sources: dict
    totals: dict = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))
    unassigned: int = 0
    duplicate_bytes: int = 0
    warnings: list = field(default_factory=list)
    current_directory: str = ""


class BankInventory:
    def __init__(self, project_root, *, limits=None):
        self.root = Path(project_root).resolve()
        self.limits = limits or ArchiveLimits()
        self._verified = {}
        self._snapshots = SnapshotVerifier()

    def _verify(self, source, directory, control):
        if directory.parent.is_symlink() or (directory.parent.exists() and getattr(directory.parent.lstat(), "st_file_attributes", 0) & 0x400):
            return "unknown", "缓存根目录为链接，不作为完整缓存使用。"
        if not directory.exists():
            return "missing", ""
        if directory.is_symlink():
            return "unknown", "缓存目录为链接，不作为完整缓存使用。"
        if not (directory / "complete.json").exists():
            return "incomplete", "缺少完成记录；目录存在不代表下载完成。"
        try:
            record = _json(directory / "complete.json")
            digest = record["source"]["archive_sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("归档摘要无效")
            archive = directory / (digest[:32] + ".zip")
            if archive.is_symlink():
                raise ValueError("归档不能是链接")
            fingerprint = tuple((p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ino)
                                for p in (directory / "complete.json", archive))
            key = (source.repository, source.commit, fingerprint)
            if self._verified.get(directory) == key:
                return "verified", "已校验完整 commit、大小、SHA256、ZIP 路径和 CRC。"
            cached = load_archive(directory.parent, source, control, self.limits)
            if not cached:
                raise ValueError("完成记录、仓库/commit、文件大小或 SHA256 不匹配")
            safe_extract(archive, None, control, limits=self.limits, verify_only=True)
            after = tuple((p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ino)
                          for p in (directory / "complete.json", archive))
            if after != fingerprint:
                return "unknown", "校验期间来源有变化，正在等待重新扫描。"
            self._verified[directory] = key
            return "verified", "已校验完整 commit、大小、SHA256、ZIP 路径和 CRC。"
        except BuildCancelled:
            raise
        except BuildTimeout:
            return "unknown", "后台校验达到时限；可刷新重试。"
        except PermissionError:
            return "unknown", "无法读取缓存进行完整性校验；请检查目录权限和文件占用。"
        except (OSError, ValueError, TypeError, KeyError, BuildError) as error:
            return "invalid", f"缓存无法复用：{error}。可重新构建修复。"

    def scan(self, sources, *, generation="", cancel=None):
        control = BuildControl(cancel=cancel, timeout=3600)
        result = DiskInventory({repo: SourceInventory(repo, s.commit) for repo, s in sources.items()})
        data = self.root / "data"
        try:
            data.resolve().relative_to(self.root)
        except ValueError:
            result.warnings.append("数据目录指向管理目录之外，已跳过统计。")
            for row in result.sources.values():
                row.local_state, row.used = "unknown", "未知·数据目录为外部链接"
            return result
        known = {repo.casefold(): repo for repo in sources}
        try:
            current = active_bank_dir(self.root)
            current.resolve().relative_to(self.root)
        except (OSError, ValueError):
            current = None
            result.warnings.append("当前版本指针损坏或缺失对应目录；无法确定启用来源。")
            for row in result.sources.values():
                row.used = "未知·当前版本指针无效"
        result.current_directory = str(current or "")
        active_sources = None
        if current is not None:
            try:
                active_sources = _json(current / "sources.json")["sources"]
                if not isinstance(active_sources, list):
                    raise ValueError("来源列表无效")
            except (OSError, ValueError, KeyError, TypeError):
                if (current / "questions.json").exists():
                    for row in result.sources.values():
                        row.used = "未知·旧版本缺少来源记录"
        if active_sources is not None:
            for entry in active_sources:
                if not isinstance(entry, dict):
                    continue
                repo = known.get(str(entry.get("repository", "")).casefold())
                if repo:
                    commit = entry.get("commit")
                    result.sources[repo].used = ("当前版本使用" if commit == sources[repo].commit else
                        "当前使用其他 commit" if commit else "使用中·commit 未记录")

        # Global identity ownership: cache > active/live build > history > temp.
        # Per-source numbers are a breakdown, never added again to the totals.
        groups = {category: [] for category in CATEGORIES}
        cache_root = data / "source-cache"
        cache_owners = {cache_directory(cache_root, source).name: repo for repo, source in sources.items()}
        legacy_seen = set()

        def children(path):
            try:
                if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                    result.warnings.append(f"跳过链接目录：{path.name}")
                    return []
                return sorted(path.iterdir(), key=lambda item: item.name.casefold())
            except FileNotFoundError:
                return []
            except OSError:
                result.warnings.append(f"无法扫描 {path.name}；统计不完整。")
                return []

        def owner_map(directory):
            mapping = {s.directory.casefold(): repo for repo, s in sources.items()}
            if directory.is_symlink() or (directory.exists() and getattr(directory.lstat(), "st_file_attributes", 0) & 0x400):
                return mapping
            try:
                for entry in _json(directory / "sources.json")["sources"]:
                    repo = known.get(str(entry.get("repository", "")).casefold())
                    component = entry.get("directory") or (sources[repo].directory if repo else None)
                    if repo and _component(component):
                        mapping[component.casefold()] = repo
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            return mapping

        def walk(path, category, owner=None, mapping=None, snapshot=False):
            control.check()
            try:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    result.warnings.append(f"跳过链接：{path.name}")
                    return
                if stat.S_ISDIR(info.st_mode):
                    for child in children(path):
                        child_owner = owner
                        child_mapping = mapping
                        if mapping and owner is None and path.name == "repos":
                            child_owner = mapping.get(child.name.casefold())
                            child_mapping = None
                            if child_owner and snapshot:
                                legacy_seen.add(child_owner)
                        elif mapping and owner is None and child.suffix == ".zip":
                            child_owner = mapping.get(child.stem.casefold())
                        walk(child, category, child_owner, child_mapping, snapshot)
                elif stat.S_ISREG(info.st_mode):
                    kind = "temporary" if path.name.endswith(".tmp") else category
                    identity = (info.st_dev, info.st_ino) if info.st_ino else str(path.resolve()).casefold()
                    groups[kind].append((identity, info.st_size, owner))
            except FileNotFoundError:
                result.warnings.append("扫描期间有文件移除；空间为动态快照。")
            except OSError:
                result.warnings.append(f"无法读取 {path.name}；统计不完整。")

        for directory in children(cache_root):
            owner = cache_owners.get(directory.name)
            if owner is None and directory.is_dir() and not directory.is_symlink():
                try:
                    entry = _json(directory / "complete.json")["source"]
                    repo = known.get(entry["repository"].casefold())
                    commit = entry["commit"]
                    if repo and isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
                        identity_source = replace(sources[repo], commit=commit)
                        if cache_directory(cache_root, identity_source) == directory:
                            owner = repo
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    pass
            walk(directory, "cache", owner)
        for directory in children(data / "banks"):
            walk(directory, "current" if directory == current else "history", mapping=owner_map(directory), snapshot=True)
            # Only published UUID generations count, never .bank-builds staging.
            if not re.fullmatch(r"[0-9a-f]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", directory.name):
                continue
            try:
                entries = _json(directory / "sources.json")["sources"]
            except (OSError, ValueError, TypeError, KeyError):
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                try:
                    if not isinstance(entry, dict):
                        continue
                    repo = known.get(str(entry.get("repository", "")).casefold())
                    commit = entry.get("commit")
                    if (repo and not result.sources[repo].downloaded
                            and isinstance(commit, str) and commit.lower() == sources[repo].commit.lower()
                            and self._snapshots.verify(directory, entry, control)):
                        result.sources[repo].downloaded = True
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    continue
        live = generation if re.fullmatch(r"[0-9a-f]{32}", generation) else None
        for directory in children(data / ".bank-builds"):
            walk(directory, "current" if directory.name == live else "temporary", mapping=owner_map(directory))
        # Legacy top-level outputs: whitelist bank artifacts, never study DB/config.
        legacy_mapping = owner_map(data)
        for name in ("questions.json", "question_concepts.json", "sources.json", "repos", "extracted",
                     "knowledge_tag_report.json", "concept_profile_report.json", "title_cleanup_report.json"):
            path = data / name
            if path.exists():
                walk(path, "current" if current == data else "history", mapping=legacy_mapping, snapshot=True)
        for path in data.glob("*.tmp"):
            walk(path, "temporary")

        seen = set()
        for category in CATEGORIES:
            for identity, size, owner in groups[category]:
                if identity in seen:
                    result.duplicate_bytes += size
                    continue
                seen.add(identity)
                result.totals[category] += size
                if owner in result.sources:
                    result.sources[owner].bytes_by_category[category] += size
                else:
                    result.unassigned += size
        for repo, source in sources.items():
            control.check()
            row = result.sources[repo]
            row.local_state, row.reason = self._verify(source, cache_directory(cache_root, source), control)
            row.downloaded = row.downloaded or row.local_state == "verified"
            if row.local_state == "missing" and repo in legacy_seen:
                row.local_state = "legacy"
                row.reason = "版本来源目录无可核对的 ZIP/完成记录，不能直接作为可复用缓存。"
            elif row.local_state == "missing" and row.local_bytes:
                row.local_state = "incomplete"
                row.reason = "仅有临时来源文件，不能作为已完成缓存复用。"
        result.warnings = list(dict.fromkeys(result.warnings))
        return result


def delete_source_cache(project_root, sources):
    """Explicit user action: remove cache only, under the same OS build lock."""
    root = Path(project_root).resolve()
    data = root / "data"
    data.resolve().relative_to(root)
    builds = data / ".bank-builds"
    builds.resolve().relative_to(data.resolve())
    if builds.is_symlink() or (builds.exists() and getattr(builds.lstat(), "st_file_attributes", 0) & 0x400):
        raise ValueError("构建目录为链接，拒绝删除。")
    with _build_lock(builds):
        selected = {source.repository.casefold() for source in sources}
        current = active_bank_dir(root)
        try:
            entries = _json(current / "sources.json")["sources"]
            if any(str(entry.get("repository", "")).casefold() in selected for entry in entries):
                raise BuildError("cleanup", "来源用于当前题库；请使用带确认和安全切换的题库清理入口。")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            if (current / "questions.json").exists():
                raise BuildError("cleanup", "当前题库来源未知，拒绝直接删除缓存；请先重建。")
        cache_root = data / "source-cache"
        cache_root.resolve().relative_to(data.resolve())
        if cache_root.is_symlink() or (cache_root.exists() and getattr(cache_root.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("缓存根目录为链接，拒绝删除。")
        for source in sources:
            path = cache_directory(data / "source-cache", source)
            if not path.exists():
                continue
            if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("缓存目录为链接，拒绝删除。")
            path.resolve().relative_to((data / "source-cache").resolve())
            retired = data / ".bank-builds" / uuid.uuid4().hex
            retired.resolve().relative_to(builds.resolve())
            os.replace(path, retired)
            shutil.rmtree(retired)
