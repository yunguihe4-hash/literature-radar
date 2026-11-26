"""Reject user research state under the source checkout or installed release."""
from __future__ import annotations

from pathlib import Path


def ensure_private_path(path: Path) -> Path:
    destination = Path(path).expanduser().resolve()
    source = Path(__file__).resolve()
    forbidden = []
    for parent in source.parents:
        if (parent / "pyproject.toml").is_file():
            forbidden.append(parent)
            break
        if (parent / "pyvenv.cfg").is_file():
            forbidden.append(parent)
            if parent.parent.parent.name == "releases":
                forbidden.append(parent.parent)
            break
    if any(destination == root or destination.is_relative_to(root) for root in forbidden):
        raise ValueError("Private configuration or runtime data cannot be written inside source or release files")
    return destination
