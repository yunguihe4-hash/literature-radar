"""Obsidian Vault discovery, private binding, and safe Topic-scoped card delivery."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
import unicodedata
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

from literature_radar.delivery.cards import END, card_key, frontmatter
from literature_radar.delivery.review_ui import set_properties, review_state
from literature_radar.private_paths import ensure_private_path

DASH_BEGIN = "<!-- LITERATURE-RADAR-DELIVERY:BEGIN -->"
DASH_END = "<!-- LITERATURE-RADAR-DELIVERY:END -->"
INDEX_BEGIN = "<!-- LITERATURE-RADAR-INDEX:BEGIN -->"
INDEX_END = "<!-- LITERATURE-RADAR-INDEX:END -->"
METADATA_BEGIN = "<!-- LITERATURE-RADAR-METADATA:BEGIN -->"
METADATA_END = "<!-- LITERATURE-RADAR-METADATA:END -->"
NAMESPACE = "Literature-Radar"
BINDING_SCHEMA_VERSION = 2


class ObsidianError(ValueError):
    pass


def binding_file(*, home: Path | None = None, platform: str | None = None,
                 environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    platform = platform or sys.platform
    home = Path(home or Path.home())
    override = env.get("LITERATURE_RADAR_PRIVATE_CONFIG_DIR")
    if override:
        return ensure_private_path(Path(override) / "obsidian.json")
    if platform == "win32":
        base = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    elif platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return ensure_private_path(base / "literature-radar" / "obsidian.json")


def registry_file(*, home: Path | None = None, platform: str | None = None,
                  environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    platform = platform or sys.platform
    home = Path(home or Path.home())
    if platform == "win32":
        base = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    elif platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "obsidian" / "obsidian.json"


def discover_vaults(*, registry: Path | None = None, home: Path | None = None,
                    platform: str | None = None, environ: dict | None = None) -> list[Path]:
    valid = set()
    paths = [registry] if registry is not None else [registry_file(home=home, platform=platform, environ=environ)]
    if registry is None and (platform or sys.platform).startswith("linux"):
        home = Path(home or Path.home())
        paths.extend((home / ".var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json",
                      home / "snap/obsidian/current/.config/obsidian/obsidian.json"))
    for path in paths:
        if not path.is_file():
            continue
        try:
            registered = json.loads(path.read_text(encoding="utf-8"))["vaults"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if isinstance(registered, dict):
            for item in registered.values():
                raw = item.get("path") if isinstance(item, dict) else None
                if isinstance(raw, str):
                    candidate = Path(raw).expanduser()
                    if candidate.is_absolute() and candidate.is_dir() and (candidate / ".obsidian").is_dir():
                        valid.add(candidate.resolve())
    return sorted(valid)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".obsidian-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _writable(directory: Path) -> bool:
    return directory.is_dir() and bool(directory.stat().st_mode & 0o222) and os.access(directory, os.W_OK)


def _valid_name(name: str) -> bool:
    import re
    return (bool(name) and name == name.strip() and name not in (".", "..")
            and not name.endswith(".") and not re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            and name.upper() not in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"})


def preview_new_vault(parent: Path, name: str = NAMESPACE, *,
                      registered: list[Path] | None = None) -> Path:
    """Validate a new Vault target without changing the filesystem."""
    if not _valid_name(name):
        raise ObsidianError("Vault name is not a safe single directory name")
    parent = Path(parent).expanduser().resolve()
    if not _writable(parent):
        raise ObsidianError("Vault parent directory does not exist or is not writable")
    for ancestor in (parent, *parent.parents):
        if (ancestor / ".obsidian").is_dir():
            raise ObsidianError("A dedicated Vault cannot be created inside another Vault")
        if ancestor != Path(tempfile.gettempdir()).resolve() and (
                (ancestor / ".git").exists() or (ancestor / "pyproject.toml").is_file()):
            raise ObsidianError("A dedicated Vault cannot be created inside a project directory")
    target = parent / name
    if target.is_symlink():
        raise ObsidianError("Vault target cannot be a symlink")
    managed_resume = (target.is_dir() and (target / ".obsidian").is_dir() and
                      (_read_marker(target) or {}).get("layout") == "dedicated-root")
    for existing in registered if registered is not None else discover_vaults():
        existing = Path(existing).resolve()
        if (target == existing and not managed_resume) or target != existing and (
                target.is_relative_to(existing) or existing.is_relative_to(target)):
            raise ObsidianError("Vault target overlaps an existing registered Vault")
    if target.exists():
        if not target.is_dir() or any(target.iterdir()) and not managed_resume:
            raise ObsidianError("Vault target already exists and is not empty; choose another path or select existing Vault mode")
    return target


def _namespace_root(vault: Path) -> Path:
    root = vault / NAMESPACE
    if root.is_symlink() or not root.resolve().is_relative_to(vault.resolve()):
        raise ObsidianError("Literature Radar namespace escapes Vault")
    return root


def _marker_path(root: Path) -> Path:
    return root / "System" / "delivery" / "namespace.json"


def _read_marker(root: Path) -> dict | None:
    marker = _marker_path(root)
    if not marker.is_file() or marker.is_symlink() or not _within(root, marker):
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("namespace") == NAMESPACE and data.get("schema_version") == BINDING_SCHEMA_VERSION else None


def _initialize_namespace(vault: Path, topic_ids: tuple[str, ...] = ()) -> tuple[Path, str]:
    root = _namespace_root(vault)
    if root.exists() and (not root.is_dir() or not _writable(root)):
        raise ObsidianError("Literature Radar namespace is not a writable directory")
    marker = _read_marker(root) if root.exists() else None
    if root.exists() and any(root.iterdir()) and marker is None:
        raise ObsidianError("Literature-Radar directory already contains files without a valid ownership marker")
    for topic in topic_ids:
        if not _safe_topic(topic):
            raise ObsidianError("Invalid Topic ID for Vault initialization")
    if root.exists():
        for child in (root / "Bootstrap", root / "Weekly", root / "System",
                      root / "System" / "delivery", root / "System" / "feedback"):
            if child.is_symlink() or child.exists() and not child.is_dir() or not _within(root, child):
                raise ObsidianError("Literature Radar namespace has an unsafe directory conflict")
        dashboard = root / "dashboard.md"
        if dashboard.is_symlink() or dashboard.exists() and not dashboard.is_file():
            raise ObsidianError("Literature Radar dashboard has an unsafe file conflict")
    root.mkdir(mode=0o700, exist_ok=True)
    for relative in ("Bootstrap", "Weekly", "System/delivery", "System/feedback"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for topic in topic_ids:
        for section in ("Bootstrap", "Weekly"):
            destination = root / section / topic
            if destination.is_symlink() or not _within(root, destination):
                raise ObsidianError("Topic directory escapes Literature Radar namespace")
            destination.mkdir(exist_ok=True)
    identifier = str(marker["vault_identifier"]) if marker and marker.get("vault_identifier") else uuid.uuid4().hex
    if marker is None:
        _write_json(_marker_path(root), {"namespace": NAMESPACE, "schema_version": BINDING_SCHEMA_VERSION,
                                         "vault_identifier": identifier})
    dashboard = root / "dashboard.md"
    if not dashboard.exists():
        with dashboard.open("x", encoding="utf-8") as handle:
            handle.write("# Literature Radar\n\n" + _dashboard(root, {"cards": {}}))
    return root, identifier


def _initialize_dedicated_root(vault: Path, topic_ids: tuple[str, ...] = ()) -> tuple[Path, str]:
    """A dedicated Vault is itself the delivery root; no second nested namespace."""
    root = vault
    marker = _read_marker(root)
    existing = {item.name for item in root.iterdir()}
    if marker is None and existing - {".obsidian"}:
        raise ObsidianError("Dedicated Vault contains unknown files without a valid ownership marker")
    for topic in topic_ids:
        if not _safe_topic(topic):
            raise ObsidianError("Invalid Topic ID for dedicated Vault")
    for relative in ("Bootstrap", "Weekly", "System", "System/delivery", "System/feedback"):
        child = root / relative
        if child.is_symlink() or child.exists() and not child.is_dir() or not _within(root, child):
            raise ObsidianError("Dedicated Vault has an unsafe directory conflict")
        child.mkdir(parents=True, exist_ok=True)
    for topic in topic_ids:
        for section in ("Bootstrap", "Weekly"):
            destination = root / section / topic
            if destination.is_symlink() or not _within(root, destination):
                raise ObsidianError("Topic directory escapes dedicated Vault")
            destination.mkdir(exist_ok=True)
        (root / "Bootstrap" / topic / "BOOTSTRAP_SEED").mkdir(exist_ok=True)
        for section in ("Bootstrap", "Weekly"):
            index = root / section / topic / "index.md"
            if index.is_symlink() or index.exists() and not index.is_file():
                raise ObsidianError("Topic index has an unsafe file conflict")
            if not index.exists():
                with index.open("x", encoding="utf-8") as handle:
                    handle.write(f"# {topic}\n\n{INDEX_BEGIN}\n\n待交付阅读卡片。\n\n{INDEX_END}\n")
    identifier = str(marker["vault_identifier"]) if marker and marker.get("vault_identifier") else uuid.uuid4().hex
    if marker is None:
        _write_json(_marker_path(root), {"namespace": NAMESPACE, "schema_version": BINDING_SCHEMA_VERSION,
                                         "vault_identifier": identifier, "layout": "dedicated-root"})
    for relative, text in (("README.md", "# Literature Radar\n\n从 [[dashboard]] 开始阅读；在每篇卡片的「我的评价」中点击一个反馈按钮。\n"),
                           ("System/README.md", "# Literature Radar System\n\n交付状态与反馈入口。研究配置和数据库保存在 Vault 外。\n")):
        path = root / relative
        if path.is_symlink() or path.exists() and not path.is_file():
            raise ObsidianError("Dedicated Vault has an unsafe README conflict")
        if not path.exists():
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text)
    dashboard = root / "dashboard.md"
    if dashboard.is_symlink() or dashboard.exists() and not dashboard.is_file():
        raise ObsidianError("Dedicated Vault has an unsafe dashboard conflict")
    if not dashboard.exists():
        with dashboard.open("x", encoding="utf-8") as handle:
            handle.write("# Literature Radar\n\n" + _dashboard(root, {"cards": {}}, prefix=""))
    return root, identifier


def _read_binding(*, config: Path | None = None, require_live: bool = True) -> dict | None:
    path = ensure_private_path(config or binding_file())
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ObsidianError("Obsidian binding is unreadable; run literature-radar obsidian setup") from exc
    if isinstance(data, dict) and data.get("schema_version") == BINDING_SCHEMA_VERSION and data.get("state") == "deferred":
        return None
    if not isinstance(data, dict) or data.get("schema_version") != BINDING_SCHEMA_VERSION:
        raise ObsidianError("Obsidian binding uses an older schema; explicitly choose a Vault again with literature-radar obsidian setup")
    required = ("vault_root", "delivery_root", "vault_mode", "vault_identifier", "binding_created_at")
    if any(not isinstance(data.get(key), str) or not data[key] for key in required):
        raise ObsidianError("Obsidian binding is incomplete; run literature-radar obsidian setup")
    vault = Path(data["vault_root"])
    root = Path(data["delivery_root"])
    valid_root = (root == vault / NAMESPACE or data["vault_mode"] == "dedicated" and root == vault)
    if not vault.is_absolute() or not root.is_absolute() or not valid_root:
        raise ObsidianError("Obsidian binding contains an unsafe delivery path")
    if data["vault_mode"] not in ("dedicated", "existing"):
        raise ObsidianError("Obsidian binding mode is invalid")
    if require_live:
        if not vault.is_dir() or not (vault / ".obsidian").is_dir():
            raise ObsidianError(f"Bound Vault moved or removed: {vault}; run literature-radar obsidian setup to reconnect")
        if root.is_symlink() or not root.is_dir() or not _within(vault, root):
            raise ObsidianError("Bound Literature Radar namespace is missing or escapes Vault")
        marker = _read_marker(root)
        if marker is None or marker.get("vault_identifier") != data["vault_identifier"]:
            raise ObsidianError("Literature Radar namespace ownership marker does not match binding")
    return data


def _save_binding(vault: Path, root: Path, mode: str, identifier: str,
                  *, config: Path | None = None, rebind: bool = False) -> dict:
    path = ensure_private_path(config or binding_file())
    try:
        old = _read_binding(config=path, require_live=False)
    except ObsidianError:
        if not rebind:
            raise
        old = None
    if old and old["vault_root"] != str(vault) and not rebind:
        raise ObsidianError("A different Vault is already bound; explicitly confirm rebind")
    if old and old["vault_root"] == str(vault) and old["vault_identifier"] == identifier:
        return {"status": "ALREADY_BOUND", "vault_root": str(vault),
                "delivery_root": str(root), "vault_mode": old["vault_mode"],
                "vault_identifier": identifier, "binding_file": str(path)}
    payload = {"schema_version": BINDING_SCHEMA_VERSION, "vault_root": str(vault),
               "delivery_root": str(root), "vault_mode": mode,
               "vault_identifier": identifier,
               "binding_created_at": datetime.now(timezone.utc).isoformat()}
    prior_manifest = _manifest_path(path)
    manifest_backup = None
    if path.exists():
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.backup-{suffix}")
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
        if prior_manifest.exists():
            manifest_backup = prior_manifest.with_name(f"{prior_manifest.name}.backup-{suffix}")
            shutil.copy2(prior_manifest, manifest_backup)
            os.chmod(manifest_backup, 0o600)
    _write_json(path, payload)
    if manifest_backup is not None:
        prior_manifest.replace(manifest_backup)
    return {"status": "BOUND", **payload, "binding_file": str(path)}


def select_existing_vault(vault: Path, *, config: Path | None = None,
                          topic_ids: tuple[str, ...] = (), rebind: bool = False) -> dict:
    vault = Path(vault).expanduser().resolve()
    if not vault.is_dir() or not (vault / ".obsidian").is_dir() or not _writable(vault):
        raise ObsidianError("Selected path is not a writable Obsidian Vault with a .obsidian directory")
    path = ensure_private_path(config or binding_file())
    try:
        old = _read_binding(config=path, require_live=False)
    except ObsidianError:
        if not rebind:
            raise
        old = None
    if old and old["vault_root"] != str(vault) and not rebind:
        raise ObsidianError("A different Vault is already bound; explicitly confirm rebind")
    root, identifier = _initialize_namespace(vault, topic_ids)
    return _save_binding(vault, root, "existing", identifier, config=path, rebind=rebind)


def create_dedicated_vault(parent: Path, name: str = NAMESPACE, *,
                           config: Path | None = None, topic_ids: tuple[str, ...] = (),
                           registered: list[Path] | None = None, rebind: bool = False) -> dict:
    path = ensure_private_path(config or binding_file())
    try:
        old = _read_binding(config=path, require_live=False)
    except ObsidianError:
        if not rebind:
            raise
        old = None
    if not _valid_name(name):
        raise ObsidianError("Vault name is not a safe single directory name")
    target = Path(parent).expanduser().resolve() / name
    if old and old["vault_root"] == str(target) and old["vault_mode"] == "dedicated":
        live = _read_binding(config=path)
        return {"status": "ALREADY_BOUND", **live, "binding_file": str(path),
                "registration_hint": "Open the Vault folder in Obsidian to register it"}
    target = preview_new_vault(parent, name, registered=registered)
    if old and old["vault_root"] != str(target) and not rebind:
        raise ObsidianError("A different Vault is already bound; explicitly confirm rebind")
    target.mkdir(mode=0o700, exist_ok=True)
    (target / ".obsidian").mkdir(exist_ok=True)
    root, identifier = _initialize_dedicated_root(target, topic_ids)
    result = _save_binding(target, root, "dedicated", identifier, config=path, rebind=rebind)
    return {**result, "registration_hint": "Open the Vault folder in Obsidian to register it"}


def bind_vault(vault: Path, *, config: Path | None = None) -> dict:
    """Compatibility alias: an explicitly supplied Vault uses existing-Vault mode."""
    return select_existing_vault(vault, config=config)


def defer_setup(*, config: Path | None = None) -> dict:
    path = ensure_private_path(config or binding_file())
    try:
        existing = _read_binding(config=path, require_live=False)
    except ObsidianError:
        existing = None
    if existing:
        return {"status": "ALREADY_BOUND", "vault_root": existing["vault_root"],
                "delivery_root": existing["delivery_root"]}
    if path.exists():
        backup = path.with_name(f"{path.name}.backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}")
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    _write_json(path, {"schema_version": BINDING_SCHEMA_VERSION, "state": "deferred"})
    return {"status": "DEFERRED", "next": "literature-radar obsidian setup"}


def bound_vault(*, config: Path | None = None) -> Path | None:
    binding = _read_binding(config=config)
    return Path(binding["vault_root"]) if binding else None


def _within(root: Path, child: Path) -> bool:
    return child.resolve().is_relative_to(root.resolve())


def _literature(vault: Path) -> Path:
    """Compatibility name for the private Literature Radar namespace."""
    return _namespace_root(vault)


def doctor(*, config: Path | None = None) -> dict:
    path = config or binding_file()
    if not path.exists():
        return {"status": "OBSIDIAN_NOT_CONNECTED", "next": "literature-radar obsidian setup"}
    try:
        if _read_binding(config=path, require_live=False) is None:
            return {"status": "OBSIDIAN_NOT_CONNECTED", "setup_state": "DEFERRED",
                    "next": "literature-radar obsidian setup"}
    except ObsidianError:
        pass
    issues = []
    if path.stat().st_mode & 0o077:
        issues.append("binding file must have mode 600")
    try:
        binding = _read_binding(config=path)
        vault = Path(binding["vault_root"])
        root = Path(binding["delivery_root"])
        if not _writable(root):
            issues.append("Literature Radar delivery directory is not writable")
        if not _writable(vault):
            issues.append("Vault is not writable")
        for relative in ("Bootstrap", "Weekly", "System/delivery", "System/feedback"):
            directory = root / relative
            if not directory.is_dir() or not _within(root, directory):
                issues.append(f"Missing or unsafe namespace directory: {relative}")
        manifest_file = _manifest_path(path)
        if manifest_file.exists():
            if manifest_file.stat().st_mode & 0o077:
                issues.append("delivery manifest must have mode 600")
            try:
                manifest = _load_manifest(path)
                for item in manifest["cards"].values():
                    relative = Path(item["relative_path"])
                    if relative.is_absolute() or ".." in relative.parts or not _within(root, root / relative):
                        issues.append("delivery manifest contains an unsafe path")
                        break
                    if not (root / relative).is_file():
                        issues.append("delivery manifest refers to a missing card")
                        break
            except (OSError, ValueError, KeyError, TypeError):
                issues.append("delivery manifest is unreadable")
    except ObsidianError as exc:
        issues.append(str(exc))
        vault = None
        root = None
    return {"status": "PASS" if not issues else "FAIL", "vault": str(vault) if vault else None,
            "delivery_root": str(root) if root else None, "literature_root": str(root) if root else None,
            "issues": issues}


def _manifest_path(config: Path) -> Path:
    return config.with_name("obsidian-delivery-manifest.json")


def _load_manifest(config: Path) -> dict:
    path = _manifest_path(config)
    if path.is_file():
        result = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(result, dict) and isinstance(result.get("cards"), dict):
            return result
    return {"version": 1, "cards": {}, "last_delivery": None, "last_status": None}


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".radar-card-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _safe_topic(topic_id: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", topic_id))


def _managed_card_location(relative: str) -> tuple[str, str, str, str | None] | None:
    """Accept legacy Vault paths and dated Weekly paths without moving either."""
    path = Path(relative)
    parts = path.parts
    if path.is_absolute() or ".." in parts or path.suffix != ".md":
        return None
    if len(parts) == 4 and parts[0] in ("Bootstrap", "Weekly"):
        section, topic, level, _ = parts
        delivery_day = None
    elif len(parts) == 5 and parts[0] == "Weekly":
        section, delivery_day, topic, level, _ = parts
        try:
            if len(delivery_day) != 10 or date.fromisoformat(delivery_day).isoformat() != delivery_day:
                return None
        except ValueError:
            return None
    else:
        return None
    allowed = ("BOOTSTRAP_SEED", "HIGH", "MEDIUM", "LOW") if section == "Bootstrap" else ("HIGH", "MEDIUM", "LOW")
    return (section, topic, level, delivery_day) if _safe_topic(topic) and level in allowed else None


def _title_filename(title: str) -> str:
    """Use a readable title while staying within common filesystem limits."""
    import re
    value = unicodedata.normalize("NFC", str(title))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = " ".join(value.split()).strip(" .")
    if not value:
        raise ObsidianError("Paper title cannot form a safe filename")
    while len(value.encode("utf-8")) > 180:
        value = value[:-1].rstrip(" .")
    return value + ".md"


def _title_relative(manifest: dict, item_id: str, section: str, topic: str,
                    level: str, title: str, *, delivery_day: str | None = None) -> str:
    base = _title_filename(title)[:-3]
    taken = {item["relative_path"].casefold() for key, item in manifest["cards"].items() if key != item_id}
    digest = hashlib.sha256(item_id.encode()).hexdigest()
    for suffix in ("", f" ({digest[:8]})", f" ({digest[:12]})", f" ({digest[:16]})"):
        prefix = f"Weekly/{delivery_day}" if section == "Weekly" and delivery_day else section
        relative = f"{prefix}/{topic}/{level}/{base}{suffix}.md"
        if relative.casefold() not in taken:
            return relative
    raise ObsidianError("Too many duplicate paper titles")


def _merge_card(source: str, existing: str) -> str:
    source_meta = frontmatter(source)
    old_meta = frontmatter(existing)
    for field in ("topic_id", "paper_key", "paper_id"):
        if old_meta.get(field) != source_meta.get(field):
            raise ObsidianError("Existing card identity conflicts with source")
    if END not in source or END not in existing:
        raise ObsidianError("Existing card has no managed boundary; refusing overwrite")
    # Keep the entire human region, including feedback and notes.
    human_marker = "## 我的评价"
    if human_marker not in existing:
        raise ObsidianError("Existing card has no human feedback region; refusing overwrite")
    generated = source[:source.index(human_marker)]
    old_review = {key: old_meta[key] for key in
                  ("paper_title", "feedback_decision", "review_status", "feedback_updated_at",
                  "feedback_sync_status", "feedback_synced_decision") if key in old_meta}
    if old_review:
        generated = set_properties(generated, old_review)
    prior_extra = existing[existing.index(END) + len(END):existing.index(human_marker)]
    if METADATA_BEGIN not in generated and METADATA_BEGIN in prior_extra:
        generated = generated.rstrip() + "\n\n" + prior_extra.strip() + "\n\n"
    return generated + existing[existing.index(human_marker):]


def _enrich_card(raw: str, metadata: dict | None) -> str:
    if not metadata or END not in raw or "## 我的评价" not in raw:
        return raw
    safe = lambda value: " ".join(str(value or "未核实").split()).replace("<", "&lt;").replace(">", "&gt;")
    lines = [METADATA_BEGIN, "## 文献元数据", "",
             f"- 期刊：{safe(metadata.get('journal'))}",
             f"- 科学相关性分数：{safe(metadata.get('score'))}",
             "- 主要发现：见上文「摘要」与「相关性证据」；不额外推断。",
             "- 跨物种方法迁移价值：适用时见上文「为什么推荐」与「方法学」。",
             METADATA_END]
    block = "\n".join(lines)
    position = raw.index("## 我的评价")
    return raw[:position].rstrip() + "\n\n" + block + "\n\n" + raw[position:]


def _inventory(root: Path, manifest: dict) -> dict[str, dict[str, list[dict]]]:
    """Count only cards that actually exist inside the authorized delivery root."""
    from literature_radar.feedback.cards import parse_decision

    result: dict[str, dict[str, list[dict]]] = {"Bootstrap": {}, "Weekly": {}}
    for item in manifest["cards"].values():
        relative = item.get("relative_path", "")
        location = _managed_card_location(relative)
        if location is None:
            continue
        section, topic, level, _ = location
        target = root / relative
        if not _within(root, target) or not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8")
            meta = frontmatter(content)
            if (meta.get("topic_id") != topic or meta.get("paper_key") != item.get("paper_key")
                    or level not in {meta.get("first_attention"), meta.get("current_attention")}):
                continue
            try:
                decision, _ = parse_decision(content)
                state, _ = review_state(content)
                if "review_status" not in meta:
                    state = "reviewed" if decision else "pending"
            except ValueError:
                decision = "CONFLICT"
                state = "invalid"
        except (OSError, UnicodeError, ValueError):
            continue
        result[section].setdefault(topic, []).append({
            "relative": relative, "title": item.get("title") or target.stem,
            "topic_name": meta.get("topic_display_name") or topic,
            "attention": meta.get("current_attention") or "UNKNOWN",
            "decision": decision, "review_status": state,
            "sync_status": meta.get("feedback_sync_status", "")})
    return result


def _dashboard(root: Path, manifest: dict, *, prefix: str = NAMESPACE,
               config: Path | None = None) -> str:
    found = _inventory(root, manifest)
    topics = sorted({topic for section in ("Bootstrap", "Weekly") for topic in found[section]} |
                    {p.name for section in ("Bootstrap", "Weekly") if (root / section).is_dir()
                     for p in (root / section).iterdir() if p.is_dir() and _safe_topic(p.name)
                     and (section == "Bootstrap" or (p / "index.md").is_file())})
    all_cards = [card for section in found.values() for cards in section.values() for card in cards]
    decisions = {name: sum(card["decision"] == name for card in all_cards)
                 for name in ("must_read", "keep", "later", "discard")}
    reviewed = sum(card["review_status"] == "reviewed" for card in all_cards)
    pending_total = sum(card["review_status"] == "pending" for card in all_cards)
    invalid_total = sum(card["review_status"] == "invalid" for card in all_cards)
    total_bootstrap = sum(len(cards) for cards in found["Bootstrap"].values())
    lines = [DASH_BEGIN, "", "## Bootstrap 初次调研", "",
             "<!-- LITERATURE-RADAR-LIVE:BEGIN -->", ""]
    for topic in topics:
        cards = found["Bootstrap"].get(topic, [])
        name = cards[0]["topic_name"] if cards else topic
        seed_count = sum(card["attention"] == "BOOTSTRAP_SEED" for card in cards)
        legacy_count = len(cards) - seed_count
        decision_counts = Counter(card["decision"] for card in cards)
        pending_count = sum(card["review_status"] == "pending" for card in cards)
        invalid_count = sum(card["review_status"] == "invalid" for card in cards)
        reviewed_count = sum(card["review_status"] == "reviewed" for card in cards)
        index = f"Bootstrap/{topic}/index"
        link = f"{prefix}/{index}" if prefix else index
        lines.append(f"- [[{link}|{name}]]：{len(cards)} 篇（初次调研种子 {seed_count}；保留的旧版卡片 {legacy_count}）")
        lines.append(f"  - 待评价 {pending_count}；已评价 {reviewed_count}；很重要 {decision_counts['must_read']}；有用 {decision_counts['keep']}；一般 {decision_counts['later']}；没用 {decision_counts['discard']}；无效反馈 {invalid_count}。")
        pending_sync = sum(card["review_status"] == "reviewed" and card["sync_status"] not in ("synced", "failed") for card in cards)
        failed_sync = sum(card["sync_status"] == "failed" for card in cards)
        imported = 0
        if config is not None and (config.parent / topic / "topic.json").is_file():
            try:
                from literature_radar.feedback.batches import batch_progress
                progress = batch_progress(config.parent / topic)
                imported = progress['valid_feedback']
                lines.append(f"  - 待同步 {pending_sync}；同步失败 {failed_sync}；正式导入反馈 {imported}。")
                processed = progress.get('processed_feedback', progress['completed_batches'] * 30)
                waiting = progress.get('unprocessed_feedback', max(0, progress['valid_feedback'] - processed))
                lines.append(f"  - Attention Memory 提案：卡片评价进度 {reviewed_count}/{len(cards)}；有效正式反馈 {progress['valid_feedback']}（累计）；已完成 {progress['completed_batches']} 批；待处理 {progress['pending_batches']} 批；已处理反馈 {processed}；待处理反馈 {waiting}；下一批提案阈值 {progress['next_threshold']} 条。")
                from literature_radar.attention.calibration import training_status
                trained = training_status(config.parent / topic)
                lines.append(f"  - 原 AI_OS 模型校准：{trained.get('last_training_status', trained.get('status', 'NEVER_TRAINED'))}；已激活版本 {trained.get('active_version') or '无'}；有效训练反馈 {trained.get('trainable_relevance_count', 0)}；待校准 {trained.get('pending_trainable_feedback', 0)}／30 条。")
            except (OSError, ValueError):
                lines.append("  - Attention 校准状态暂不可读取。")
        else:
            lines.append(f"  - 待同步 {pending_sync}；同步失败 {failed_sync}；正式导入反馈 {imported}。")
    lines.extend([f"- **总计：{total_bootstrap} 篇**", ""])
    lines.extend(["<!-- LITERATURE-RADAR-LIVE:END -->", ""])
    views = sorted((root / "System/views").glob("*.base")) if (root / "System/views").is_dir() else []
    if views:
        lines.extend(["", "## 阅读状态视图", ""])
        for view in views:
            if _within(root, view) and not view.is_symlink():
                path = f"{prefix}/System/views/{view.name}" if prefix else f"System/views/{view.name}"
                lines.extend([f"![[{path}]]", ""])
    lines.extend(["## Weekly 后续追踪", ""])
    for topic in topics:
        cards = found["Weekly"].get(topic, [])
        name = (found["Bootstrap"].get(topic) or cards or [{"topic_name": topic}])[0]["topic_name"]
        index = f"Weekly/{topic}/index"
        link = f"{prefix}/{index}" if prefix else index
        lines.append(f"- [[{link}|{name}]]：{len(cards)} 篇" if cards else
                     f"- [[{link}|{name}]]：尚未启用（0 篇）")
    lines.extend(["", "## 阅读进度", "", "<!-- LITERATURE-RADAR-SUMMARY:BEGIN -->", "",
                  f"- 待阅读：{pending_total} 篇",
                  f"- 已阅读 / 已反馈：{reviewed} 篇",
                  f"- 反馈异常：{invalid_total} 篇",
                  f"- 必读：{decisions['must_read']} 篇",
                  f"- 保留：{decisions['keep']} 篇",
                  f"- 稍后：{decisions['later']} 篇",
                  f"- 不相关：{decisions['discard']} 篇", "",
                  "反馈入库后才计入正式导入和 Attention 进度。", "",
                  "<!-- LITERATURE-RADAR-SUMMARY:END -->", "",
                  DASH_END, ""])
    return "\n".join(lines).rstrip() + "\n"


def _topic_index(root: Path, section: str, topic: str, cards: list[dict], *, prefix: str) -> str:
    name = cards[0]["topic_name"] if cards else topic
    lines = [INDEX_BEGIN, "", f"## {name} · {section}", "", f"共 {len(cards)} 篇。", ""]
    groups = (("初次调研种子", [card for card in cards if card["attention"] == "BOOTSTRAP_SEED"]),
              ("旧版卡片（保留原目录）", [card for card in cards if card["attention"] != "BOOTSTRAP_SEED"])) if section == "Bootstrap" else tuple(
                  (level, [card for card in cards if card["attention"] == level])
                  for level in ("HIGH", "MEDIUM", "LOW"))
    for label, matching in groups:
        if section == "Bootstrap" and not matching:
            continue
        lines.extend([f"### {label}（{len(matching)}）", ""])
        for card in sorted(matching, key=lambda row: row["title"].casefold()):
            link = f"{prefix}/{card['relative'][:-3]}" if prefix else card["relative"][:-3]
            label = str(card["title"]).replace("|", " ").replace("[", "(").replace("]", ")")
            state = card["decision"] or "PENDING"
            lines.append(f"- [[{link}|{label}]] · {state}")
        lines.append("")
    lines.extend([INDEX_END, ""])
    return "\n".join(lines).rstrip() + "\n"


def _update_topic_indexes(root: Path, manifest: dict, config: Path, *, prefix: str) -> None:
    found = _inventory(root, manifest)
    for section, groups in found.items():
        for topic, cards in groups.items():
            index = root / section / topic / "index.md"
            if index.is_symlink() or not _within(root, index):
                raise ObsidianError("Topic index escapes delivery root")
            managed = _topic_index(root, section, topic, cards, prefix=prefix)
            if index.exists():
                old = index.read_text(encoding="utf-8")
                if old.count(INDEX_BEGIN) != 1 or old.count(INDEX_END) != 1:
                    raise ObsidianError("Existing Topic index is not managed by Literature Radar")
                start = old.index(INDEX_BEGIN)
                end = old.index(INDEX_END) + len(INDEX_END)
                new = old[:start] + managed.rstrip("\n") + old[end:]
                if new != old:
                    backup = config.parent / "obsidian-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") / section / topic / "index.md"
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(index, backup)
                    _atomic_bytes(index, new.encode("utf-8"))
            else:
                _atomic_bytes(index, (f"# {topic}\n\n" + managed).encode("utf-8"))


def _dashboard_update(existing: str, managed: str) -> str:
    if DASH_BEGIN in existing or DASH_END in existing:
        if existing.count(DASH_BEGIN) != 1 or existing.count(DASH_END) != 1:
            raise ObsidianError("Dashboard managed section markers conflict")
        start = existing.index(DASH_BEGIN)
        end = existing.index(DASH_END) + len(DASH_END)
        if end <= start:
            raise ObsidianError("Dashboard managed section markers are reversed")
        return existing[:start] + managed.rstrip("\n") + existing[end:]
    if not existing:
        return "# Literature Radar\n\n" + managed
    return existing.rstrip() + "\n\n" + managed


def _deliver_cards_locked(cards: list[Path], *, section: str, config: Path | None = None,
                          metadata_by_key: dict[str, dict] | None = None) -> dict:
    if section not in ("Bootstrap", "Weekly"):
        raise ObsidianError("section must be Bootstrap or Weekly")
    config = ensure_private_path(config or binding_file())
    vault = bound_vault(config=config)
    if vault is None:
        return {"status": "OBSIDIAN_NOT_CONNECTED", "new": 0, "updated": 0,
                "failed": len(cards), "next": "literature-radar obsidian setup"}
    binding = _read_binding(config=config)
    root = Path(binding["delivery_root"])
    if not _writable(vault) or not _writable(root):
        raise ObsidianError("Vault or Literature Radar directory is read-only")
    manifest = _load_manifest(config)
    created = updated = unchanged = failed = 0
    errors = []
    for source in cards:
        try:
            source = Path(source)
            if source.is_symlink() or not source.is_file():
                raise ObsidianError("Source card missing or symlinked")
            raw = source.read_text(encoding="utf-8")
            meta = frontmatter(raw)
            topic = meta.get("topic_id", "")
            key = meta.get("paper_key", "")
            metadata = (metadata_by_key or {}).get(key)
            level = meta.get("current_attention", "")
            if (meta.get("type") != "literature-review-card" or meta.get("topic") not in {topic, meta.get("topic_display_name")}
                    or not _safe_topic(topic) or not key.startswith(topic + "|")):
                raise ObsidianError("Invalid Topic or card identity")
            allowed = {"BOOTSTRAP_SEED", "HIGH", "MEDIUM", "LOW"} if section == "Bootstrap" else {"HIGH", "MEDIUM", "LOW"}
            if (level not in allowed or source.parent.name not in allowed or
                    source.parent.name not in {meta.get("first_attention"), level} or
                    source.parent.parent.name != topic):
                raise ObsidianError("Source directory disagrees with card Topic or Attention")
            date_part = source.parent.parent.parent.name
            if len(date_part) != 10 or date.fromisoformat(date_part).isoformat() != date_part:
                raise ObsidianError("Source archive directory is not a delivery date")
            delivery_day = str(meta.get("delivery_date") or date_part)
            if len(delivery_day) != 10 or date.fromisoformat(delivery_day).isoformat() != delivery_day:
                raise ObsidianError("Card delivery date is invalid")
            item_id = f"{section}|{key}"
            title = next((line[2:].strip() for line in raw.splitlines() if line.startswith("# ")), source.stem)
            relative = _title_relative(manifest, item_id, section, topic, level, title,
                                       delivery_day=delivery_day if section == "Weekly" else None)
            prior = manifest["cards"].get(item_id)
            if prior:
                relative = prior["relative_path"]
                location = _managed_card_location(relative)
                if location is None or location[:2] != (section, topic):
                    raise ObsidianError("Delivery manifest card path conflicts with Topic namespace")
            target = root / relative
            if not _within(root, target) or target.is_symlink():
                raise ObsidianError("Target path escapes Literature Radar namespace")
            if target.exists() and prior is None:
                raise ObsidianError("Target card exists without a delivery manifest record; refusing overwrite")
            if target.exists():
                existing = target.read_text(encoding="utf-8")
                content = _merge_card(_enrich_card(raw, metadata), existing)
                if content == existing:
                    unchanged += 1
                    continue
                backup = config.parent / "obsidian-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                updated += 1
            else:
                content = _enrich_card(raw, metadata)
                created += 1
            _atomic_bytes(target, content.encode("utf-8"))
            manifest["cards"][item_id] = {"relative_path": relative, "topic_id": topic,
                                           "paper_key": key, "title": title,
                                           "journal": metadata.get("journal") if metadata else None,
                                           "score": metadata.get("score") if metadata else None,
                                           "sha256": _hash(content.encode("utf-8"))}
            _write_json(_manifest_path(config), manifest)
        except (OSError, ValueError, UnicodeError) as exc:
            failed += 1
            errors.append({"source": str(source), "error": str(exc)})
    try:
        dashboard = root / "dashboard.md"
        if not _within(root, dashboard) or dashboard.is_symlink():
            raise ObsidianError("Dashboard path escapes Literature Radar namespace")
        prefix = "" if root == vault else NAMESPACE
        _update_topic_indexes(root, manifest, config, prefix=prefix)
        generated = _dashboard(root, manifest, prefix=prefix, config=config)
        if dashboard.exists():
            existing = dashboard.read_text(encoding="utf-8")
            updated_dashboard = _dashboard_update(existing, generated)
            if existing != updated_dashboard:
                backup = config.parent / "obsidian-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") / "dashboard.md"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dashboard, backup)
                _atomic_bytes(dashboard, updated_dashboard.encode("utf-8"))
        else:
            _atomic_bytes(dashboard, _dashboard_update("", generated).encode("utf-8"))
    except (OSError, ValueError) as exc:
        failed += 1
        errors.append({"source": "dashboard", "error": str(exc)})
    manifest["last_delivery"] = datetime.now(timezone.utc).isoformat()
    manifest["last_status"] = "SUCCESS" if failed == 0 else "PARTIAL"
    _write_json(_manifest_path(config), manifest)
    counts: dict[str, int] = {}
    for item in manifest["cards"].values():
        if item["relative_path"].startswith(section + "/") and ".." not in Path(item["relative_path"]).parts:
            counts[item["topic_id"]] = counts.get(item["topic_id"], 0) + 1
    return {"status": manifest["last_status"], "message": f"Obsidian Delivery: {manifest['last_status']}", "vault": str(vault),
            "review_location": str(root / section), "topics": counts,
            "new": created, "updated": updated, "unchanged": unchanged,
            "failed": failed, "errors": errors,
            "open_command": "literature-radar obsidian open"}


def deliver_cards(cards: list[Path], *, section: str, config: Path | None = None,
                  metadata_by_key: dict[str, dict] | None = None) -> dict:
    """Serialize the shared Vault manifest, indexes, and Dashboard across workers."""
    config = ensure_private_path(config or binding_file())
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = config.parent / ".obsidian-delivery.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _deliver_cards_locked(cards, section=section, config=config,
                                     metadata_by_key=metadata_by_key)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def deliver_selected(cards: list[Path], *, section: str, config: Path | None = None) -> dict:
    """Deliver one run's card set through the ordinary manifest and merge path."""
    try:
        return deliver_cards(cards, section=section, config=config)
    except (OSError, ValueError) as exc:
        return {"status": "FAILED", "new": 0, "updated": 0, "failed": len(cards),
                "error": str(exc), "next": "literature-radar obsidian doctor"}


