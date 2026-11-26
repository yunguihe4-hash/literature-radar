"""Install the first-party Obsidian Bridge into one explicitly selected Vault.

Run this file from the source checkout. It never changes Obsidian's enabled
plugins list; the user enables the Bridge in Obsidian after installation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


PLUGIN_ID = "literature-radar-bridge"
ASSETS = ("manifest.json", "main.js", "styles.css")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def install(vault: Path, *, upgrade: bool = False) -> dict:
    vault = vault.expanduser().resolve(strict=True)
    obsidian = vault / ".obsidian"
    if not obsidian.is_dir() or obsidian.is_symlink():
        raise ValueError("Choose an existing Obsidian Vault containing a real .obsidian directory")
    source = Path(__file__).resolve().parent
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("id") != PLUGIN_ID or not all((source / name).is_file() for name in ASSETS):
        raise ValueError("Bridge distribution is incomplete or has the wrong plugin ID")
    plugins = obsidian / "plugins"
    if plugins.is_symlink():
        raise ValueError("Refusing a symlinked Obsidian plugins directory")
    target = plugins / PLUGIN_ID
    if target.is_symlink():
        raise ValueError("Refusing a symlinked Bridge plugin directory")
    if target.exists():
        if not target.is_dir():
            raise ValueError("Bridge destination exists and is not a directory")
        installed_manifest = target / "manifest.json"
        if not installed_manifest.is_file() or json.loads(installed_manifest.read_text(encoding="utf-8")).get("id") != PLUGIN_ID:
            raise ValueError("Bridge destination belongs to a different plugin or is unrecognized")
        if all((target / name).is_file() and _sha256(source / name) == _sha256(target / name) for name in ASSETS):
            return {"status": "ALREADY_INSTALLED", "version": manifest["version"], "path": str(target)}
        if not upgrade:
            raise ValueError("Bridge already exists; pass --upgrade to back it up and replace its code assets")
        backups = obsidian / "literature-radar-backups"
        backups.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backups / f"{PLUGIN_ID}-{stamp}"
        if backup.exists():
            raise ValueError("Backup destination already exists; retry later")
        shutil.copytree(target, backup, symlinks=True)
        status = "UPGRADED"
    else:
        plugins.mkdir(mode=0o700, exist_ok=True)
        target.mkdir(mode=0o700)
        backup = None
        status = "INSTALLED"
    for name in ASSETS:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=target)
        try:
            with os.fdopen(descriptor, "wb") as output, (source / name).open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
            os.replace(temporary, target / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {"status": status, "version": manifest["version"], "path": str(target),
            "backup": str(backup) if backup else None,
            "next_step": "Enable Literature Radar Bridge in Obsidian Community plugins"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True, help="Existing Obsidian Vault path")
    parser.add_argument("--upgrade", action="store_true", help="Back up an existing Bridge before replacing its code")
    arguments = parser.parse_args()
    print(json.dumps(install(arguments.vault, upgrade=arguments.upgrade), indent=2))


if __name__ == "__main__":
    main()
