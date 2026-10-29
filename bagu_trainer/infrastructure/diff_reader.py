# -*- coding: utf-8 -*-
"""文件变动模块：git diff 优先；无 git 时用 mtime+size 快照轮询记录变更。
返回统一结构 {"source": "git"|"watch", "files": [str], "diff_text": str}
diff_text 会被截断，仅作为出题上下文片段。

隐私约束：
  - 只收录扩展名白名单内的源码/文档类文件（DIFF_EXT_WHITELIST）
  - 敏感文件名黑名单（config.json/.env/*.key/私钥/凭据类）经
    bagu_trainer.infrastructure.secrets.is_sensitive_path 统一判定，任何模式下一律排除，
    且截取片段前二次校验（防御性，杜绝黑名单文件内容进入上下文）
"""
import re
import os
import subprocess
import threading
from pathlib import Path

from bagu_trainer.infrastructure.secrets import is_sensitive_path

DIFF_LIMIT = 4000   # diff 上下文截断长度
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
SKIP_PREFIX = (".",)  # 隐藏文件
MAX_FILE_BYTES = 512 * 1024

# 扩展名白名单：仅这些文件类型的内容可进入出题上下文
DIFF_EXT_WHITELIST = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
    ".java", ".go", ".rs", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".swift", ".kt", ".kts", ".scala", ".sql", ".sh", ".bash", ".ps1",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".json",
    ".md", ".markdown", ".txt", ".html", ".htm", ".css", ".scss", ".less",
    ".xml", ".gradle",
}


def _file_allowed(rel) -> bool:
    """文件是否允许进入上下文：扩展名白名单通过且非敏感文件名"""
    if is_sensitive_path(rel):
        return False
    return Path(str(rel)).suffix.lower() in DIFF_EXT_WHITELIST


class DiffReader:
    def __init__(self, project_root: str):
        self.root = Path(project_root)
        self._snapshot = None  # watch 模式快照: {relpath: (mtime_ns, size)}
        self._lock = threading.Lock()  # 监控线程与工具调用并发访问同一实例

    def _run_git(self, args: list) -> subprocess.CompletedProcess:
        """git 子进程必须切断 stdin 继承并限制超时：
        Windows 下 git(MSYS) 继承父进程的 stdin 管道句柄时可能永不退出，
        导致 subprocess.run 死等（实测卡在 is_git_repo）。
        pythonw 无控制台时还须禁止子进程创建控制台，避免每次轮询闪黑窗。
        """
        return subprocess.run(
            # Git emits UTF-8 paths and source text regardless of Windows' ANSI
            # code page. A decoding error in subprocess' reader thread leaves
            # stdout as None and breaks every subsequent monitoring poll.
            ["git", "-c", "core.quotepath=false"] + args, cwd=str(self.root),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def is_git_repo(self) -> bool:
        try:
            r = self._run_git(["-C", str(self.root), "rev-parse", "--is-inside-work-tree"])
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    @staticmethod
    def _filter_git_diff(stdout: str) -> dict:
        """按 diff --git 文件块过滤：黑名单/白名单文件的内容整体剔除"""
        files, kept = set(), []
        for chunk in re.split(r"(?=^diff --git )", stdout, flags=re.M):
            m = re.search(r"^diff --git a/(.+?) b/(.+?)$", chunk, flags=re.M)
            if not m:
                continue
            b_path = m.group(2).strip().strip('"')
            if not _file_allowed(b_path):
                continue
            files.add(b_path)
            kept.append(chunk)
        return {"files": sorted(files), "diff_text": "\n".join(kept)}

    def git_changes(self) -> dict:
        """未提交改动 = 工作区 diff + 暂存区 diff（AI 实际改了什么），
        仅保留白名单内且非敏感文件的内容"""
        files, parts = set(), []
        for args in (["diff"], ["diff", "--cached"]):
            try:
                r = self._run_git(args + ["--"])
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            if not r.stdout.strip():
                continue
            filtered = self._filter_git_diff(r.stdout)
            files.update(filtered["files"])
            parts.append(filtered["diff_text"])
        return {
            "source": "git",
            "files": sorted(files),
            "diff_text": "\n".join(p for p in parts if p)[-DIFF_LIMIT:],
        }

    def watch_changes(self, recent_minutes: int = 30) -> dict:
        """无 git 兜底：对项目树做快照，与上一次快照比对得到变更文件列表。
        首次建立基线时，把最近 recent_minutes 内改动过的文件也视为变更，
        保证服务器刚启动后的第一次报告就能抓到近期新增/修改的文件。
        敏感文件/非白名单文件不进快照，也进不了片段。
        """
        now = self._snapshot_files()
        with self._lock:
            changed = []
            if self._snapshot is not None:
                for rel, meta in now.items():
                    if self._snapshot.get(rel) != meta:
                        changed.append(rel)
                changed += [rel for rel in self._snapshot if rel not in now]
            else:
                import time
                cutoff_ns = (time.time() - recent_minutes * 60) * 1e9
                changed = [rel for rel, (mt, _sz) in now.items() if mt >= cutoff_ns]
            self._snapshot = now
        snippets = []
        for rel in changed[:20]:
            if not _file_allowed(rel):  # 截取前二次校验（防御）
                continue
            p = self.root / rel
            if p.is_file():
                try:
                    snippets.append(f"--- {rel}\n" + p.read_text(encoding="utf-8", errors="ignore")[:300])
                except OSError:
                    pass
        return {
            "source": "watch",
            "files": [f for f in changed if _file_allowed(f)][:20],
            "diff_text": "\n".join(snippets)[:DIFF_LIMIT],
        }

    def _snapshot_files(self) -> dict:
        snap = {}
        def unreadable(error):
            # os.walk otherwise silently treats an inaccessible project as empty.
            raise error
        # Prune dependency/build trees before walking them; filtering rglob's
        # results still traverses every node_modules/.git file on every poll.
        for directory, subdirs, files in os.walk(self.root, followlinks=False, onerror=unreadable):
            subdirs[:] = [name for name in subdirs
                          if name not in SKIP_DIRS and not name.startswith(SKIP_PREFIX)]
            for name in files:
                if name.startswith(SKIP_PREFIX):
                    continue
                p = Path(directory) / name
                rel_str = str(p.relative_to(self.root))
                if not _file_allowed(rel_str):
                    continue
                try:
                    p.resolve().relative_to(self.root.resolve())
                    st = p.stat()
                    if p.is_file() and st.st_size <= MAX_FILE_BYTES:
                        snap[rel_str] = (st.st_mtime_ns, st.st_size)
                except (OSError, ValueError):
                    continue  # Atomic saves/deletes can race with a scan.
        return snap

    def changes(self) -> dict:
        """统一入口：git 优先，无 git 时 watch"""
        if self.is_git_repo():
            return self.git_changes()
        return self.watch_changes()