def deliver_generated(card_root: Path, *, section: str, config: Path | None = None) -> dict:
    cards = sorted(Path(card_root).rglob("*.md")) if Path(card_root).is_dir() else []
    return deliver_selected(cards, section=section, config=config)


def rename_delivered_cards(*, config: Path | None = None) -> dict:
    """Rename managed cards to titles in place, preserving all bytes and user notes."""
    config = ensure_private_path(config or binding_file())
    binding = _read_binding(config=config)
    if binding is None:
        raise ObsidianError("OBSIDIAN_NOT_CONNECTED")
    root = Path(binding["delivery_root"])
    manifest = _load_manifest(config)
    renamed = []
    for item_id, item in list(manifest["cards"].items()):
        old = root / item["relative_path"]
        if old.is_symlink() or not old.is_file() or not _within(root, old):
            raise ObsidianError("Managed card is missing or unsafe")
        meta = frontmatter(old.read_text(encoding="utf-8"))
        location = _managed_card_location(item["relative_path"])
        if location is None:
            raise ObsidianError("Managed card path is invalid")
        section, topic, level, delivery_day = location
        if (meta.get("topic_id") != topic or meta.get("paper_key") != item.get("paper_key")
                or level not in {meta.get("first_attention"), meta.get("current_attention")}):
            raise ObsidianError("Managed card identity differs from manifest")
        title = item.get("title") or next((line[2:].strip() for line in old.read_text(encoding="utf-8").splitlines()
                                           if line.startswith("# ")), "")
        relative = _title_relative(manifest, item_id, section, topic, level, title,
                                   delivery_day=delivery_day)
        if relative == item["relative_path"]:
            continue
        new = root / relative
        if new.exists() or new.is_symlink() or not _within(root, new):
            raise ObsidianError("Title filename conflicts with an existing file")
        backup = config.parent / "obsidian-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") / item["relative_path"]
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, backup)
        digest = _hash(old.read_bytes())
        old.rename(new)
        if _hash(new.read_bytes()) != digest:
            raise ObsidianError("Renamed card checksum changed")
        item["relative_path"] = relative
        _write_json(_manifest_path(config), manifest)
        renamed.append({"from": str(old), "to": str(new), "sha256": digest})
    deliver_cards([], section="Bootstrap", config=config)
    return {"renamed": len(renamed), "items": renamed}


