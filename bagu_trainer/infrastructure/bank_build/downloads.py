"""Bounded HTTPS ZIP downloads and portable, fail-closed archive extraction."""
import hashlib
import http.client
import re
import stat
import urllib.error
import urllib.request
import zipfile
from contextlib import nullcontext
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from bagu_trainer.infrastructure.bank_build.control import BuildError


class ArchiveLimitError(BuildError):
    """A local resource policy failure does not mean an archive is corrupt."""


@dataclass(frozen=True)
class ArchiveLimits:
    download_bytes: int = 512 * 1024 * 1024
    unpack_bytes: int = 2 * 1024 * 1024 * 1024
    file_bytes: int = 64 * 1024 * 1024
    members: int = 100000
    compression_ratio: int = 1000


class GithubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        address = urlsplit(newurl)
        if (address.scheme != "https" or address.hostname not in
                {"api.github.com", "github.com", "codeload.github.com"}
                or address.username or address.password):
            raise BuildError("download", "下载重定向不在允许的 GitHub HTTPS 地址内。")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download_archive(source, destination, control, *, opener=None, retries=2,
                     request_timeout=5, limits=None):
    limits = limits or ArchiveLimits()
    opener = opener or urllib.request.build_opener(GithubRedirects()).open
    url = f"https://api.github.com/repos/{source.repository}/zipball/{source.commit}"
    request = urllib.request.Request(url, headers={"User-Agent": "bagu-trainer",
                                                   "Accept": "application/vnd.github+json"})
    destination = Path(destination)
    for attempt in range(1, retries + 2):
        control.emit("download", f"准备请求 {source.repository}", attempt=attempt)
        control.phase("download", f"下载 {source.repository} @ {source.commit[:12]}")
        control.emit("download", f"下载 {source.repository}", attempt=attempt)
        try:
            with opener(request, timeout=min(request_timeout, control.remaining())) as response:
                length = response.headers.get("Content-Length")
                total = int(length) if length else 0
                if total < 0 or total > limits.download_bytes:
                    raise ArchiveLimitError("download", "ZIP 下载大小超出限制。")
                downloaded = 0
                digest = hashlib.sha256()
                read = getattr(response, "read1", response.read)
                with destination.open("wb") as output:
                    while True:
                        control.check()
                        chunk = read(64 * 1024)
                        control.check()
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > limits.download_bytes:
                            raise ArchiveLimitError("download", "ZIP 下载大小超出限制。")
                        output.write(chunk)
                        digest.update(chunk)
                        control.emit("download", source.repository, downloaded, total, attempt)
                if total and downloaded != total:
                    raise http.client.IncompleteRead(b"", total - downloaded)
                return {"repository": source.repository, "commit": source.commit,
                        "archive_url": url, "archive_sha256": digest.hexdigest(),
                        "download_bytes": downloaded, "observed_license": source.license,
                        "evidence_url": source.evidence_url}
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, ConnectionError) as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            destination.unlink(missing_ok=True)
            control.check()
            retryable = not isinstance(error, urllib.error.HTTPError) or error.code in {408, 429} or error.code >= 500
            if not retryable or attempt > retries:
                status = f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__
                if isinstance(error, urllib.error.HTTPError):
                    reason = ("仓库或 commit 不可访问，请核对固定版本和仓库访问权限" if error.code == 404
                              else "请求被限流，请稍后重试" if error.code == 429
                              else "远端服务拒绝请求或暂时不可用，请检查网络与仓库访问权限后重试")
                elif isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
                    reason = f"单次网络请求等待超时（上限 {request_timeout:g} 秒），请检查网络或增大单次请求超时后重试"
                elif isinstance(error, http.client.IncompleteRead):
                    reason = "下载响应不完整，请检查网络连接后重试，残缺内容不会作为缓存使用"
                else:
                    reason = f"网络连接失败：{getattr(error, 'reason', error)}，请检查网络、代理或证书配置后重试"
                raise BuildError("download", f"下载 {source.repository} 失败（{status}）：{reason}。") from error
            control.emit("retry", f"下载失败，准备第 {attempt + 1} 次尝试", attempt=attempt + 1)
            control.wait(min(2 ** (attempt - 1), 4))
    raise AssertionError("unreachable")


_DEVICE = re.compile(r"^(?:con|prn|aux|nul|conin\$|conout\$|clock\$|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|$)", re.I)


