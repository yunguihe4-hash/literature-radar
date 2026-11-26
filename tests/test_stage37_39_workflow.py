"""Offline acceptance for the new private Topic, review and weekly workflow."""
from __future__ import annotations

import csv
from collections import Counter
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from literature_radar.attention.recommendation import recommend
from literature_radar.intelligence.engine import align
from literature_radar.config import load_bundle, validate_config_dir
from literature_radar.database.academic import AcademicStore
from literature_radar.database.backup import backup_database, restore_database
from literature_radar.delivery.notifiers import ObsidianNotifier, NotificationUnavailable
from literature_radar.feedback.review import apply_memory, feedback_summary, import_review, propose_memory
from literature_radar.feedback.store import load_feedback
from literature_radar.journal.registry import import_registry, match_quality, load_registry, validate_entry
from literature_radar.models import Paper
from literature_radar.sources.common import source_record
from literature_radar.topics.manager import initialize, list_topics, describe
from literature_radar.workflow.bootstrap import five_year_window, run_bootstrap
from literature_radar.workflow.state import advance, read_state
from literature_radar.workflow.weekly import activate_weekly, run_weekly
from literature_radar.cli import main as cli_main
from tests.synthetic_provider import SyntheticProvider


NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def intake(identifier="synthetic-signals"):
    return {"id": identifier, "name": "Fictional Signal Study",
            "goal": "Compare invented signal states", "questions": ["signal mapping"],
            "target_species": ["Imaginaryus alpha"],
            "model_species": ["Fictivus beta"],
            "methods": ["phase lattice assay"],
            "applications": ["fictional sensor"],
            "transfer_directions": ["lattice method transfer"],
            "exclusions": ["unrelated astronomy"],
            "journal_categories": ["Synthetic Systems"]}


def record(index=1, *, date="2026-11-01", doi=None):
    return {"source_id": f"SYN-{index}",
            "title": f"Imaginaryus alpha signal mapping {index} with phase lattice assay",
            "abstract": "Imaginaryus alpha signal mapping uses a phase lattice assay to compare invented states.",
            "doi": doi or f"10.0000/synthetic-{index}",
            "publication_date": date, "indexed_date": "2026-11-02",
            "journal": "Journal of Fictional Systems", "issn": ["1234-567X"]}


class RichProvider(SyntheticProvider):
    def normalize(self, raw):
        row = raw.payload
        return source_record(raw, source_id=row["source_id"],
                             fields={"title": row["title"], "abstract": row.get("abstract"),
                                     "authors": [], "journal": row.get("journal"),
                                     "issn": row.get("issn"),
                                     "publication_date": row.get("publication_date"),
                                     "online_date": row.get("online_date"),
                                     "indexed_date": row.get("indexed_date"),
                                     "published_doi": row.get("published_doi"),
                                     "document_type": row.get("document_type"),
                                     "landing_url": row.get("landing_url")},
                             identifiers={key: row[key] for key in ("doi", "pmid", "openalex_id")
                                          if row.get(key)})


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, payload, key):
        self.sent.append((payload, key))


class FailingRecorder:
    def send(self, payload, key):
        raise RuntimeError("fictional delivery failure")


class TopicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_topic_init_validate_show_list_isolation_and_permissions(self):
        first = initialize(self.root / "private", intake())
        second = initialize(self.root / "private", intake("synthetic-echo"))
        self.assertEqual(validate_config_dir(first).files_validated, 8)
        self.assertEqual(load_bundle(first)["topic"]["id"], "synthetic-signals")
        self.assertEqual({item["id"] for item in list_topics(self.root / "private")},
                         {"synthetic-signals", "synthetic-echo"})
        self.assertEqual(describe(first)["methods"], ["phase lattice assay"])
        self.assertEqual(json.loads((first / "feedback.json").read_text())["entries"], [])
        self.assertEqual(read_state(first)["state"], "BOOTSTRAP_PENDING")
        self.assertEqual(first.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in first.glob("*.json")))
        with self.assertRaises(FileExistsError):
            initialize(self.root / "private", intake())
        self.assertNotEqual(first, second)

    def test_invalid_topic_is_rejected_without_install(self):
        bad = intake("synthetic-invalid")
        bad["questions"] = []
        with self.assertRaisesRegex(ValueError, "questions"):
            initialize(self.root / "private", bad)
        self.assertFalse((self.root / "private/synthetic-invalid").exists())

    def test_topic_cli_json_import_show_and_list(self):
        source = self.root / "intake.json"
        source.write_text(json.dumps(intake("synthetic-cli")))
        capture = io.StringIO()
        with redirect_stdout(capture):
            self.assertEqual(cli_main(["topic", "init", "--config-root", str(self.root / "private"),
                                       "--input", str(source)]), 0)
        self.assertEqual(json.loads(capture.getvalue())["topic_id"], "synthetic-cli")
        capture = io.StringIO()
        with redirect_stdout(capture):
            self.assertEqual(cli_main(["topic", "show", "--config-dir",
                                       str(self.root / "private/synthetic-cli")]), 0)
        self.assertEqual(json.loads(capture.getvalue())["goal"], "Compare invented signal states")
        capture = io.StringIO()
        with redirect_stdout(capture):
            self.assertEqual(cli_main(["topic", "list", "--config-root", str(self.root / "private")]), 0)
        self.assertEqual(len(json.loads(capture.getvalue())), 1)

    def test_interactive_topic_and_focus_version_switch(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True
        answers = "\n".join(("fictional-wizard", "Fictional Wizard Study", "Explore invented signals",
                             "How are invented signals mapped?", "Imaginaryus alpha", "Fictivus beta",
                             "Synthetic imaging", "Fictional sensor", "Cross-domain imaging review",
                             "Unrelated catalogs", "Synthetic Systems", "none")) + "\n"
        with patch("sys.stdin", FakeTTY(answers)), redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["topic", "init", "--config-root", str(self.root / "wizard")]), 0)
        config = self.root / "wizard/fictional-wizard"
        bundle = load_bundle(config)
        self.assertNotIn("alert_limit", bundle["topic"]["extensions"]["weekly"])
        self.assertEqual({item["type"] for item in bundle["ontology"]["relations"]},
                         {"direct_reference", "method_transfer"})
        self.assertEqual(bundle["attention-memory"]["preferred_concept_ids"], [])
        self.assertEqual(load_feedback(config / "feedback.json")["entries"], [])
        updated = dict(bundle["focus"], version="v002")
        (config / "focus.v002.json").write_text(json.dumps(updated))
        topic = bundle["topic"]
        topic["focus_file"] = "focus.v002.json"
        (config / "topic.json").write_text(json.dumps(topic))
        self.assertEqual(load_bundle(config)["focus"]["version"], "v002")

    def test_topic_path_traversal_and_external_symlink_rejected(self):
        bad = intake("../outside")
        with self.assertRaisesRegex(ValueError, "traversal"):
            initialize(self.root / "private", bad)
        config = initialize(self.root / "private", intake())
        external = self.root / "external-focus.json"
        external.write_text((config / "focus.json").read_text())
        (config / "focus.external.json").symlink_to(external)
        topic = json.loads((config / "topic.json").read_text())
        topic["focus_file"] = "focus.external.json"
        (config / "topic.json").write_text(json.dumps(topic))
        with self.assertRaisesRegex(ValueError, "focus_file"):
            validate_config_dir(config)
        topic["focus_file"] = "../external-focus.json"
        (config / "topic.json").write_text(json.dumps(topic))
        with self.assertRaisesRegex(ValueError, "focus_file"):
            validate_config_dir(config)
        alias = self.root / "config-alias"
        alias.symlink_to(config, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            validate_config_dir(alias)

    def test_direct_reference_schema_and_evidence_gate(self):
        config = initialize(self.root / "private", intake())
        ontology = json.loads((config / "ontology.json").read_text())
        ontology["concepts"] = [{"id": "source-context", "label": "Imaginaryus alpha",
                                 "aliases": ["Imaginaryus alpha"]}]
        ontology["relations"] = [{"from": "source-context", "to": "question-signal-mapping",
                                  "type": "direct_reference"}]
        (config / "ontology.json").write_text(json.dumps(ontology))
        bundle = load_bundle(config)
        paper = Paper("SYN-1", record()["title"], record()["abstract"], "fictional")
        result = recommend(paper, bundle)
        self.assertEqual(result["attention_ranking"]["category"], "A")
        self.assertEqual(result["reason"]["structured_transfers"][0]["transfer_type"], "direct_reference")
        self.assertEqual(result["reason"]["structured_transfers"][0]["source_species"], "Imaginaryus alpha")
        absent = Paper("SYN-2", "phase lattice assay", "An invented method without a scientific question.", "fictional")
        self.assertEqual(recommend(absent, bundle)["attention_ranking"]["category"], "D")

    def test_unproven_method_transfer_keeps_nulls_and_review_flag(self):
        config = initialize(self.root / "private", intake())
        ontology = json.loads((config / "ontology.json").read_text())
        ontology["concepts"] = [{"id": "model-context", "label": "Fictivus beta",
                                 "aliases": ["Fictivus beta"]}]
        ontology["relations"] = [{"from": "model-context", "to": "question-signal-mapping",
                                  "type": "method_transfer", "scope": "cross_species"}]
        (config / "ontology.json").write_text(json.dumps(ontology))
        bundle = load_bundle(config)
        paper = Paper("SYN-3", "Fictivus beta model finding", "Fictivus beta lacks a described method.", "fictional")
        result = recommend(paper, bundle)
        self.assertEqual(result["attention_ranking"]["category"], "D")
        transfer = result["reason"]["structured_transfers"][0]
        self.assertIsNone(transfer["source_method"])
        self.assertIsNone(transfer["transfer_rationale"])
        self.assertEqual(transfer["status"], "NEEDS_REVIEW")

    def test_transfer_needs_a_separate_abstract_premise(self):
        config = initialize(self.root / "private", intake())
        ontology = json.loads((config / "ontology.json").read_text())
        ontology["concepts"] = [{"id": "model-context", "label": "Fictivus beta",
                                 "aliases": ["Fictivus beta"]}]
        relation = {"from": "model-context", "to": "question-signal-mapping",
                    "type": "method_transfer", "scope": "cross_species"}
        ontology["relations"] = [relation]
        (config / "ontology.json").write_text(json.dumps(ontology))
        paper = Paper("SYN-4", "Fictivus beta signal mapping with phase lattice assay",
                      "Fictivus beta signal mapping uses a phase lattice assay. "
                      "The lattice calibrates invented states across organisms.", "fictional")
        without = recommend(paper, load_bundle(config))
        self.assertEqual(without["attention_ranking"]["category"], "D")
        self.assertEqual(without["reason"]["structured_transfers"][0]["status"], "NEEDS_REVIEW")
        self.assertIn("transfer_evidence_phrase_absent_or_unmatched",
                      without["reason"]["structured_transfers"][0]["missing_evidence"])
        relation["extensions"] = {"transfer_evidence_phrase":
                                  "lattice calibrates invented states across organisms"}
        (config / "ontology.json").write_text(json.dumps(ontology))
        with_evidence = recommend(paper, load_bundle(config))
        self.assertEqual(with_evidence["attention_ranking"]["category"], "B")
        self.assertEqual(with_evidence["base_score"], without["base_score"])
        transfer = with_evidence["reason"]["structured_transfers"][0]
        self.assertEqual(transfer["status"], "EVIDENCE_MATCHED")
        self.assertEqual(transfer["evidence_field"], "abstract")
        self.assertEqual(transfer["missing_evidence"], [])
        self.assertEqual(transfer["transfer_evidence_phrase"],
                         "lattice calibrates invented states across organisms")
        self.assertIn("lattice calibrates", transfer["transfer_rationale"])
        for relation_type in ("concept_transfer", "application_transfer"):
            relation["type"] = relation_type
            (config / "ontology.json").write_text(json.dumps(ontology))
            self.assertEqual(recommend(paper, load_bundle(config))["attention_ranking"]["category"], "C")

    def test_four_relation_types_load_and_invalid_type_fails(self):
        config = initialize(self.root / "private", intake())
        ontology = json.loads((config / "ontology.json").read_text())
        ontology["concepts"] = [
            {"id": "source-context", "label": "Imaginaryus alpha", "aliases": ["Imaginaryus alpha"]},
            {"id": "future-idea", "label": "Future synthetic idea"},
        ]
        ontology["relations"] = [
            {"from": "source-context", "to": "question-signal-mapping", "type": "direct_reference"},
            {"from": "source-context", "to": "future-idea", "type": "method_transfer"},
            {"from": "source-context", "to": "application-fictional-sensor", "type": "concept_transfer"},
            {"from": "source-context", "to": "method-phase-lattice-assay", "type": "application_transfer"},
        ]
        (config / "ontology.json").write_text(json.dumps(ontology))
        bundle = load_bundle(config)
        paper = Paper("SYN-1", "Imaginaryus alpha signal mapping",
                      "Imaginaryus alpha signal mapping is a fictional problem.", "fictional")
        self.assertEqual({item["type"] for item in align(paper, bundle["profile"], bundle["ontology"])["transfers"]},
                         {"direct_reference", "method_transfer", "concept_transfer", "application_transfer"})
        ontology["relations"][0]["type"] = "teleport_transfer"
        (config / "ontology.json").write_text(json.dumps(ontology))
        with self.assertRaises(ValueError):
            load_bundle(config)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def entry(self, cas=1, jcr="Q1", year=2025):
        return {"issn": ["1234-567X"], "issn_l": "1234-567X",
                "journal_title": "Journal of Fictional Systems", "aliases": ["J. Fictional Systems"],
                "cas": {"edition_year": year, "major_quartile": cas,
                        "minor_quartile": 2, "major_category": "Synthetic Sciences",
                        "minor_category": "Synthetic Systems", "source": "User licensed table"},
                "jcr": {"edition_year": year,
                        "categories": [{"category": "Synthetic Systems", "quartile": jcr},
                                       {"category": "Imaginary Materials", "quartile": "Q2"}]},
                "license_or_usage_basis": "User-provided permitted local copy"}

    def test_dual_quality_and_issn_match_keep_raw_categories(self):
        row = validate_entry(self.entry())
        result = match_quality([row], issn=["1234-567X"], categories=["Synthetic Systems"])
        self.assertEqual(result["combined"], "DUAL_HIGH")
        self.assertEqual(result["cas"]["minor_quartile"], 2)
        self.assertEqual(len(result["jcr"]["categories"]), 2)
        self.assertEqual(match_quality([row], journal_title="J. Fictional Systems")["combined"], "UNVERIFIED")

    def test_conflict_preprint_and_unverified(self):
        row = validate_entry(self.entry(cas=1, jcr="Q4"))
        self.assertEqual(match_quality([row], issn=["1234-567X"], categories=["Synthetic Systems"])["combined"], "MIXED")
        self.assertEqual(match_quality([row], issn=["1234-567X"], preprint=True)["combined"], "UNVERIFIED")
        self.assertEqual(match_quality([], issn=["1234-567X"])["cas"]["status"], "未核实")

    def test_years_not_merged_and_invalid_data_not_written(self):
        row = self.entry()
        row["jcr"]["edition_year"] = 2024
        self.assertEqual(match_quality([validate_entry(row)], issn=["1234-567X"])["combined"], "UNVERIFIED")
        source = self.root / "bad.json"
        source.write_text(json.dumps([{"issn": ["bad"], "cas": {"major_quartile": 1}}]))
        target = self.root / "registry.json"
        with self.assertRaises(ValueError):
            import_registry(source, target)
        self.assertFalse(target.exists())

    def test_json_import_and_duplicate_rejection(self):
        source = self.root / "quality.json"
        source.write_text(json.dumps([self.entry()]))
        target = self.root / "registry.json"
        self.assertEqual(import_registry(source, target), 1)
        self.assertEqual(len(load_registry(target)), 1)
        with self.assertRaises(ValueError):
            import_registry(source, target)

    def test_csv_import_and_single_system_high(self):
        source = self.root / "quality.csv"
        with source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("issn", "issn_l", "journal_title", "aliases",
                                    "cas_edition_year", "cas_major_quartile", "cas_minor_quartile",
                                    "cas_major_category", "cas_minor_category", "cas_source",
                                    "jcr_edition_year", "jcr_categories", "license_or_usage_basis"))
            writer.writeheader()
            writer.writerow({"issn": "1234-567X", "journal_title": "Journal of Fictional Systems",
                             "cas_edition_year": 2025, "cas_major_quartile": 1,
                             "cas_major_category": "Synthetic Sciences",
                             "cas_source": "User licensed table",
                             "license_or_usage_basis": "User-provided permitted local copy"})
        target = self.root / "registry.json"
        self.assertEqual(import_registry(source, target), 1)
        self.assertEqual(match_quality(load_registry(target), issn=["1234-567X"])["combined"],
                         "SINGLE_SYSTEM_HIGH")

    def test_dual_axis_matrix_separate_imports_and_identity_conflicts(self):
        for cas, jcr, expected in ((1, "Q1", "DUAL_HIGH"), (2, "Q2", "DUAL_UPPER"),
                                   (1, "Q3", "MIXED"), (3, "Q1", "MIXED")):
            with self.subTest(cas=cas, jcr=jcr):
                result = match_quality([validate_entry(self.entry(cas, jcr))],
                                       issn=["1234-567X"], categories=["Synthetic Systems"])
                self.assertEqual(result["combined"], expected)
                self.assertEqual(result["cas"]["major_quartile"], cas)
                self.assertEqual(result["jcr"]["categories"][0]["quartile"], jcr)
        cas_only = self.entry()
        cas_only["jcr"] = {}
        jcr_only = self.entry()
        jcr_only["cas"] = {}
        paired = [validate_entry(cas_only), validate_entry(jcr_only)]
        self.assertEqual(match_quality(paired, issn=["1234-567X"],
                                       categories=["Synthetic Systems"])["combined"], "DUAL_HIGH")
        self.assertEqual(match_quality(paired, issn=["1234-567X"])["combined"], "DUAL_UPPER")
        self.assertEqual(match_quality([paired[0]], issn=["1234-567X"])["combined"], "SINGLE_SYSTEM_HIGH")
        self.assertEqual(match_quality([paired[1]], issn=["1234-567X"],
                                       categories=["Synthetic Systems"])["combined"], "SINGLE_SYSTEM_HIGH")
        self.assertEqual(match_quality([], issn=["1234-567X"])["combined"], "UNVERIFIED")
        incompatible = self.entry()
        incompatible["journal_title"] = "A Different Fictional Journal"
        incompatible["issn"] = ["1234-567X", "7777-888X"]
        result = match_quality([paired[0], validate_entry(incompatible)], issn=["1234-567X"])
        self.assertEqual((result["combined"], result["identity_status"]), ("UNVERIFIED", "CONFLICT"))
        unrelated = self.entry()
        unrelated["issn"] = ["9999-000X"]
        unrelated["issn_l"] = "9999-000X"
        self.assertEqual(match_quality([validate_entry(unrelated)], issn=["1234-567X"],
                                       journal_title="Journal of Fictional Systems")["combined"], "UNVERIFIED")

    def test_newest_edition_does_not_mix_years_or_choose_conflicts(self):
        old = validate_entry(self.entry(cas=1, jcr="Q1", year=2024))
        new = validate_entry(self.entry(cas=2, jcr="Q2", year=2025))
        self.assertEqual(match_quality([old, new], issn=["1234-567X"])["combined"], "DUAL_UPPER")
        self.assertEqual(match_quality([old, new], issn=["1234-567X"], edition_year=2024,
                                       categories=["Synthetic Systems"])["combined"], "DUAL_HIGH")
        conflict = self.entry(cas=3, jcr="Q2", year=2025)
        conflict["issn"] = ["1234-567X", "7777-888X"]
        result = match_quality([old, new, validate_entry(conflict)], issn=["1234-567X"])
        self.assertEqual((result["combined"], result["identity_status"]), ("UNVERIFIED", "CONFLICT"))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = initialize(self.root / "private", intake())
        self.db = self.root / "state.sqlite3"
        self.output = self.root / "review"

    def providers(self, rows, *, names=None, fail_at=None):
        names = names or ["openalex", "crossref", "europepmc", "pubmed", "biorxiv"]
        return {name: RichProvider(name, [rows], fail_at=fail_at if name == names[0] else None)
                for name in names}

    def test_five_provider_sixty_plus_candidate_review_and_fifty_feedback(self):
        names = ("openalex", "crossref", "europepmc", "pubmed", "biorxiv")
        ontology = json.loads((self.config / "ontology.json").read_text())
        for relation in ontology["relations"]:
            if relation["type"] == "method_transfer":
                relation["extensions"] = {"transfer_evidence_phrase":
                                          "lattice calibrates invented states across organisms"}
        ontology["concepts"].append({"id": "species-speciatus-delta", "label": "Speciatus delta",
                                      "aliases": ["Speciatus delta"]})
        ontology["relations"].append({"from": "species-speciatus-delta",
                                      "to": "question-signal-mapping", "type": "concept_transfer",
                                      "scope": "cross_species", "from_domain": "Speciatus delta",
                                      "extensions": {"transfer_evidence_phrase":
                                                     "lattice calibrates invented states across organisms"}})
        (self.config / "ontology.json").write_text(json.dumps(ontology))
        rows = []
        for index in range(1, 61):
            item = record(index)
            if index >= 36:
                species = "Fictivus beta" if index <= 45 else "Speciatus delta"
                item["title"] = f"{species} signal mapping {index} with phase lattice assay"
                item["abstract"] = (f"{species} signal mapping uses a phase lattice assay. "
                                    "The lattice calibrates invented states across organisms.")
            item["issn"] = [f"{index % 5 + 1000:04d}-567X"]
            item["journal"] = f"Fictional Journal {index % 5}"
            item["document_type"] = "journal-article"
            item["landing_url"] = f"https://example.invalid/paper/{index}"
            if index == 58:
                item["abstract"] = None
            if index == 59:
                item["publication_date"] = None
            if index == 60:
                item["publication_date"] = "2021-11-02"
            if index == 57:
                item["doi"] = None
            rows.append(item)
        for index in range(61, 66):
            item = record(index)
            item["title"] = f"Unrelated fictional catalog {index}"
            item["abstract"] = "A keyword in a catalog has no research question or method."
            rows.append(item)
        preprint = dict(record(66), source_id="SYN-PREPRINT-1", doi="10.0000/preprint-1",
                        document_type="preprint", online_date="2026-01-01",
                        published_doi="10.0000/synthetic-1")
        rows.append(preprint)
        pages = {name: [[], []] for name in names}
        for index, row in enumerate(rows):
            pages[names[index % 5]][(index // 5) % 2].append(row)
        for index in range(5):
            duplicate = dict(rows[index], source_id=f"OTHER-{index}")
            pages[names[(index + 1) % 5]][1].append(duplicate)
        providers = {name: RichProvider(name, pages[name]) for name in names}
        journal_entries = [{"journal_title": f"Fictional Journal {index}",
                            "issn": "", "issn_l": "", "jcr_year": "2025",
                            "impact_factor": str(2.5 + index), "jcr_quartile": "Q1",
                            "subject_category": "Synthetic Systems",
                            "source": "Fictional licensed fixture",
                            "license_or_usage_basis": "Fictional local fixture",
                            "retrieved_at": "2026-11-03"} for index in range(5)]
        registry = self.root / "fictional-journals.json"
        registry.write_text(json.dumps({"schema_version": 1, "entries": journal_entries}))
        result = run_bootstrap(self.config, self.db, self.output, now=NOW,
                               registry_path=registry, providers=providers, limit=50)
        self.assertEqual(result["completeness"], "COMPLETE")
        self.assertEqual(result["selected"], 50)
        self.assertEqual(AcademicStore(self.db).count(), 50)
        self.assertFalse((self.output / "bootstrap-working.sqlite3").exists())
        report = json.loads((self.output / "bootstrap-report.json").read_text())
        self.assertEqual(report["coverage"]["candidate_counts"]["observed"], 66)
        self.assertEqual(report["coverage"]["candidate_counts"]["preprint_version_duplicate"], 1)
        self.assertEqual(Counter(row["relevance"] for row in report["papers"]),
                         {"A": 35, "B": 10, "C": 5})
        self.assertEqual(Counter(row["transfer_type"] for row in report["papers"]),
                         {"direct_reference": 35, "method_transfer": 10, "concept_transfer": 5})
        self.assertTrue(all(row["transfer_explanation"] and
                            row["transfer_explanation"]["source_species"] and
                            row["transfer_explanation"]["source_research_question"] and
                            row["transfer_explanation"]["source_method"] and
                            row["transfer_explanation"]["target_research_question"] and
                            row["transfer_explanation"]["evidence_excerpt"] and
                            row["borrowing_value"]
                            for row in report["papers"] if row["relevance"] in ("B", "C")))
        self.assertTrue(all(row["why_recommended"] and row["evidence"] for row in report["papers"]))
        self.assertTrue(all(row["borrowing_value"] for row in report["papers"]))
        self.assertTrue(all(row["year"] and row["journal"] and row["url"] and row["document_type"]
                            for row in report["papers"]))
        self.assertTrue(all("cas" not in row and "jcr" not in row and
                            "journal_quality_status" not in row for row in report["papers"]))
        self.assertEqual(sum(item["pages"] for item in report["coverage"]["providers"].values()), 10)
        markdown = (self.output / "bootstrap-report.md").read_text()
        self.assertIn("Evidence and borrowing details", markdown)
        self.assertIn("Document type:", markdown)
        review = self.output / "bootstrap-review.tsv"
        with review.open(encoding="utf-8", newline="") as handle:
            items = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(len(items), 50)
        for index, item in enumerate(items):
            item["decision"] = ("must_read" if index < 10 else "keep" if index < 35 else
                                "later" if index < 45 else "discard")
            item["scientific_reason"] = "Fictional manual review"
            item["important_method"] = "phase lattice assay"
            item["important_question"] = "signal mapping"
        with review.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=items[0], delimiter="\t")
            writer.writeheader()
            writer.writerows(items)
        before = recommend(next(p for p in AcademicStore(self.db).topic_papers("synthetic-signals")
                                if p.paper_id == items[0]["paper_id"]), load_bundle(self.config))
        self.assertEqual(import_review(self.config, review, self.config / "feedback.json")["imported"], 50)
        self.assertEqual(feedback_summary(self.config, self.config / "feedback.json")["decisions"],
                         {"must_read": 10, "keep": 25, "later": 10, "discard": 5})
        proposal_path = self.root / "memory-proposal.json"
        proposal = propose_memory(self.config, self.config / "feedback.json", proposal_path)
        self.assertEqual(load_bundle(self.config)["attention-memory"]["preferred_concept_ids"], [])
        proposal["confirmed"] = proposal["candidates"]
        proposal_path.write_text(json.dumps(proposal))
        apply_memory(self.config, proposal_path, confirmed=True)
        after = recommend(next(p for p in AcademicStore(self.db).topic_papers("synthetic-signals")
                               if p.paper_id == items[0]["paper_id"]), load_bundle(self.config),
                          load_bundle(self.config)["attention-memory"])
        # Legacy Attention scoring intentionally includes confirmed Memory;
        # the evidence-only score and research category remain unchanged.
        self.assertEqual(before["attention_ranking"]["category"], after["attention_ranking"]["category"])
        self.assertEqual(before["reason"]["rules_evidence"], after["reason"]["rules_evidence"])
        self.assertEqual(before["reason"]["evidence"], after["reason"]["evidence"])
        self.assertGreaterEqual(after["base_score"], before["base_score"])
        self.assertGreater(after["attention_ranking"]["components"]["confirmed_memory"],
                           before["attention_ranking"]["components"]["confirmed_memory"])
        self.assertNotEqual(before["attention_ranking"]["ordering"], after["attention_ranking"]["ordering"])

    def test_five_year_window_and_five_provider_bootstrap(self):
        self.assertEqual(five_year_window(NOW.date()), ("2021-11-03", "2026-11-03"))
        rows = [record(1), record(2)]
        result = run_bootstrap(self.config, self.db, self.output,
                               providers=self.providers(rows), now=NOW)
        self.assertEqual(result["completeness"], "PARTIAL")
        self.assertEqual(result["selected"], 2)
        report = json.loads((self.output / "bootstrap-report.json").read_text())
        self.assertEqual(set(report["coverage"]["providers"]),
                         {"openalex", "crossref", "europepmc", "pubmed", "biorxiv"})
        self.assertTrue(all(row["relevance"] == "A" for row in report["papers"]))
        self.assertTrue(all("journal_quality_status" not in row for row in report["papers"]))
        self.assertEqual(read_state(self.config)["state"], "BOOTSTRAP_REVIEW_READY")
        self.assertTrue((self.output / "bootstrap-report.md").is_file())
        self.assertTrue((self.output / "bootstrap-review.tsv").is_file())
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM papers_v2").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)

    def test_bootstrap_dry_run_does_not_create_state_or_database(self):
        result = run_bootstrap(self.config, self.db, self.output, now=NOW, dry_run=True,
                               provider_names=["openalex"],
                               providers=self.providers([record(1)], names=["openalex"]))
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.db.exists())
        self.assertEqual(read_state(self.config)["state"], "BOOTSTRAP_PENDING")

    def test_bootstrap_partial_resume_and_review_table_preserved(self):
        provider = RichProvider("openalex", [[record(1)], [record(2)]])
        first = run_bootstrap(self.config, self.db, self.output, provider_names=["openalex"],
                              providers={"openalex": provider}, max_pages=1, now=NOW)
        self.assertEqual(first["completeness"], "INCOMPLETE")
        self.assertEqual(read_state(self.config)["state"], "BOOTSTRAP_RUNNING")
        tsv = self.output / "bootstrap-review.tsv"
        self.assertFalse(tsv.exists())
        self.assertFalse(self.db.exists())
        self.assertTrue((self.output / "bootstrap-working.sqlite3").exists())
        second = run_bootstrap(self.config, self.db, self.output, provider_names=["openalex"],
                               providers={"openalex": provider}, max_pages=2, now=NOW,
                               resume=True)
        self.assertEqual(second["completeness"], "PARTIAL")
        self.assertEqual(second["selected"], 2)
        with tsv.open(encoding="utf-8", newline="") as handle:
            review_rows = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(len(review_rows), 2)
        self.assertFalse((self.output / "bootstrap-working.sqlite3").exists())
        self.assertEqual(review_rows[1]["scientific_reason"], "")
        report = json.loads((self.output / "bootstrap-report.json").read_text())
        self.assertTrue(all(row["abstract_status"] == "AVAILABLE" for row in report["papers"]))
        self.assertTrue(all(row["abstract"] for row in report["papers"]))
        self.assertEqual(provider.calls, [0, 1])

    def test_missing_date_and_d_category_do_not_fill_fifty(self):
        dated = record(1)
        unknown = record(2, date=None)
        unrelated = record(3)
        unrelated["title"] = "Imaginary astronomy notes"
        unrelated["abstract"] = "Fictional sky positions with no signal experiment."
        result = run_bootstrap(self.config, self.db, self.output,
                               provider_names=["openalex"],
                               providers=self.providers([dated, unknown, unrelated], names=["openalex"]),
                               now=NOW)
        self.assertEqual(result["selected"], 1)
        counts = result["coverage"]["candidate_counts"]
        self.assertEqual(counts["date_unverified"], 1)
        self.assertEqual(counts["irrelevant_d"], 1)

    def test_window_boundary_and_preprint_published_link(self):
        old = record(1, date="2021-11-02")
        boundary = record(2, date="2021-11-03")
        preprint = record(3, date=None, doi="10.0000/preprint-3")
        preprint.update({"document_type": "preprint", "online_date": "2026-10-30",
                         "published_doi": "10.0000/formal-3"})
        formal = record(4, doi="10.0000/formal-3")
        result = run_bootstrap(self.config, self.db, self.output,
                               provider_names=["openalex"],
                               providers=self.providers([old, boundary, preprint, formal], names=["openalex"]),
                               now=NOW)
        self.assertEqual(result["selected"], 2)
        self.assertEqual(result["coverage"]["candidate_counts"]["outside_window"], 1)
        self.assertEqual(result["coverage"]["candidate_counts"]["preprint_version_duplicate"], 1)

    def test_one_provider_failure_keeps_other_sources_and_marks_incomplete(self):
        good = RichProvider("crossref", [[record(1)]])
        bad = RichProvider("openalex", [[record(2)]], fail_at=0)
        result = run_bootstrap(self.config, self.db, self.output,
                               provider_names=["openalex", "crossref"],
                               providers={"openalex": bad, "crossref": good}, now=NOW)
        self.assertEqual(result["completeness"], "INCOMPLETE")
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["coverage"]["providers"]["openalex"]["status"], "FAILED")
        self.assertEqual(result["coverage"]["providers"]["crossref"]["status"], "SUCCESS")

    def test_feedback_import_idempotence_and_memory_confirmation(self):
        run_bootstrap(self.config, self.db, self.output,
                      provider_names=["openalex"],
                      providers=self.providers([record(1), record(2)], names=["openalex"]), now=NOW)
        review = self.output / "bootstrap-review.tsv"
        with review.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        for row in rows:
            row["decision"] = "keep"
            row["scientific_reason"] = "Synthetic manual judgment"
            row["important_method"] = "phase lattice assay"
            row["important_question"] = "signal mapping"
            row["transfer_type"] = "direct_reference"
        with review.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0], delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)
        self.assertEqual(import_review(self.config, review, self.config / "feedback.json")["imported"], 2)
        self.assertEqual(import_review(self.config, review, self.config / "feedback.json")["imported"], 0)
        self.assertEqual(feedback_summary(self.config, self.config / "feedback.json")["decisions"]["keep"], 2)
        proposal_file = self.root / "memory-proposal.json"
        proposal = propose_memory(self.config, self.config / "feedback.json", proposal_file)
        self.assertTrue(any(item["kind"] == "concept_id" for item in proposal["candidates"]))
        self.assertEqual(json.loads((self.config / "attention-memory.json").read_text())["preferred_concept_ids"], [])
        with self.assertRaises(ValueError):
            apply_memory(self.config, proposal_file)
        proposal["confirmed"] = proposal["candidates"]
        proposal_file.write_text(json.dumps(proposal))
        applied = apply_memory(self.config, proposal_file, confirmed=True)
        self.assertIn("method-phase-lattice-assay", applied["preferred_concept_ids"])
        self.assertIn("direct_reference", applied["confirmed_transfer_types"])
        self.assertTrue(read_state(self.config)["memory_review_confirmed"])
        self.assertEqual(activate_weekly(self.config)["state"], "WEEKLY_MONITORING")

    def test_invalid_feedback_rejected_without_change(self):
        review = self.root / "invalid.tsv"
        review.write_text("\t".join(FIELDS) + "\nSYN-1\tinvalid\tbad\t\t\t\n")
        before = (self.config / "feedback.json").read_bytes()
        with self.assertRaises(ValueError):
            import_review(self.config, review, self.config / "feedback.json")
        self.assertEqual((self.config / "feedback.json").read_bytes(), before)

    def test_all_feedback_decisions_remain_paper_specific(self):
        advance(self.config, "BOOTSTRAP_REVIEW_READY")
        review = self.root / "four-decisions.tsv"
        with review.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter="\t")
            writer.writeheader()
            for index, decision in enumerate(("must_read", "keep", "later", "discard"), 1):
                writer.writerow({"paper_id": f"SYN-{index}", "decision": decision,
                                 "scientific_reason": "Synthetic manual review"})
        self.assertEqual(import_review(self.config, review, self.config / "feedback.json")["imported"], 4)
        summary = feedback_summary(self.config, self.config / "feedback.json")
        self.assertEqual(summary["decisions"], {"must_read": 1, "keep": 1, "later": 1, "discard": 1})
        proposal = propose_memory(self.config, self.config / "feedback.json",
                                  self.root / "empty-proposal.json")
        self.assertEqual(proposal["candidates"], [])
        self.assertEqual(load_feedback(self.config / "feedback.json")["entries"][-1]["decision"], "discard")

    def test_initial_feedback_and_weekly_require_explicit_review(self):
        review = self.root / "premature.tsv"
        review.write_text("\t".join(FIELDS) + "\nSYN-1\tkeep\tFictional manual note\t\t\t\n")
        with self.assertRaisesRegex(ValueError, "Bootstrap review"):
            import_review(self.config, review, self.config / "feedback.json")
        advance(self.config, "MEMORY_REVIEW")
        with self.assertRaisesRegex(ValueError, "explicitly record"):
            activate_weekly(self.config)

    def test_latest_feedback_controls_memory_without_erasing_history(self):
        advance(self.config, "BOOTSTRAP_REVIEW_READY")
        review = self.root / "revisions.tsv"
        def write(rows):
            with review.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter="\t")
                writer.writeheader()
                writer.writerows(rows)
        positive = [{"paper_id": f"SYN-{index}", "decision": "keep",
                     "scientific_reason": "Fictional initial judgment",
                     "important_method": "phase lattice assay"} for index in (1, 2)]
        write(positive)
        self.assertEqual(import_review(self.config, review, self.config / "feedback.json")["imported"], 2)
        write([{"paper_id": "SYN-1", "decision": "discard",
                "scientific_reason": "Fictional later correction"}])
        self.assertEqual(import_review(self.config, review, self.config / "feedback.json")["imported"], 1)
        self.assertEqual(len(load_feedback(self.config / "feedback.json")["entries"]), 3)
        self.assertEqual(feedback_summary(self.config, self.config / "feedback.json")["decisions"],
                         {"must_read": 0, "keep": 1, "later": 0, "discard": 1})
        proposal = propose_memory(self.config, self.config / "feedback.json",
                                  self.root / "revised-proposal.json")
        self.assertEqual(proposal["candidates"], [])

    def test_weekly_requires_activation_then_recommends_all_and_deduplicates(self):
        with self.assertRaises(ValueError):
            run_weekly(self.config, self.db, self.output, now=NOW,
                       provider_names=["openalex"], providers=self.providers([record(1)], names=["openalex"]))
        activate_weekly(self.config, skip_bootstrap=True, reason="Fictional offline test")
        delivery = json.loads((self.config / "delivery.json").read_text())
        delivery["notification"] = {"enabled": True, "provider": "desktop"}
        (self.config / "delivery.json").write_text(json.dumps(delivery))
        recorder = Recorder()
        rows = [record(index) for index in range(1, 6)]
        first = run_weekly(self.config, self.db, self.output, now=NOW,
                           provider_names=["openalex"],
                           providers=self.providers(rows, names=["openalex"]),
                           notifiers={"desktop": recorder})
        self.assertEqual(first["alerted"], 5)
        self.assertEqual(len(recorder.sent), 5)
        self.assertEqual(first["notification"]["sent"], 5)
        self.assertEqual(run_weekly(self.config, self.db, self.output, now=NOW,
                                    provider_names=["openalex"],
                                    providers=self.providers(rows, names=["openalex"]))["status"], "NOT_DUE")
        second = run_weekly(self.config, self.db, self.output, now=NOW,
                            provider_names=["openalex"],
                            providers=self.providers(rows, names=["openalex"]),
                            notifiers={"desktop": recorder}, force=True)
        self.assertEqual(second["alerted"], 0)
        self.assertEqual(len(recorder.sent), 5)

    def test_weekly_late_indexed_and_material_update(self):
        activate_weekly(self.config, skip_bootstrap=True, reason="Fictional offline test")
        late = record(1, date="2026-10-20")
        first = run_weekly(self.config, self.db, self.output, now=NOW,
                           provider_names=["openalex"],
                           providers=self.providers([late], names=["openalex"]))
        self.assertEqual(first["events"][0]["event"], "LATE_INDEXED")
        self.assertEqual(first["primary_window"]["start"], "2026-10-28")
        self.assertEqual(first["overlap_window"]["start"], "2026-10-20")
        updated = dict(late, abstract=late["abstract"] + " A new fictional validation was added.")
        later = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
        second = run_weekly(self.config, self.db, self.output, now=later,
                            provider_names=["openalex"],
                            providers=self.providers([updated], names=["openalex"]))
        self.assertEqual(second["events"][0]["event"], "MATERIAL_UPDATE")

    def test_weekly_delivery_failure_keeps_checkpoint_and_can_retry(self):
        from literature_radar.database.academic import AcademicStore
        from literature_radar.delivery.engine import dispatch_outbox
        activate_weekly(self.config, skip_bootstrap=True, reason="Fictional offline test")
        delivery = json.loads((self.config / "delivery.json").read_text())
        delivery["notification"] = {"enabled": True, "provider": "desktop"}
        (self.config / "delivery.json").write_text(json.dumps(delivery))
        first = run_weekly(self.config, self.db, self.output, now=NOW,
                           provider_names=["openalex"],
                           providers=self.providers([record(1)], names=["openalex"]),
                           notifiers={"desktop": FailingRecorder()})
        self.assertEqual(first["notification"]["failed"], 1)
        store = AcademicStore(self.db)
        self.assertIsNotNone(store.latest_provider_success("openalex", "synthetic-signals"))
        self.assertEqual(store.retry_failed_notifications(channel="desktop"), 1)
        recorder = Recorder()
        self.assertEqual(dispatch_outbox(store, {"desktop": recorder})["sent"], 1)
        self.assertEqual(len(recorder.sent), 1)

    def test_thirty_day_weekly_events_metadata_and_provider_recovery(self):
        from datetime import timedelta
        activate_weekly(self.config, skip_bootstrap=True, reason="Fictional accelerated clock")
        early = record(1)
        late = record(2, date="2026-10-20")
        first = run_weekly(self.config, self.db, self.output, now=NOW,
                           provider_names=["openalex"], providers=self.providers([early, late], names=["openalex"]))
        self.assertEqual({item["event"] for item in first["events"]}, {"NEW_PAPER", "LATE_INDEXED"})
        self.assertLessEqual(first["alerted"], 3)
        bibliographic = dict(early, journal="A corrected fictional journal title",
                             indexed_date="2026-11-10")
        second = run_weekly(self.config, self.db, self.output, now=NOW + timedelta(days=7),
                            provider_names=["openalex"], providers=self.providers([bibliographic], names=["openalex"]))
        self.assertEqual(second["events"][0]["event"], "UNCHANGED")
        self.assertEqual(second["alerted"], 0)
        revised = dict(bibliographic, abstract=bibliographic["abstract"] +
                       " An additional invented experiment changes the conclusion.")
        third = run_weekly(self.config, self.db, self.output, now=NOW + timedelta(days=14),
                           provider_names=["openalex"], providers=self.providers([revised], names=["openalex"]))
        self.assertEqual(third["events"][0]["event"], "MATERIAL_UPDATE")
        failure = run_weekly(self.config, self.db, self.output, now=NOW + timedelta(days=21),
                             provider_names=["openalex"],
                             providers=self.providers([revised], names=["openalex"], fail_at=0))
        self.assertEqual(failure["status"], "INCOMPLETE")
        recovered = run_weekly(self.config, self.db, self.output, now=NOW + timedelta(days=21),
                               provider_names=["openalex"],
                               providers=self.providers([revised], names=["openalex"]))
        self.assertEqual(recovered["status"], "SUCCESS")
        no_doi = dict(record(3, date="2026-12-01"), doi=None, pmid="987654321",
                      indexed_date="2026-12-01")
        cross_source = dict(no_doi, source_id="CROSS-SOURCE-3")
        providers = {"openalex": RichProvider("openalex", [[no_doi]]),
                     "crossref": RichProvider("crossref", [[cross_source]])}
        fifth = run_weekly(self.config, self.db, self.output, now=NOW + timedelta(days=28),
                           provider_names=["openalex", "crossref"], providers=providers)
        self.assertEqual(fifth["status"], "SUCCESS")
        self.assertLessEqual(fifth["alerted"], 3)
        self.assertEqual(len(fifth["events"]), 1)
        self.assertEqual(fifth["providers"]["openalex"]["records"] +
                         fifth["providers"]["crossref"]["records"], 2)
        self.assertNotIn("journal_quality", fifth["events"][0])
        day30 = run_weekly(self.config, self.db, self.output, now=NOW + timedelta(days=30),
                           provider_names=["openalex"], providers=self.providers([], names=["openalex"]))
        self.assertEqual(day30["status"], "NOT_DUE")
        (self.root / "weekly-acceptance-summary.json").write_text(json.dumps({
            "day0": {"events": Counter(item["event"] for item in first["events"]), "alerted": first["alerted"]},
            "day7": {"events": Counter(item["event"] for item in second["events"]), "alerted": second["alerted"]},
            "day14": {"events": Counter(item["event"] for item in third["events"]), "alerted": third["alerted"]},
            "day21_failure": failure["status"], "day21_recovery": recovered["status"],
            "day28": {"events": Counter(item["event"] for item in fifth["events"]), "alerted": fifth["alerted"]},
            "day30": day30["status"],
        }, indent=2))

    def test_independent_sqlite_and_feedback_memory_backup_restore(self):
        activate_weekly(self.config, skip_bootstrap=True, reason="Fictional backup test")
        delivery = json.loads((self.config / "delivery.json").read_text())
        delivery["notification"] = {"enabled": True, "provider": "desktop"}
        (self.config / "delivery.json").write_text(json.dumps(delivery))
        run_weekly(self.config, self.db, self.output, now=NOW,
                   provider_names=["openalex"], providers=self.providers([record(1)], names=["openalex"]),
                   notifiers={"desktop": FailingRecorder()})
        feedback = {"schema_version": 1, "entries": [{"topic_id": "synthetic-signals",
                     "paper_key": "SYN-1", "decision": "keep", "reason": "Fictional review",
                     "recorded_at": NOW.isoformat()}]}
        (self.config / "feedback.json").write_text(json.dumps(feedback))
        memory = json.loads((self.config / "attention-memory.json").read_text())
        memory["preferred_concept_ids"] = ["question-signal-mapping"]
        (self.config / "attention-memory.json").write_text(json.dumps(memory))
        backup = backup_database(self.db, self.root / "backup.sqlite3")
        private_backup = self.root / "private-backup"
        private_backup.mkdir(mode=0o700)
        for name in ("feedback.json", "attention-memory.json"):
            shutil.copy2(self.config / name, private_backup / name)
        restored = restore_database(backup, self.root / "restored.sqlite3")
        restored_config = self.root / "restored-config"
        shutil.copytree(self.config, restored_config)
        for name in ("feedback.json", "attention-memory.json"):
            shutil.copy2(private_backup / name, restored_config / name)
        self.assertEqual(load_feedback(restored_config / "feedback.json"), feedback)
        self.assertEqual(load_bundle(restored_config)["attention-memory"]["preferred_concept_ids"],
                         ["question-signal-mapping"])
        with closing(sqlite3.connect(self.db)) as original, closing(sqlite3.connect(restored)) as copy:
            for table in ("papers_v2", "source_records", "provider_checkpoints", "notification_outbox"):
                count = lambda db: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(count(original), count(copy), table)
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(copy.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_temporary_obsidian_vault_versions_and_symlink_boundary(self):
        vault = self.root / "fictional-vault"
        vault.mkdir()
        payload = {"topic_id": "synthetic-signals", "content_version": "fictional-v1",
                   "priority": "HIGH", "relevance": "A", "transfer_type": "direct_reference",
                   "research_question": "signal mapping", "why_recommended": "Fictional abstract evidence",
                   "feedback_file": "PRIVATE_FEEDBACK_FILE",
                   "paper": {"paper_id": "SYN-1", "title": "Fictional signal mapping",
                             "authors": ["Invented Author"], "publication_date": "2026-11-01",
                             "sources": ["fictional"], "doi": "10.0000/synthetic-1",
                             "landing_url": "https://example.invalid/fictional-1"},
                   "reason": {"questions": ["signal mapping"], "methods": ["phase lattice assay"],
                              "applications": [], "transfers": [],
                              "evidence": [{"field": "abstract", "excerpt": "Fictional abstract evidence"}]}}
        notifier = ObsidianNotifier(vault)
        notifier.send(payload, "fictional-key-1")
        notifier.send(payload, "fictional-key-1")
        notes = list((vault / "Inbox/Literature").glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].read_text().count("literature-radar-version:"), 1)
        notifier.send({**payload, "content_version": "fictional-v2"}, "fictional-key-2")
        content = notes[0].read_text()
        self.assertEqual(content.count("literature-radar-version:"), 2)
        for text in ("10.0000/synthetic-1", "https://example.invalid/fictional-1",
                     "Fictional abstract evidence", "Feedback: keep / must_read / later / discard"):
            self.assertIn(text, content)
        self.assertFalse((vault / "Knowledge").exists())
        self.assertFalse(list(vault.rglob("*Dashboard*")))
        hostile_vault = self.root / "hostile-vault"
        hostile_vault.mkdir()
        outside = self.root / "outside-vault"
        outside.mkdir()
        (hostile_vault / "Inbox").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(NotificationUnavailable):
            ObsidianNotifier(hostile_vault).send(payload, "fictional-key-3")
        self.assertFalse((outside / "Literature").exists())


FIELDS = ("paper_id", "decision", "scientific_reason", "important_method",
          "important_question", "transfer_type")