def migrate_staged(source_dirs: list[Path], *, config: Path | None = None,
                   expected_per_topic: int = 30, confirmed: bool = False,
                   database: Path | None = None) -> dict:
    """Back up complete Topic reports, copy cards, and verify identities without deleting sources."""
    if not confirmed:
        raise ObsidianError("Explicit migration confirmation is required")
    if expected_per_topic < 1 or not source_dirs:
        raise ObsidianError("Migration requires Topic report directories and a positive expected count")
    config = ensure_private_path(config or binding_file())
    binding = _read_binding(config=config)
    if binding is None:
        raise ObsidianError("OBSIDIAN_NOT_CONNECTED; run literature-radar obsidian setup")
    root = Path(binding["delivery_root"])
    sources: dict[str, Path] = {}
    all_cards: list[Path] = []
    originals: dict[Path, str] = {}
    keys = set()
    metadata_by_key: dict[str, dict] = {}
    paper_topics: dict[str, str] = {}
    for requested in source_dirs:
        source = Path(requested).expanduser().resolve()
        if config.parent.resolve().is_relative_to(source):
            raise ObsidianError("Private migration backup directory cannot be inside a source report")
        topic = source.name
        cards_root = source / "cards"
        if not _safe_topic(topic) or topic in sources or not cards_root.is_dir() or cards_root.is_symlink():
            raise ObsidianError("Migration source must be a distinct Topic report directory with cards/")
        files = sorted(path for path in source.rglob("*") if path.is_file() or path.is_symlink())
        if any(path.is_symlink() for path in files):
            raise ObsidianError("Migration source contains a symlink")
        cards = sorted(cards_root.rglob("*.md"))
        if len(cards) != expected_per_topic:
            raise ObsidianError(f"Topic {topic} has {len(cards)} cards; expected {expected_per_topic}")
        for card in cards:
            meta = frontmatter(card.read_text(encoding="utf-8"))
            if meta.get("topic_id") != topic or meta.get("topic") not in {topic, meta.get("topic_display_name")} or "## 我的评价" not in card.read_text(encoding="utf-8"):
                raise ObsidianError("Migration card Topic or feedback region is invalid")
            key = meta.get("paper_key")
            if not key or key in keys:
                raise ObsidianError("Migration contains a duplicate or missing paper_key")
            keys.add(key)
            paper_topics[meta["paper_id"]] = topic
        report_path = source / "bootstrap-report.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            for row in report.get("cards", report.get("papers", [])):
                if not isinstance(row, dict) or not row.get("paper_id"):
                    continue
                key = row.get("paper_key") or card_key(topic, str(row["paper_id"]), row.get("doi"))
                if key in keys:
                    metadata_by_key[key] = {"journal": row.get("journal"),
                                            "score": row.get("scientific_score", row.get("score"))}
        originals.update({path: _hash(path.read_bytes()) for path in files})
        sources[topic] = source
        all_cards.extend(cards)
    if database is not None:
        database = ensure_private_path(database)
        if not database.is_file():
            raise ObsidianError("Metadata database does not exist")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            for paper_id, topic in paper_topics.items():
                row = connection.execute(
                    "SELECT p.payload FROM papers_v2 p JOIN topic_paper_state t ON p.paper_id=t.paper_id "
                    "WHERE p.paper_id=? AND t.topic_id=?", (paper_id, topic)).fetchone()
                if row is None:
                    raise ObsidianError("Metadata database lacks a staged paper identity")
                payload = json.loads(row[0])
                doi = str(payload.get("doi") or payload.get("identifiers", {}).get("doi") or "")
                key = card_key(topic, paper_id, doi)
                if key not in keys:
                    raise ObsidianError("Metadata database DOI conflicts with staged card")
                metadata_by_key.setdefault(key, {})["journal"] = payload.get("journal")
    backup = config.parent / "obsidian-migration-backups" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    for topic, source in sources.items():
        shutil.copytree(source, backup / topic)
        for path in source.rglob("*"):
            if path.is_file() and _hash(path.read_bytes()) != _hash((backup / topic / path.relative_to(source)).read_bytes()):
                raise ObsidianError("Migration source backup checksum mismatch")
    delivered = deliver_cards(all_cards, section="Bootstrap", config=config,
                              metadata_by_key=metadata_by_key)
    errors = list(delivered.get("errors", []))
    manifest = _load_manifest(config)
    for card in all_cards:
        meta = frontmatter(card.read_text(encoding="utf-8"))
        item = manifest["cards"].get("Bootstrap|" + meta["paper_key"])
        target = root / item["relative_path"] if item else None
        if target is None or not _within(root, target) or not target.is_file():
            errors.append({"source": str(card), "error": "Delivered card missing"})
            continue
        target_text = target.read_text(encoding="utf-8")
        target_meta = frontmatter(target_text)
        if any(target_meta.get(field) != meta.get(field) for field in ("topic_id", "paper_key", "paper_id", "doi", "current_attention")):
            errors.append({"source": str(card), "error": "Delivered card identity differs"})
        if END not in target_text or target_text[:target_text.index(END) + len(END)] != card.read_text(encoding="utf-8").split(END)[0] + END:
            errors.append({"source": str(card), "error": "Delivered analysis content differs"})
        if "## 我的评价" not in target_text:
            errors.append({"source": str(card), "error": "Delivered feedback region missing"})
    if not (root / "dashboard.md").is_file() or DASH_BEGIN not in (root / "dashboard.md").read_text(encoding="utf-8"):
        errors.append({"source": "dashboard", "error": "Dashboard is missing"})
    if any(not path.is_file() or _hash(path.read_bytes()) != digest for path, digest in originals.items()):
        errors.append({"source": "staging", "error": "Original staged material changed"})
    result = {"status": "SUCCESS" if not errors else "PARTIAL", "backup_path": str(backup),
              "vault_root": binding["vault_root"], "delivery_root": str(root),
              "topics": {topic: expected_per_topic for topic in sources},
              "source_preserved": not any(item["source"] == "staging" for item in errors),
              "delivery": delivered, "errors": errors}
    _write_json(backup / "migration-result.json", result)
    return result


