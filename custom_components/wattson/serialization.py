"""JSON-safe snapshots without lossy stringification of datamodels."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
import math
from collections.abc import Mapping


def json_safe(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: json_safe(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, Mapping):
        return {str(json_safe(key)): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
