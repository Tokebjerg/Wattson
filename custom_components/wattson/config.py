"""Config entry helpers for Wattson."""
from __future__ import annotations

from typing import Any


def entry_value(entry: Any, key: str, default: Any = None) -> Any:
    options = getattr(entry, "options", None) or {}
    if key in options:
        return options[key]
    data = getattr(entry, "data", None) or {}
    return data.get(key, default)


def merged_entry_config(entry: Any) -> dict[str, Any]:
    data = dict(getattr(entry, "data", None) or {})
    options = dict(getattr(entry, "options", None) or {})
    return {**data, **options}


def update_entry_options(hass: Any, entry: Any, **updates: Any) -> bool:
    options = dict(getattr(entry, "options", None) or {})
    options.update(updates)
    return hass.config_entries.async_update_entry(entry, options=options)