def status(*, config: Path | None = None) -> dict:
    config = config or binding_file()
    health = doctor(config=config)
    if health["status"] != "PASS":
        return health
    try:
        vault = bound_vault(config=config)
    except ObsidianError:
        return health
    binding = _read_binding(config=config)
    root = Path(binding["delivery_root"])
    manifest = _load_manifest(config)
    topics = sorted({item["topic_id"] for item in manifest["cards"].values()} |
                    {p.name for section in ("Bootstrap", "Weekly")
                     for p in (root / section).iterdir() if p.is_dir() and _safe_topic(p.name)
                     and (section == "Bootstrap" or (p / "index.md").is_file())})
    found = _inventory(root, manifest)
    counts = {section: {topic: len(found[section].get(topic, [])) for topic in topics}
              for section in ("Bootstrap", "Weekly")}
    return {"status": health["status"], "vault_name": vault.name, "vault": str(vault),
            "vault_mode": binding["vault_mode"], "vault_identifier": binding["vault_identifier"],
            "delivery_root": str(root), "literature_root": str(root), "topic_directories": {
                topic: {section: str(root / section / topic) for section in ("Bootstrap", "Weekly")}
                for topic in topics}, "counts": counts, "last_delivery": manifest["last_delivery"],
            "last_delivery_status": manifest["last_status"], "issues": health["issues"]}


