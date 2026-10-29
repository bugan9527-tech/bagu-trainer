"""Repair saved watch paths without deleting project files or learning data."""
import os
import stat
from pathlib import Path

from bagu_trainer.infrastructure import config


def _definitely_invalid(value):
    if not isinstance(value, str) or not value.strip():
        return False  # Malformed settings remain visible to the normal validator.
    path = Path(value)
    if not path.is_absolute() or path.drive.startswith("\\\\"):
        return False  # A disconnected network share is not a deleted project.
    try:
        # An unavailable drive must not cause its projects to be forgotten.
        with os.scandir(path.anchor):
            pass
        return not stat.S_ISDIR(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        # Probe the volume again: the first exception may come from the anchor.
        try:
            with os.scandir(path.anchor):
                pass
        except OSError:
            return False
        return True
    except (OSError, ValueError):
        return False  # Permission and temporary I/O failures are not deletion.


def prune_missing_projects():
    """Return (removed, remaining); commit only actual changes under the config lock."""
    with config.config_transaction():
        settings = config.read_user_config()
        roots = settings.get("project_roots")
        if not isinstance(roots, list):
            return [], []
        removed, remaining = [], []
        for root in roots:
            (removed if _definitely_invalid(root) else remaining).append(root)
        if removed:
            settings["project_roots"] = remaining
            config._write_config_atomic(settings)
        return removed, remaining