def _portable_paths(validated, control):
    """Give case-distinct Git paths distinct disk names, independent of ZIP order."""
    nodes = {(): True}
    for member, relative in validated:
        parts = relative.parts if relative != Path() else ()
        for depth in range(1, len(parts) + 1):
            control.check()
            logical = parts[:depth]
            is_dir = depth < len(parts) or member.is_dir()
            if logical in nodes and nodes[logical] != is_dir:
                raise BuildError("unpack", "ZIP 同一路径同时声明为文件和目录。")
            nodes[logical] = is_dir
    children = defaultdict(list)
    for logical in nodes:
        if logical:
            children[logical[:-1]].append(logical)
    physical = {(): Path()}
    for parent in sorted(children, key=lambda parts: (len(parts), parts)):
        siblings = children[parent]
        groups = defaultdict(list)
        for logical in siblings:
            groups[logical[-1].casefold()].append(logical)
        reserved = set(groups)
        for logical in sorted(siblings):
            control.check()
            name = logical[-1]
            if len(groups[name.casefold()]) > 1:
                suffix = Path(name).suffix
                stem = name[:-len(suffix)] if suffix else name
                digest = hashlib.sha256("/".join(logical).encode("utf-8")).hexdigest()[:12]
                attempt = 0
                while True:
                    counter = f"-{attempt}" if attempt else ""
                    candidate = f"{stem}.__bagu_case_{digest}{counter}{suffix}"
                    if candidate.casefold() not in reserved:
                        break
                    attempt += 1
                name = candidate
                reserved.add(name.casefold())
            physical[logical] = physical[parent] / name
    return physical


def safe_extract(archive, destination, control, *, limits=None, verify_only=False):
    """Extract safely; return original -> stored aliases for case-distinct files."""
    limits = limits or ArchiveLimits()
    destination = Path(destination) if not verify_only else None
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > limits.members:
                raise ArchiveLimitError("unpack", "ZIP 条目数量超出限制。")
            if not members:
                raise BuildError("unpack", "ZIP 条目数量不合法。")
            seen, root, expanded = set(), None, 0
            validated = []
            for member in members:
                control.check()
                raw = member.orig_filename
                parts = PurePosixPath(raw).parts
                # Windows treats backslashes, drive letters, ADS, device names and
                # trailing dots/spaces specially; reject them on every platform.
                if (not parts or raw.startswith("/") or "\\" in raw or "\x00" in raw
                        or any(p in {".", ".."} or ":" in p or p.endswith((".", " "))
                               or _DEVICE.match(p) or any(ord(c) < 32 or c in '<>"|?*' for c in p) for p in parts)):
                    raise BuildError("unpack", "ZIP 含不安全路径。")
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or member.flag_bits & 1:
                    raise BuildError("unpack", "ZIP 含链接、特殊文件或加密条目。")
                if root is None:
                    root = parts[0]
                if parts[0] != root or (len(parts) == 1 and not member.is_dir()):
                    raise BuildError("unpack", "ZIP 必须包含单一来源根目录。")
                relative = Path(*parts[1:])
                key = relative.as_posix()
                if key in seen:
                    raise BuildError("unpack", "ZIP 包含重复路径。")
                seen.add(key)
                expanded += member.file_size
                if (member.file_size > limits.file_bytes or expanded > limits.unpack_bytes
                        or member.file_size > max(1, member.compress_size) * limits.compression_ratio):
                    raise ArchiveLimitError("unpack", "ZIP 解压大小或压缩比超出限制。")
                validated.append((member, relative))
            physical = _portable_paths(validated, control)
            aliases = {}
            for index, (member, relative) in enumerate(validated, 1):
                control.check()
                stored = physical[relative.parts if relative != Path() else ()]
                target = destination / stored if destination is not None else None
                if target is not None:
                    target.resolve().relative_to(destination.resolve())
                if member.is_dir():
                    if target is not None:
                        target.mkdir(parents=True, exist_ok=True)
                else:
                    if target is not None:
                        target.parent.mkdir(parents=True, exist_ok=True)
                    count = 0
                    with bundle.open(member) as incoming, (target.open("xb") if target is not None else nullcontext()) as output:
                        while True:
                            control.check()
                            block = incoming.read(64 * 1024)
                            if not block:
                                break
                            count += len(block)
                            if count > member.file_size or count > limits.file_bytes:
                                raise BuildError("unpack", "ZIP 实际解压大小超出声明。")
                            if output is not None:
                                output.write(block)
                    if count != member.file_size:
                        raise BuildError("unpack", "ZIP 内容不完整。")
                    if stored != relative:
                        aliases[relative.as_posix()] = stored.as_posix()
                control.emit("unpack", "正在安全解压来源文件", index, len(validated))
            return aliases
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
        raise BuildError("unpack", "ZIP 已损坏或使用不支持的压缩方式。") from error
