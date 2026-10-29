"""File evidence for saved source snapshots, independent of the active pointer."""
import hashlib
import re
import stat
from pathlib import PurePosixPath


def _regular(path, directory=False):
    info = path.lstat()
    if (getattr(info, "st_file_attributes", 0) & 0x400 or
            not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError("来源文件不是普通文件/目录")
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino


def _digest(path, control):
    digest = hashlib.sha256()
    with path.open("rb") as incoming:
        while True:
            control.check()
            block = incoming.read(64 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def source_files(directory, control):
    """Record all extracted files before the complete generation is published."""
    files = []

    def walk(parent):
        _regular(parent, directory=True)
        for path in sorted(parent.iterdir()):
            control.check()
            if path.is_dir():
                walk(path)
            else:
                before = _regular(path)
                digest = _digest(path, control)
                if _regular(path) != before:
                    raise ValueError("来源文件在校验时变化")
                files.append({"path": path.relative_to(directory).as_posix(),
                              "size": before[0], "sha256": digest})
    walk(directory)
    return files


class SnapshotVerifier:
    def __init__(self):
        self._verified = {}

    def verify(self, version, entry, control):
        """Fail closed for old snapshots without a complete file inventory."""
        directory = entry.get("directory")
        if (not isinstance(directory, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", directory)
                or directory.endswith(".")):
            return False
        records = entry.get("source_files")
        if not isinstance(records, list) or not records:
            return False
        _regular(version, directory=True)
        _regular(version / "repos", directory=True)
        root = version / "repos" / directory
        _regular(root, directory=True)
        seen = set()
        for record in records:
            control.check()
            name, size, digest = record["path"], record["size"], record["sha256"]
            if not isinstance(name, str):
                return False
            parts = PurePosixPath(name).parts
            if (not parts or PurePosixPath(name).is_absolute() or any(p in {".", ".."} or ":" in p or "\\" in p for p in parts)
                    or name in seen or type(size) is not int or size < 0
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                return False
            seen.add(name)
            parent = root
            for part in parts[:-1]:
                parent /= part
                _regular(parent, directory=True)
            path = root.joinpath(*parts)
            before = _regular(path)
            if before[0] != size:
                return False
            key = (digest, before)
            if self._verified.get(path) != key:
                if _digest(path, control) != digest or _regular(path) != before:
                    return False
                self._verified[path] = key
        return True


def download_label(source, downloaded, task=None, busy=False):
    """Task preparation/cache hits are not actual network downloads."""
    matches = (task is not None and task.repository.casefold() == source.repository.casefold()
               and task.commit.lower() == source.commit.lower())
    if busy and matches:
        if task.state == "downloading" and task.stage in {"download", "retry"}:
            return "正在下载"
        if task.state == "completed":
            return "已下载"
    return "已下载" if downloaded else "未下载"