def open_location(*, config: Path | None = None, launcher=None) -> dict:
    vault = bound_vault(config=config)
    if vault is None:
        raise ObsidianError("OBSIDIAN_NOT_CONNECTED; run literature-radar obsidian setup")
    binding = _read_binding(config=config)
    root = Path(binding["delivery_root"])
    relative_dashboard = "dashboard.md" if root == vault else f"{NAMESPACE}/dashboard.md"
    uri = "obsidian://open?vault=" + quote(vault.name, safe="") + "&file=" + quote(relative_dashboard, safe="")
    launch = launcher or (lambda args: subprocess.run(args, check=True, stdout=subprocess.DEVNULL,
                                                       stderr=subprocess.DEVNULL))
    if sys.platform == "win32":
        import os as _os
        try:
            _os.startfile(uri)  # type: ignore[attr-defined]
            return {"status": "OPENED", "uri": uri, "path": str(root)}
        except OSError:
            _os.startfile(str(root))  # type: ignore[attr-defined]
            return {"status": "OPENED_DIRECTORY", "uri": uri, "path": str(root)}
    command = ["open" if sys.platform == "darwin" else "xdg-open", uri]
    try:
        launch(command)
        return {"status": "OPENED", "uri": uri, "path": str(root)}
    except (OSError, subprocess.CalledProcessError):
        launch([command[0], str(root)])
        return {"status": "OPENED_DIRECTORY", "uri": uri, "path": str(root)}
