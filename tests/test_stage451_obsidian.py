"""Vault discovery and safe delivery using only fictional Topics and temporary Vaults."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path

from literature_radar.delivery.cards import write_card
from literature_radar.delivery.obsidian import (ObsidianError, bind_vault, bound_vault,
    deliver_cards, discover_vaults, doctor, open_location, registry_file,
    select_existing_vault, status)


class Obsidian451Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vaults = [self.root / f"vault-{index}" for index in range(3)]
        for vault in self.vaults:
            (vault / ".obsidian").mkdir(parents=True)
        self.config = self.root / "private" / "obsidian.json"
        self.registry = self.root / "app" / "obsidian.json"
        self.registry.parent.mkdir()

    def _register(self, vaults):
        self.registry.write_text(json.dumps({"vaults": {str(i): {"path": str(v)}
                                                       for i, v in enumerate(vaults)}}))

    def _cards(self, count=30, topic="fictional-alpha"):
        root = self.root / f"staging-{topic}"
        paths = []
        for index in range(count):
            path = write_card(root, {"paper_id": f"{topic}-{index}",
                                      "title": f"Fictional paper {index}",
                                      "abstract": "Synthetic abstract", "why_recommended": "Synthetic reason"},
                              topic_id=topic, topic_name=topic,
                              run_id="synthetic", seen_on="2026-11-05", attention="HIGH")
            paths.append(path)
        return paths

    def test_duplicate_titles_use_short_identity_suffix_and_keep_notes(self):
        root = self.root / "staging"
        rows = [{"paper_id": "paper-one", "title": "Same fictional title"},
                {"paper_id": "paper-two", "title": "Same fictional title"}]
        cards = [write_card(root, row, topic_id="fictional-alpha",
                            topic_name="Fictional Alpha", run_id="synthetic",
                            seen_on="2026-11-05", attention="HIGH") for row in rows]
        self.assertEqual(cards[0].name, "Same fictional title.md")
        self.assertRegex(cards[1].name, r"^Same fictional title \([a-f0-9]{8}\)\.md$")
        cards[0].write_text(cards[0].read_text() + "My private note\n")
        updated = write_card(root, rows[0], topic_id="fictional-alpha",
                             topic_name="Fictional Alpha", run_id="synthetic-next",
                             seen_on="2026-11-06", attention="HIGH")
        self.assertEqual(updated, cards[0])
        self.assertIn("My private note", updated.read_text())
        bind_vault(self.vaults[0], config=self.config)
        result = deliver_cards(cards, section="Bootstrap", config=self.config)
        self.assertEqual((result["new"], result["failed"]), (2, 0))
        delivered = sorted((self.vaults[0] / "Literature-Radar/Bootstrap/fictional-alpha/HIGH").glob("*.md"))
        self.assertEqual(len(delivered), 2)
        self.assertTrue(any(re.fullmatch(r"Same fictional title \([a-f0-9]{8}\)\.md", p.name)
                            for p in delivered))

    def test_registry_single_multiple_none_and_platform_locations(self):
        self._register([self.vaults[0]])
        self.assertEqual(discover_vaults(registry=self.registry), [self.vaults[0]])
        self._register(self.vaults)
        self.assertEqual(discover_vaults(registry=self.registry), self.vaults)
        (self.vaults[1] / ".obsidian").rmdir()
        self.assertEqual(discover_vaults(registry=self.registry), [self.vaults[0], self.vaults[2]])
        self.assertEqual(discover_vaults(registry=self.root / "missing"), [])
        flatpak = self.root / ".var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json"
        flatpak.parent.mkdir(parents=True)
        flatpak.write_text(json.dumps({"vaults": {"f": {"path": str(self.vaults[2])}}}))
        self.assertIn(self.vaults[2], discover_vaults(home=self.root, platform="linux", environ={}))
        self.assertEqual(registry_file(home=self.root, platform="linux", environ={}), self.root / ".config/obsidian/obsidian.json")
        self.assertEqual(registry_file(home=self.root, platform="darwin", environ={}), self.root / "Library/Application Support/obsidian/obsidian.json")
        self.assertEqual(registry_file(home=self.root, platform="win32", environ={}), self.root / "AppData/Roaming/obsidian/obsidian.json")
        xdg = self.root / "xdg-config"
        self.assertEqual(registry_file(home=self.root, platform="linux", environ={"XDG_CONFIG_HOME": str(xdg)}),
                         xdg / "obsidian/obsidian.json")

    def test_binding_permissions_move_and_path_safety(self):
        bind_vault(self.vaults[0], config=self.config)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(bound_vault(config=self.config), self.vaults[0])
        self.assertEqual(doctor(config=self.config)["status"], "PASS")
        moved = self.root / "moved"
        self.vaults[0].rename(moved)
        with self.assertRaisesRegex(ObsidianError, "moved or removed"):
            bound_vault(config=self.config)
        self.assertEqual(doctor(config=self.config)["status"], "FAIL")
        select_existing_vault(moved, config=self.config, rebind=True)
        self.assertEqual(bound_vault(config=self.config), moved)
        (moved / "Literature-Radar").rename(moved / "owned-backup")
        (moved / "Literature-Radar").symlink_to(self.vaults[2])
        with self.assertRaisesRegex(ObsidianError, "namespace"):
            deliver_cards(self._cards(1), section="Bootstrap", config=self.config)

    def test_60_cards_two_topics_idempotence_and_notes(self):
        bind_vault(self.vaults[0], config=self.config)
        legacy_dashboard = self.vaults[0] / "Literature/dashboard.md"
        legacy_dashboard.parent.mkdir()
        legacy_dashboard.write_text("# Existing reading dashboard\n\nPersonal navigation.\n")
        cards = self._cards(30, "fictional-alpha") + self._cards(30, "fictional-beta")
        first = deliver_cards(cards, section="Bootstrap", config=self.config)
        self.assertEqual((first["status"], first["new"], first["failed"]), ("SUCCESS", 60, 0))
        self.assertEqual(first["topics"], {"fictional-alpha": 30, "fictional-beta": 30})
        self.assertEqual(deliver_cards(cards, section="Bootstrap", config=self.config)["new"], 0)
        info = status(config=self.config)
        self.assertEqual(info["counts"]["Bootstrap"], first["topics"])
        self.assertEqual(legacy_dashboard.read_text(), "# Existing reading dashboard\n\nPersonal navigation.\n")
        managed_dashboard = self.vaults[0] / "Literature-Radar/dashboard.md"
        self.assertEqual(managed_dashboard.read_text().count("LITERATURE-RADAR-DELIVERY:BEGIN"), 1)
        self.assertIn("Fictional paper 0", (self.vaults[0] / "Literature-Radar/Bootstrap/fictional-alpha/index.md").read_text())
        target = next((self.vaults[0] / "Literature-Radar/Bootstrap/fictional-alpha/HIGH").glob("*.md"))
        target.write_text(target.read_text() + "My private note\n")
        source = cards[0]
        source.write_text(source.read_text().replace("Synthetic abstract", "Updated abstract"))
        second = deliver_cards([source], section="Bootstrap", config=self.config)
        self.assertEqual(second["updated"], 1)
        self.assertIn("My private note", target.read_text())
        self.assertTrue(list((self.config.parent / "obsidian-backups").rglob("*.md")))
        self.assertEqual(stat.S_IMODE((self.config.parent / "obsidian-delivery-manifest.json").stat().st_mode), 0o600)

    def test_conflict_readonly_uri_fallback_and_unconnected(self):
        self.assertEqual(deliver_cards(self._cards(1), section="Weekly", config=self.config)["status"],
                         "OBSIDIAN_NOT_CONNECTED")
        bind_vault(self.vaults[0], config=self.config)
        card = self._cards(1)[0]
        target = self.vaults[0] / "Literature-Radar/Bootstrap/fictional-alpha/HIGH/Fictional paper 0.md"
        target.parent.mkdir(parents=True)
        target.write_text("Personal note; keep untouched")
        result = deliver_cards([card], section="Bootstrap", config=self.config)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(target.read_text(), "Personal note; keep untouched")
        calls = []
        def launcher(args):
            calls.append(args)
            if args[1].startswith("obsidian://"):
                raise OSError("URI unavailable")
        opened = open_location(config=self.config, launcher=launcher)
        self.assertEqual(opened["status"], "OPENED_DIRECTORY")
        self.assertTrue(calls[0][1].startswith("obsidian://open?"))
        self.assertIn("Literature-Radar%2Fdashboard.md", calls[0][1])
        self.assertEqual(calls[1][1], str(self.vaults[0] / "Literature-Radar"))
        # Mode bits are checked directly so this is meaningful even under root.
        os.chmod(self.vaults[0], 0o500)
        self.assertEqual(doctor(config=self.config)["status"], "FAIL")
