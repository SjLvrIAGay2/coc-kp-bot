"""Canonical JSON shared by domain audit values and persistence adapters."""
import json
from typing import Any


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
