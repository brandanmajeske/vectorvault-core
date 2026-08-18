"""Working-set pins and cite-key extraction (V-47).

Named pins are stored as procedural memories under ``working-set-{name}`` task_ids.
Cite expansion scans ``supersedes``, ``parent_key``, and inline ``mem_…`` references.
"""

from __future__ import annotations

import json
import re

WORKING_SET_TASK_PREFIX = "working-set-"

# ``mem_{agent}_{task}_{hash16}_v{version}`` — agent/task segments are alphanumeric + hyphen.
MEM_KEY_PATTERN = re.compile(r"\bmem_[a-zA-Z0-9][a-zA-Z0-9_-]*_v\d+\b")

DEFAULT_EXPAND_MAX_KEYS = 16
DEFAULT_EXPAND_MAX_DEPTH = 1
DEFAULT_PIN_TTL_S = 7 * 86400


def working_set_task_id(name: str) -> str:
    """Canonical task_id for a named working-set pin."""
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("name must be non-empty")
    if cleaned.startswith(WORKING_SET_TASK_PREFIX):
        return cleaned
    return f"{WORKING_SET_TASK_PREFIX}{cleaned}"


def extract_mem_keys(*texts: str | None) -> list[str]:
    """Return unique ``mem_…`` keys found in ``texts``, in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        if not text:
            continue
        for match in MEM_KEY_PATTERN.finditer(text):
            key = match.group(0)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def encode_pin_content(name: str, keys: list[str]) -> str:
    return json.dumps({"name": name, "keys": keys}, separators=(",", ":"))


def decode_pin_content(content: str | None) -> list[str]:
    """Parse keys from a pin memory body; returns [] on missing/invalid JSON."""
    if not content:
        return []
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("keys")
    if not isinstance(raw, list):
        return []
    return [k.strip() for k in raw if isinstance(k, str) and k.strip()]
