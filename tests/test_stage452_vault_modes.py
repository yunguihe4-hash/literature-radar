"""Explicit Vault choices and isolated namespace regression with synthetic papers."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literature_radar.cli import _interactive_obsidian_setup, main as cli_main
from literature_radar.delivery.cards import frontmatter, write_card
from literature_radar.delivery.review_ui import set_properties
from literature_radar.delivery.obsidian import (ObsidianError, bind_vault, binding_file,
    bound_vault, create_dedicated_vault, deliver_cards, doctor, migrate_staged, preview_new_vault,
    select_existing_vault, status)


class Stage452VaultModes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.config = self.base / "private" / "obsidian.json"
        self.parent = self.base / "vault-parent"
        self.parent.mkdir()

    def _existing(self, name="existing"):
        vault = self.base / name
        (vault / ".obsidian").mkdir(parents=True)
        return vault

    def _cards(self, topic: str, count: int = 30):
        root = self.base / "staged" / topic
        cards = []
        for number in range(count):
            cards.append(write_card(root, {
                "paper_id": f"{topic}-{number}", "doi": f"10.0000/{topic}-{number}",
                "title": f"Synthetic {topic} paper {number}",
                "abstract": f"Abstract {number}", "why_recommended": f"Reason {number}"},
                topic_id=topic, topic_name=topic, run_id="synthetic-run",
                seen_on="2026-11-05", attention=("HIGH", "MEDIUM", "LOW")[number % 3]))
        return cards

    def test_create_dedicated_vault_and_repeat_binding(self):
        target = preview_new_vault(self.parent, registered=[])
        self.assertEqual(target, self.parent / "Literature-Radar")
        self.assertFalse(target.exists())
        result = create_dedicated_vault(self.parent, config=self.config,
                    topic_ids=("fictional-alpha", "fictional-beta"), registered=[])
        self.assertEqual(result["vault_mode"], "dedicated")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["vault_root"], str(target))
        self.assertEqual(result["delivery_root"], str(target))
        self.assertTrue((target / ".obsidian").is_dir())
        for section in ("Bootstrap", "Weekly"):
            for topic in ("fictional-alpha", "fictional-beta"):
                self.assertTrue((target / section / topic).is_dir())
        for relative in ("System/delivery", "System/feedback", "dashboard.md"):
            self.assertTrue((target / relative).exists())
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(doctor(config=self.config)["status"], "PASS")
        self.assertEqual(create_dedicated_vault(self.parent, config=self.config,
                         registered=[])["status"], "ALREADY_BOUND")
        recovered = create_dedicated_vault(self.parent, config=self.base / "recovery" / "obsidian.json",
                                           registered=[target])
        self.assertEqual(recovered["vault_identifier"], result["vault_identifier"])
        self.assertEqual(recovered["delivery_root"], str(target))

    def test_nonempty_target_project_overlap_and_legacy_vault_isolation(self):
        target = self.parent / "Literature-Radar"
        target.mkdir()
        (target / "user-note.md").write_text("private")
        with self.assertRaisesRegex(ObsidianError, "not empty"):
            create_dedicated_vault(self.parent, config=self.config, registered=[])
        self.assertEqual((target / "user-note.md").read_text(), "private")
        legacy = self._existing("AI" + "_" + "OS")
        (legacy / "Literature").mkdir()
        (legacy / "Literature" / "dashboard.md").write_text("legacy dashboard")
        with self.assertRaises(ObsidianError):
            preview_new_vault(legacy, "Nested", registered=[legacy])
        other = self.base / "source-project"
        (other / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(ObsidianError, "project"):
            preview_new_vault(other, "Radar", registered=[])
        self.assertEqual((legacy / "Literature" / "dashboard.md").read_text(), "legacy dashboard")
        self.assertFalse(self.config.exists())

    def test_existing_vault_namespace_only_and_conflicts(self):
        vault = self._existing()
        for name in ("Literature", "Knowledge", "Projects", "System"):
            (vault / name).mkdir()
            (vault / name / "notes.md").write_text(f"untouched {name}")
        before = {name: hashlib.sha256((vault / name / "notes.md").read_bytes()).hexdigest()
                  for name in ("Literature", "Knowledge", "Projects", "System")}
        result = select_existing_vault(vault, config=self.config, topic_ids=("fictional-alpha",))
        self.assertEqual(result["vault_mode"], "existing")
        card = self._cards("fictional-alpha", 1)[0]
        self.assertEqual(deliver_cards([card], section="Bootstrap", config=self.config)["new"], 1)
        for name, digest in before.items():
            self.assertEqual(hashlib.sha256((vault / name / "notes.md").read_bytes()).hexdigest(), digest)
        self.assertEqual(len(list((vault / "Literature-Radar/Bootstrap/fictional-alpha/HIGH").glob("*.md"))), 1)
        self.assertEqual(status(config=self.config)["counts"]["Bootstrap"]["fictional-alpha"], 1)

        conflict = self._existing("conflict")
        (conflict / "Literature-Radar").mkdir()
        (conflict / "Literature-Radar" / "user.md").write_text("do not overwrite")
        with self.assertRaisesRegex(ObsidianError, "ownership marker"):
            select_existing_vault(conflict, config=self.base / "other.json")
        self.assertEqual((conflict / "Literature-Radar/user.md").read_text(), "do not overwrite")

    def test_60_card_copy_integrity_feedback_and_idempotence(self):
        created = create_dedicated_vault(self.parent, config=self.config,
                    topic_ids=("fictional-alpha", "fictional-beta"), registered=[])
        vault = Path(created["vault_root"])
        cards = self._cards("fictional-alpha") + self._cards("fictional-beta")
        original = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in cards}
        result = deliver_cards(cards, section="Bootstrap", config=self.config)
        self.assertEqual((result["status"], result["new"], result["failed"]), ("SUCCESS", 60, 0))
        self.assertEqual(result["topics"], {"fictional-alpha": 30, "fictional-beta": 30})
        for topic in ("fictional-alpha", "fictional-beta"):
            targets = list((vault / "Bootstrap" / topic).glob("*/*.md"))
            self.assertEqual(len(targets), 30)
            self.assertTrue(all(frontmatter(path.read_text())["topic_id"] == topic for path in targets))
            self.assertTrue(all("## 我的评价" in path.read_text() for path in targets))
        self.assertTrue(all(hashlib.sha256(path.read_bytes()).hexdigest() == digest
                            for path, digest in original.items()))
        again = deliver_cards(cards, section="Bootstrap", config=self.config)
        self.assertEqual((again["new"], again["updated"], again["unchanged"]), (0, 0, 60))
        target = next((vault / "Bootstrap/fictional-alpha/HIGH").glob("*.md"))
        target.write_text(set_properties(target.read_text(), {"feedback_decision": "must_read", "review_status": "reviewed"}) + "\nMy private note\n")
        self.assertEqual(deliver_cards(cards[:30], section="Bootstrap", config=self.config)["updated"], 0)
        self.assertIn("My private note", target.read_text())
        self.assertEqual(frontmatter(target.read_text())["feedback_decision"], "must_read")
        dashboard = (vault / "dashboard.md").read_text()
        self.assertIn("fictional-alpha]]：30 篇", dashboard)
        self.assertIn("fictional-beta]]：30 篇", dashboard)
        self.assertIn("已阅读 / 已反馈：1 篇", dashboard)
        self.assertTrue((vault / "Bootstrap/fictional-alpha/index.md").is_file())
        self.assertTrue((vault / "System/README.md").is_file())

    def test_moved_vault_explicit_rebind_old_schema_and_safety(self):
        vault = self._existing()
        select_existing_vault(vault, config=self.config)
        moved = self.base / "moved-vault"
        vault.rename(moved)
        self.assertEqual(doctor(config=self.config)["status"], "FAIL")
        self.assertIn(str(vault), " ".join(doctor(config=self.config)["issues"]))
        with self.assertRaisesRegex(ObsidianError, "confirm rebind"):
            select_existing_vault(moved, config=self.config)
        rebound = select_existing_vault(moved, config=self.config, rebind=True)
        self.assertEqual(rebound["vault_root"], str(moved))
        self.assertEqual(bound_vault(config=self.config), moved)
        self.assertTrue(list(self.config.parent.glob("obsidian.json.backup-*")))
        old = self.base / "legacy-private.json"
        old.write_text(json.dumps({"version": 1, "vault": str(moved)}))
        with self.assertRaisesRegex(ObsidianError, "older schema"):
            bound_vault(config=old)
        self.assertEqual(select_existing_vault(moved, config=old, rebind=True)["status"], "BOUND")
        self.assertEqual(stat.S_IMODE(old.stat().st_mode), 0o600)
        namespace = moved / "Literature-Radar"
        (namespace / "Bootstrap" / "fictional-alpha").symlink_to(self.base)
        card = self._cards("fictional-alpha", 1)[0]
        self.assertEqual(deliver_cards([card], section="Bootstrap", config=old)["failed"], 1)

    def test_confirmed_staged_migration_backs_up_and_preserves_sources(self):
        vault = self._existing()
        select_existing_vault(vault, config=self.config)
        reports = []
        original = {}
        for topic in ("fictional-alpha", "fictional-beta"):
            cards = self._cards(topic)
            report = self.base / "reports" / topic
            report.mkdir(parents=True)
            (report / "bootstrap-report.json").write_text('{"synthetic": true}')
            shutil.copytree(self.base / "staged" / topic, report / "cards")
            reports.append(report)
            original.update({path: hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in report.rglob("*") if path.is_file()})
        with self.assertRaisesRegex(ObsidianError, "confirmation"):
            migrate_staged(reports, config=self.config)
        self.assertFalse((vault / "Literature-Radar/Bootstrap/fictional-alpha").exists())
        result = migrate_staged(reports, config=self.config, confirmed=True)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["delivery"]["new"], 60)
        self.assertTrue(result["source_preserved"])
        self.assertTrue((Path(result["backup_path"]) / "fictional-alpha/bootstrap-report.json").is_file())
        self.assertEqual(len(list((vault / "Literature-Radar/Bootstrap/fictional-beta").glob("*/*.md"))), 30)
        self.assertTrue(all(hashlib.sha256(path.read_bytes()).hexdigest() == digest
                            for path, digest in original.items()))

    def test_cli_requires_mode_and_confirmation_even_one_vault(self):
        vault = self._existing()
        with patch("literature_radar.cli.discover_vaults", return_value=[vault]), \
             patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli_main(["obsidian", "setup", "--binding-file", str(self.config)]), 2)
        self.assertFalse(self.config.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["obsidian", "setup", "--mode", "later",
                                       "--binding-file", str(self.config)]), 0)
        self.assertTrue(self.config.exists())
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(doctor(config=self.config)["setup_state"], "DEFERRED")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["obsidian", "setup", "--mode", "existing", "--vault", str(vault),
                                       "--yes", "--binding-file", str(self.config)]), 0)
        self.assertEqual(bound_vault(config=self.config), vault)

    def test_single_discovered_vault_is_candidate_only(self):
        vault = self._existing()
        with patch("literature_radar.cli.discover_vaults", return_value=[vault]), \
             patch("builtins.input", return_value="3"), contextlib.redirect_stdout(io.StringIO()):
            result = _interactive_obsidian_setup(self.config)
        self.assertEqual(result["status"], "DEFERRED")
        self.assertFalse((vault / "Literature-Radar").exists())
        self.assertIsNone(bound_vault(config=self.config))

    def test_config_paths_cross_platform_and_readonly(self):
        self.assertEqual(binding_file(home=self.base, platform="linux", environ={}),
                         self.base / ".config/literature-radar/obsidian.json")
        self.assertEqual(binding_file(home=self.base, platform="darwin", environ={}),
                         self.base / "Library/Application Support/literature-radar/obsidian.json")
        self.assertEqual(binding_file(home=self.base, platform="win32", environ={}),
                         self.base / "AppData/Roaming/literature-radar/obsidian.json")
        vault = self._existing()
        os.chmod(vault, 0o500)
        with self.assertRaisesRegex(ObsidianError, "writable"):
            select_existing_vault(vault, config=self.config)

    def test_cli_create_mode_initializes_topics_and_status(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli_main(["obsidian", "setup", "--mode", "create",
                             "--parent-dir", str(self.parent), "--name", "ResearchVault",
                             "--topic-id", "fictional-alpha", "--topic-id", "fictional-beta",
                             "--yes", "--binding-file", str(self.config)])
        self.assertEqual(code, 0)
        target = self.parent / "ResearchVault"
        self.assertEqual(bound_vault(config=self.config), target)
        info = status(config=self.config)
        self.assertEqual(info["vault_mode"], "dedicated")
        self.assertEqual(info["counts"]["Bootstrap"],
                         {"fictional-alpha": 0, "fictional-beta": 0})
