"""Optional CLI adapter. Desktop builds use bagu_trainer.infrastructure.bank_build directly."""
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bagu_trainer.infrastructure.bank_build.extraction import *
from bagu_trainer.infrastructure.bank_build.extraction import main as _main

def fetch_daily_issues(entries) -> int:
    """抓取 Daily-Interview-Question 的 Issue 标题（每日一题的题目），返回条数。"""
    try:
        out = subprocess.run(
            ["gh", "api", "--paginate",
             "repos/Advanced-Frontend/Daily-Interview-Question/issues?state=all&per_page=100",
             "--jq", ".[].title"],
            capture_output=True, text=True, timeout=300)
    except Exception as ex:
        print("[issues] fetch failed:", ex)
        return 0
    if out.returncode != 0:
        print("[issues] gh api exit", out.returncode, out.stderr[:200])
        return 0
    titles = [t for t in out.stdout.splitlines() if t.strip()]
    n = 0
    for i, t in enumerate(titles):
        before = len(entries)
        collect_entry(entries, "Daily-Interview-Question",
                      "issues://#%d" % i, i, "question", t)
        if len(entries) > before:
            n += 1
    return n


def main(argv=None):
    return _main(argv, repos_dir=REPOS_DIR, out_dir=OUT_DIR, fetch_daily_issues=fetch_daily_issues)

if __name__ == "__main__":
    main()
