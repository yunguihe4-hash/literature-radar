"""Safety checks for the distributable Bridge installer."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[1] / "deploy/obsidian-bridge/install.py"
spec = importlib.util.spec_from_file_location("bridge_installer", INSTALLER)
bridge_installer = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bridge_installer)


class BridgeInstallTest(unittest.TestCase):
    def test_install_upgrade_and_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            plugins = vault / ".obsidian/plugins"
            unrelated = plugins / "unrelated-plugin"
            unrelated.mkdir(parents=True)
            (unrelated / "main.js").write_text("keep", encoding="utf-8")
            first = bridge_installer.install(vault)
            self.assertEqual(first["status"], "INSTALLED")
            target = plugins / "literature-radar-bridge"
            (target / "data.json").write_text('{"user":"setting"}', encoding="utf-8")
            self.assertEqual(bridge_installer.install(vault)["status"], "ALREADY_INSTALLED")
            (target / "main.js").write_text("old", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "--upgrade"):
                bridge_installer.install(vault)
            result = bridge_installer.install(vault, upgrade=True)
            self.assertEqual(result["status"], "UPGRADED")
            self.assertEqual((target / "data.json").read_text(encoding="utf-8"), '{"user":"setting"}')
            self.assertEqual((Path(result["backup"]) / "main.js").read_text(encoding="utf-8"), "old")
            self.assertEqual((unrelated / "main.js").read_text(encoding="utf-8"), "keep")

    def test_foreign_plugin_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            target = vault / ".obsidian/plugins/literature-radar-bridge"
            target.mkdir(parents=True)
            (target / "manifest.json").write_text(json.dumps({"id": "someone-else"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different plugin"):
                bridge_installer.install(vault, upgrade=True)


if __name__ == "__main__":
    unittest.main()
