"""Unchanged AI_OS small YAML parser for explicit, private profile migration.
Source SHA256: 47a489e15c34aae14628a08fb15f67d9bc6b9876ec0c83119cfde5e8c805240d
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any

def load_json_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return parse_simple_yaml(text, path)


def yaml_scalar(value: str) -> Any:
    value = value.strip()
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value in {"null", "~"}:
        return None
    if value in {"true", "false"}:
        return value == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_simple_yaml(value: str, path: Path) -> dict[str, Any]:
    """Parse the small, dependency-free YAML subset used by attention config."""
    lines = [
        (len(raw) - len(raw.lstrip(" ")), raw.strip())
        for raw in value.splitlines()
        if raw.strip() and not raw.lstrip().startswith("#")
    ]
    if any(indent % 2 for indent, _ in lines):
        raise ValueError(f"invalid YAML indentation in {path}")

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines) or lines[index][0] < indent:
            return {}, index
        is_list = lines[index][1].startswith("- ")
        result: Any = [] if is_list else {}
        while index < len(lines) and lines[index][0] == indent:
            text = lines[index][1]
            if is_list:
                if not text.startswith("- "):
                    raise ValueError(f"mixed YAML collection in {path}")
                item = text[2:].strip()
                if not item:
                    child, index = parse_block(index + 1, indent + 2)
                    result.append(child)
                    continue
                if ":" in item:
                    key, raw_value = item.split(":", 1)
                    entry = {key.strip(): yaml_scalar(raw_value) if raw_value.strip() else None}
                    index += 1
                    while index < len(lines) and lines[index][0] > indent:
                        child_indent, child_text = lines[index]
                        if child_indent != indent + 2 or ":" not in child_text:
                            raise ValueError(f"invalid YAML list mapping in {path}")
                        child_key, child_value = child_text.split(":", 1)
                        if child_value.strip():
                            entry[child_key.strip()] = yaml_scalar(child_value)
                            index += 1
                        else:
                            child, index = parse_block(index + 1, indent + 4)
                            entry[child_key.strip()] = child
                    result.append(entry)
                    continue
                result.append(yaml_scalar(item))
                index += 1
            else:
                if text.startswith("- ") or ":" not in text:
                    raise ValueError(f"invalid YAML mapping in {path}")
                key, raw_value = text.split(":", 1)
                index += 1
                if raw_value.strip():
                    result[key.strip()] = yaml_scalar(raw_value)
                else:
                    child, index = parse_block(index, indent + 2)
                    result[key.strip()] = child
        return result, index

    document, end = parse_block(0, lines[0][0] if lines else 0)
    if end != len(lines) or not isinstance(document, dict):
        raise ValueError(f"invalid YAML document in {path}")
    return document
