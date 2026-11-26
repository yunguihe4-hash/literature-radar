"""Private AI_OS search vocabulary imports and remains bound to Focus versions."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from literature_radar.config import load_bundle
from literature_radar.discovery.query_planner import (
    _original_groups, import_ai_os_search, load_search_semantics, planned_queries)
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.providers.models import DateWindow
from literature_radar.research.specialty import query_variants
from literature_radar.sources.openalex import OpenAlexProvider
from literature_radar.sources.biorxiv import BioRxivProvider
from literature_radar.topics.focus import set_focus
from literature_radar.topics.manager import initialize
from tests.test_stage37_39_workflow import intake
from tests.synthetic_provider import SyntheticProvider, fictional


class SearchSemanticsImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = initialize(self.root / "config", intake("fictional-alpha"))
        set_focus(self.config, ["new fictional signal"], reason="fictional phase")
        self.original = self.root / "original"
        self.source = (self.original / "Projects/literature-radar/topics/fictional-alpha")
        (self.source / "focuses").mkdir(parents=True)
        (self.source / "topic.toml").write_text('''
topic_id = "fictional-alpha"
description = "Fictional methods of receptor identification"
concepts = ["Alpha concept", "Beta concept"]
entities = ["Alpha receptor", "Beta receptor"]
synonyms = ["AR"]
organisms = ["Fly one", "Fly two"]
methods = ["Assembly method"]
applications = ["Transfer application"]
exclusions = ["Unrelated signal"]
''')
        for version in ("v001", "v002"):
            (self.source / "focuses" / f"{version}.toml").write_text(
                f'topic_id = "fictional-alpha"\nversion = "{version}"\n'
                'title = "Alpha receptor mapping"\n'
                'positive_signals = ["Receptor mapping"]\n'
                'priority_questions = ["What maps receptor?"]\n')

    def test_import_preserves_original_groups_and_specialty_variants(self):
        bundle = load_bundle(self.config)
        provider = OpenAlexProvider()
        window = DateWindow("2026-01-01", "2026-01-07")
        previous_signatures = [plan.query_signature for plan in
                               planned_queries(provider, bundle, window, config_dir=self.config)]
        dry = import_ai_os_search(self.config, self.original, dry_run=True)
        self.assertEqual(dry["status"], "DRY_RUN")
        self.assertFalse((self.config / "search-semantics").exists())
        result = import_ai_os_search(self.config, self.original)
        self.assertEqual(result["versions"], ["v001", "v002"])
        sidecar = load_search_semantics(self.config, bundle)
        self.assertEqual(_original_groups(sidecar), [
            ("core", ["Alpha concept", "Beta concept", "Alpha receptor", "Beta receptor", "AR"]),
            ("organism", ["Fly one", "Fly two", "Alpha concept", "Beta concept", "Alpha receptor"]),
            ("focus", ["Receptor mapping", "Alpha concept", "Beta concept"]),
            ("method", ["Assembly method", "Alpha concept", "Beta concept", "Alpha receptor"]),
        ])
        original_primary = provider.plan_queries(bundle["topic"], bundle["focus"], window)[0]
        plans = planned_queries(provider, bundle, window, config_dir=self.config)
        self.assertEqual(plans[0].query_signature, original_primary.query_signature)
        self.assertTrue(set(previous_signatures).issubset({p.query_signature for p in plans}))
        self.assertEqual([plan.query_parameters["semantic_group"] for plan in plans[-4:]],
                         ["core", "organism", "focus", "method"])
        self.assertEqual([plan.query_id.rsplit(":", 1)[-1] for plan in plans[-4:]],
                         ["core", "organism", "focus", "method"])
        self.assertTrue(all(":aios:" in plan.query_id for plan in plans[-4:]))
        biorxiv = BioRxivProvider()
        bio_plans = planned_queries(biorxiv, bundle, window, config_dir=self.config)
        self.assertEqual(len(bio_plans), 4)
        self.assertEqual([p.query_signature for p in bio_plans[:2]],
                         [p.query_signature for p in biorxiv.plan_queries(
                             bundle["topic"], bundle["focus"], window)])
        self.assertIn("Alpha concept", bio_plans[2].query_parameters["terms"])
        self.assertEqual(tuple(bio_plans[2].query_parameters["semantic_groups"]),
                         ("core", "organism", "focus", "method"))
        variants = query_variants(bundle, 2, sidecar)
        self.assertEqual(variants[0][2], ["Alpha concept", "Alpha receptor"])
        self.assertIn(("focus", "focus:001", ["Receptor mapping"]), variants)
        self.assertEqual(import_ai_os_search(self.config, self.original)["status"], "IMPORTED")

    def test_invalid_current_focus_does_not_write_any_snapshot(self):
        (self.source / "focuses/v002.toml").unlink()
        with self.assertRaisesRegex(ValueError, "Current Focus"):
            import_ai_os_search(self.config, self.original)
        self.assertFalse((self.config / "search-semantics").exists())

    def test_existing_snapshot_conflict_does_not_write_other_versions(self):
        target = self.config / "search-semantics/v002.json"
        target.parent.mkdir()
        target.write_text(json.dumps({"wrong": True}))
        with self.assertRaisesRegex(ValueError, "Immutable"):
            import_ai_os_search(self.config, self.original)
        self.assertFalse((self.config / "search-semantics/v001.json").exists())

    def test_sidecar_topic_identity_checked(self):
        import_ai_os_search(self.config, self.original)
        document = load_search_semantics(self.config, load_bundle(self.config))
        document["topic_id"] = "different-topic"
        path = self.config / "search-semantics/v002.json"
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "identity"):
            load_search_semantics(self.config, load_bundle(self.config))

    def test_discovery_executes_all_imported_groups_once(self):
        import_ai_os_search(self.config, self.original)

        class GroupedProvider(SyntheticProvider):
            supports_semantic_groups = True

            def fetch_page(self, query, cursor=None):
                self.query_ids.append(query.query_id)
                return super().fetch_page(query, cursor)

        provider = GroupedProvider("openalex", [[fictional("fictional-source")]])
        provider.query_ids = []
        database = self.root / "academic.sqlite3"
        kwargs = dict(database_path=database, since="2026-01-01", until="2026-01-07",
                      providers={"openalex": provider})
        first = discover_incremental(self.config, ["openalex"], **kwargs)
        self.assertEqual(first["status"], "SUCCESS")
        expected = len(planned_queries(provider, load_bundle(self.config),
                                       DateWindow("2026-01-01", "2026-01-07"),
                                       config_dir=self.config))
        self.assertEqual(len(set(provider.query_ids)), expected)
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM provider_checkpoints WHERE status='SUCCESS'").fetchone()[0], expected)
        second = discover_incremental(self.config, ["openalex"], **kwargs)
        self.assertEqual(second["status"], "SUCCESS")
        self.assertEqual(len(provider.query_ids), expected)
