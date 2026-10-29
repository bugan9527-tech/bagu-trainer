"""Convert public workflow event data into the existing JSON-compatible shape."""
import dataclasses
from pathlib import Path


def public_value(value):
    if dataclasses.is_dataclass(value):
        return public_value(dataclasses.asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): public_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [public_value(item) for item in value]
    return value
