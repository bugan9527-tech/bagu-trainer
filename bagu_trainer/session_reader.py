# -*- coding: utf-8 -*-
"""会话读取模块：探测 .trae-cn 下的内存文件（明文 JSONL），提取最近 AI 活动。

实测格式（本机 TraeCode）：
  <trae_home>/memory/projects/<项目hash>/<YYYYMMDD>/session_memory_*.jsonl
  每行一个 JSON: {"intent": str, "actions": [str], "outcome": str,
                  "learned": [str], "message_summary_time": "2026-10-18 14:20:15",
                  "message_id": str}
  <trae_home>/memory/projects/<项目hash>/<YYYYMMDD>/topics.md
  每行: "[session_id: xxx | topic_summary_time: yyy]正文"

隐私约束：默认只读取与当前项目明确关联的记录（pass project_root 时按
路径→项目目录哈希唯一匹配，匹配不到即返回空）；跨项目无归属会话
绝不进入出题上下文。服务端通过配置 session_ingest 显式开启。

注：TraeCode 的 ai-agent/database.db 为加密格式，MVP 不读取，仅用上述明文文件。
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

TOPIC_RE = None  # 惰性编译，见 _topics_entries

# 项目目录名后缀形如 --p2-<hex>：必须逐字匹配前缀且恰好唯一才算可靠归属
_SUFFIX_RE_TEMPLATE = r"--p\d+-[0-9a-f]+"


def find_trae_home(cfg_home: str = None):
    path = Path(cfg_home or (Path.home() / ".trae-cn"))
    return path if path.is_dir() else None


def _project_key(project_root) -> str:
    """把绝对路径转成 TraeCode 项目目录名前缀：
    D:\\work\\example-project -> -d-work-example-project"""
    p = str(Path(project_root).resolve()).lower()
    p = re.sub(r"^([a-z]):", r"\1", p)      # 去掉盘符冒号，保留盘符字母
    p = p.replace("\\", "-").replace("/", "-")
    p = re.sub(r"-{2,}", "-", p).strip("-")
    return "-" + p


def _project_dir(projects_root: Path, project_root) -> Path:
    """在 memory/projects 下定位与 project_root 唯一匹配的项目目录。

    目录名要求：与路径键前缀逐字匹配（大小写不敏感），后缀匹配
    --p<数字>-<hex>；恰有一个候选才返回，否则 None（无法可靠判断归属
    即不读取，宁缺毋滥）。
    """
    key = _project_key(project_root)
    cands = [d for d in projects_root.iterdir()
             if d.is_dir() and d.name.lower().startswith(key + "--")]
    valid = [d for d in cands
             if re.fullmatch(re.escape(key) + _SUFFIX_RE_TEMPLATE,
                             d.name.lower())]
    return valid[0] if len(valid) == 1 else None


def read_recent_entries(trae_home, since_minutes: int = 180, limit: int = 8,
                        project_root=None) -> list:
    """读取最近的活动条目，按时间倒序。

    project_root 提供时：只读取与该目录路径唯一匹配的项目会话目录
    （匹配不到返回空，绝不回退到全量跨项目读取）；不提供时保持旧
    行为（全量，仅供显式开启的无归属场景使用）。

    每项: {time, intent, actions, outcome, learned, project, session_id, source}
    """
    base = Path(trae_home) / "memory" / "projects"
    if not base.is_dir():
        return []
    root, jsonl_pat, topic_pat = base, "*/20*/session_memory_*.jsonl", "*/20*/topics.md"
    if project_root:
        root = _project_dir(base, project_root)
        if root is None:
            return []
        jsonl_pat, topic_pat = "20*/session_memory_*.jsonl", "20*/topics.md"
    entries = _jsonl_entries(root, since_minutes, jsonl_pat)
    entries += _topics_entries(root, since_minutes, topic_pat)
    entries.sort(key=lambda e: e["time"], reverse=True)
    return entries[:limit]


def _jsonl_entries(root: Path, since_minutes: int, pattern: str) -> list:
    cutoff = datetime.now() - timedelta(minutes=since_minutes)
    out = []
    for jf in root.glob(pattern):
        try:
            lines = jf.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue  # 文件正被 TraeCode 写入时跳过
        for line in lines:
            try:
                d = json.loads(line)
                t = datetime.strptime(d["message_summary_time"], "%Y-%m-%d %H:%M:%S")
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if t < cutoff:
                continue
            out.append({
                "time": t,
                "intent": d.get("intent") or "",
                "actions": d.get("actions") or [],
                "outcome": d.get("outcome") or "",
                "learned": d.get("learned") or [],
                "project": jf.parent.parent.name,
                "session_id": d.get("message_id", ""),
                "source": "session_memory",
            })
    return out


def _topics_entries(root: Path, since_minutes: int, pattern: str) -> list:
    global TOPIC_RE
    TOPIC_RE = re.compile(r"\[session_id: (\S+) \| topic_summary_time: ([\d\- :]+)\](.*)")
    cutoff = datetime.now() - timedelta(minutes=since_minutes)
    out = []
    for tf in root.glob(pattern):
        try:
            lines = tf.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            m = TOPIC_RE.match(line)
            if not m:
                continue
            try:
                t = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if t < cutoff:
                continue
            out.append({
                "time": t,
                "intent": m.group(3).strip(),
                "actions": [],
                "outcome": "",
                "learned": [],
                "project": tf.parent.parent.name,
                "session_id": m.group(1),
                "source": "topics",
            })
    return out


def entries_to_text(entries: list, max_chars: int = 2000) -> str:
    """把条目拼成纯文本，供匹配/出题使用（intent + actions 权重最高）"""
    parts = []
    for e in entries:
        seg = [e["intent"]] + list(e["actions"]) + ([e["outcome"]] if e["outcome"] else [])
        text = " ".join(s for s in seg if s).strip()
        if text:
            parts.append(text)
    return "\n".join(parts)[:max_chars]


if __name__ == "__main__":
    # 小工具用法: python -m bagu_trainer.session_reader [分钟数]
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    home = find_trae_home()
    print(f"trae_home: {home}")
    for e in read_recent_entries(home, since_minutes=minutes):
        print(json.dumps(
            {"time": str(e["time"]), "project": e["project"], "source": e["source"],
             "intent": e["intent"][:120], "actions": e["actions"][:4]},
            ensure_ascii=False))
