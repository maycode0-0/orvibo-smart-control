"""Validation shared by light state readers and control routing."""

from __future__ import annotations

import math
from typing import Any


def normalize_brightness(value: Any, maximum: int = 255) -> int | None:
    """Ignore unknown/negative readings; clamp valid readings to their unit range."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(min(number, maximum))
