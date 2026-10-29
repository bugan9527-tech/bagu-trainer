"""项目内 Python 未使用导入检查。

检查范围为应用代码 ``bagu_trainer/`` 和构建/辅助脚本 ``scripts/``。测试与验收
目录不纳入自动检查：其中的 pytest fixture 导入、Tk 主题副作用导入需要人工判断。

明确的兼容导出和副作用导入逐项列在 ``EXCEPTIONS`` 中，并打印理由；不提供全局忽略。
仅检查未使用导入，不做风格或类型检查，避免引入新的检查体系。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("bagu_trainer", "scripts")

# 只允许逐项、带理由的例外，不使用目录级或名称级全局忽略。
EXCEPTIONS = {
    ("scripts/check_ui_display.py", "ttkbootstrap"):
        "Tk 主题构建前必须加载的副作用导入",
    ("scripts/check_ui_redesign.py", "ttkbootstrap"):
        "Tk 主题初始化副作用导入",
}


def imported_names(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != "*":
                    yield (alias.asname or alias.name.split(".")[0]), node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for alias in node.names:
                if alias.name != "*":
                    yield (alias.asname or alias.name), node.lineno


def loaded_names(tree):
    loaded = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id != "__all__":
                continue
            if isinstance(node.value, (ast.List, ast.Tuple)):
                for item in node.value.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        loaded.add(item.value)
    return loaded


def main():
    failures, allowed = [], []
    checked = 0
    for scope in SCOPES:
        directory = ROOT / scope
        if not directory.is_dir():
            failures.append(f"{scope}: 检查目录不存在")
            continue
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT).as_posix()
            checked += 1
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError) as error:
                failures.append(f"{relative}: 无法解析（{type(error).__name__}）")
                continue
            used = loaded_names(tree)
            for name, line in imported_names(tree):
                if name in used:
                    continue
                reason = EXCEPTIONS.get((relative, name))
                if reason:
                    allowed.append(f"{relative}:{line}: {name} — {reason}")
                else:
                    failures.append(f"{relative}:{line}: 未使用导入 {name}")
    for item in allowed:
        print(f"[允许] {item}")
    if failures:
        print("Python 未使用导入检查失败：", file=sys.stderr)
        for item in failures:
            print(f"  {item}", file=sys.stderr)
        return 1
    print(f"Python 未使用导入检查通过：{checked} 个文件，{len(allowed)} 项明确例外。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
