"""Resolve complete immutable bank generations through one atomic pointer."""
import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import config


def replace_with_sharing_wait(source, destination, control=None):
    """Windows readers may briefly deny rename; never broaden the operation."""
    deadline = time.monotonic() + 6
    while True:
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            remaining = deadline - time.monotonic()
            if os.name != "nt" or getattr(error, "winerror", None) not in {5, 32, 33} or remaining <= 0:
                raise
            if control is not None:
                control.wait(min(.1, remaining))
            else:
                time.sleep(min(.1, remaining))


def active_bank_dir(project_root=None):
    data = Path(project_root or config.USER_DATA_ROOT) / "data"
    pointer = data / "active-bank.json"
    if not pointer.exists():
        return data  # Existing installations keep their original files.
    value = json.loads(pointer.read_text(encoding="utf-8"))
    generation = value.get("generation") if isinstance(value, dict) else None
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ValueError("题库版本指针无效")
    directory = data / "banks" / generation
    directory.resolve().relative_to((data / "banks").resolve())
    if not directory.is_dir():
        raise ValueError("当前题库版本不存在")
    return directory


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_sharing_wait(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
