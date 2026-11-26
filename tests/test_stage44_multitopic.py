"""Fictional Stage 4.4 end-to-end isolation and unbounded Weekly regression."""
from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.database.academic import AcademicStore
from literature_radar.cli import main as cli_main
from literature_radar.attention.recommendation import recommend
from literature_radar.config import load_bundle
from literature_radar.delivery.cards import frontmatter, write_card
from literature_radar.delivery.review_ui import set_properties
from literature_radar.feedback.cards import import_cards
from literature_radar.feedback.review import propose_memory
from literature_radar.feedback.store import load_feedback
from literature_radar.models import Paper
from literature_radar.topics.manager import initialize
from literature_radar.workflow.multi_topic import run_bootstraps
from literature_radar.workflow.bootstrap import _date_bounds
from literature_radar.workflow.weekly import activate_weekly, run_weekly
from tests.test_stage37_39_workflow import Recorder, RichProvider, intake, record

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def fictional(index, prefix, *, shared=False):
    item = record(index, doi=f"10.0000/{prefix}-{index}")
    item["source_id"] = f"{prefix}-{index}"
    if prefix == "beta":
        item["title"] = f"Fictivus beta signal modulation {index} with spectral assay"
        item["abstract"] = "Fictivus beta signal modulation uses a spectral assay to compare invented states."
    if shared:
        item["source_id"] = "SHARED-1"
        item["doi"] = "10.0000/shared-1"
        item["title"] = "Imaginaryus alpha and Fictivus beta signal mapping and signal modulation by phase lattice assay and spectral assay"
        item["abstract"] = "Imaginaryus alpha and Fictivus beta signal mapping and signal modulation use phase lattice assay and spectral assay."
    return item


class Stage44Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config_a = initialize(self.root/"private", intake("fictional-alpha"))
        spec = intake("fictional-beta")
        spec.update(name="Fictional Beta", questions=["signal modulation"],
                    target_species=["Fictivus beta"], methods=["spectral assay"])
        self.config_b = initialize(self.root/"private", spec)
        self.db = self.root/"research.sqlite3"

    def test_two_independent_thirty_pools_shared_paper_and_feedback(self):
        a = [fictional(i, "alpha") for i in range(1, 30)] + [fictional(30, "alpha", shared=True)]
        b = [fictional(i, "beta") for i in range(1, 30)] + [fictional(30, "beta", shared=True)]
        result = run_bootstraps([self.config_a, self.config_b], self.db, self.root/"bootstrap",
                                provider_names=["openalex"], now=NOW,
                                providers_by_topic={"fictional-alpha":{"openalex":RichProvider("openalex", [a])},
                                                    "fictional-beta":{"openalex":RichProvider("openalex", [b])}})
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual([result["topics"][t]["selected"] for t in ("fictional-alpha", "fictional-beta")], [30, 30])
        store = AcademicStore(self.db)
        ids_a={p.paper_id for p in store.topic_papers("fictional-alpha")}
        ids_b={p.paper_id for p in store.topic_papers("fictional-beta")}
        self.assertEqual((len(ids_a),len(ids_b),len(ids_a & ids_b)),(30,30,1))
        reasons=[]
        for topic in ("fictional-alpha", "fictional-beta"):
            report=json.loads((self.root/"bootstrap"/topic/"bootstrap-report.json").read_text())
            shared=next(row for row in report["papers"] if row["doi"]=="10.0000/shared-1")
            self.assertEqual(shared["topic_id"],topic)
            reasons.append((shared["research_question"],shared["why_recommended"]))
        self.assertNotEqual(reasons[0],reasons[1])
        for topic in ("fictional-alpha", "fictional-beta"):
            cards=list((self.root/"bootstrap"/topic/"cards").rglob("*.md"))
            self.assertEqual(len(cards),30)
            self.assertTrue(all(frontmatter(p.read_text())["topic_id"]==p.parent.parent.name==topic for p in cards))
        shared_a=next(p for p in (self.root/"bootstrap/fictional-alpha/cards").rglob("*.md")
                      if frontmatter(p.read_text())["doi"]=="10.0000/shared-1")
        text=set_properties(shared_a.read_text(),{"feedback_decision":"must_read","review_status":"reviewed"})
        shared_a.write_text(text)
        first=import_cards(self.config_a,self.root/"bootstrap/fictional-alpha/cards",self.db)
        second=import_cards(self.config_a,self.root/"bootstrap/fictional-alpha/cards",self.db)
        self.assertEqual((first["imported"],second["imported"]),(1,0))
        shared_a.write_text(text + "\n补充说明：本方法很重要。\n")
        self.assertEqual(import_cards(self.config_a,self.root/"bootstrap/fictional-alpha/cards",self.db)["imported"],0)
        self.assertEqual(len(load_feedback(self.config_b/"feedback.json")["entries"]),0)
        self.assertEqual(load_feedback(self.config_a/"feedback.json")["entries"][0]["decision"],"must_read")
        self.assertEqual(propose_memory(self.config_a,self.config_a/"feedback.json",
                                        self.root/"memory-proposal.json")["status"],
                         "AWAITING_EXPLICIT_CONFIRMATION")
        self.assertEqual(json.loads((self.config_a/"attention-memory.json").read_text())["preferred_concept_ids"],[])
        shared_a.write_text(set_properties(text,{"feedback_decision":"keep","review_status":"reviewed"}))
        with self.assertRaisesRegex(ValueError,"conflicts with previously imported feedback"):
            import_cards(self.config_a,self.root/"bootstrap/fictional-alpha/cards",self.db)
        shared_a.write_text(set_properties(text,{"feedback_decision":"invalid-choice","review_status":"reviewed"}))
        with self.assertRaisesRegex(ValueError,"invalid or multiple feedback choices"):
            import_cards(self.config_a,self.root/"bootstrap/fictional-alpha/cards",self.db)

    def test_provider_failure_isolated_and_third_topic_supported(self):
        third = initialize(self.root/"private", intake("fictional-gamma"))
        good=[fictional(i,"alpha") for i in range(30)]
        result=run_bootstraps([self.config_a,self.config_b,third],self.db,self.root/"out",
                              provider_names=["openalex"],now=NOW,
                              providers_by_topic={"fictional-alpha":{"openalex":RichProvider("openalex",[good])},
                                                  "fictional-beta":{"openalex":RichProvider("openalex",[[]],fail_at=0)},
                                                  "fictional-gamma":{"openalex":RichProvider("openalex",[good])}})
        self.assertEqual(result["status"],"PARTIAL")
        self.assertEqual(result["topics"]["fictional-alpha"]["selected"],30)
        self.assertIn(result["topics"]["fictional-beta"]["status"],{"INCOMPLETE","FAILED"})
        self.assertEqual(result["topics"]["fictional-gamma"]["selected"],30)

    def test_multi_topic_cli_dry_runs_are_explicit_and_isolated(self):
        common=["--config-dir",str(self.config_a),"--config-dir",str(self.config_b),
                "--database",str(self.db),"--output-root",str(self.root/"multi"),
                "--providers","openalex","--dry-run"]
        stream=io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(cli_main(["bootstrap","run-many",*common]),0)
        self.assertEqual(set(json.loads(stream.getvalue())["topics"]),
                         {"fictional-alpha","fictional-beta"})
        for config in (self.config_a,self.config_b):
            activate_weekly(config,skip_bootstrap=True,reason="Fictional CLI dry-run scenario")
        stream=io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(cli_main(["weekly","run-many",*common]),0)
        self.assertEqual(set(json.loads(stream.getvalue())["topics"]),
                         {"fictional-alpha","fictional-beta"})

    def test_weekly_recommends_every_qualified_paper_and_preserves_cards(self):
        for count in (0,1,3,4,20):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                config=initialize(root/"private",intake("fictional-weekly"))
                activate_weekly(config,skip_bootstrap=True,reason="Fictional offline scenario")
                rows=[fictional(i,"alpha") for i in range(1,count+1)]
                report=run_weekly(config,root/"state.sqlite3",root/"report",now=NOW,
                                  provider_names=["openalex"],providers={"openalex":RichProvider("openalex",[rows])})
                self.assertEqual(report["recommended_count"],count)
                self.assertEqual(len(report["cards"]),count)
                self.assertEqual((root/"report/cards").exists(),count>0)
                self.assertIn(f"最终推荐：{count}",(root/"report/weekly-report.md").read_text())
                second=run_weekly(config,root/"state.sqlite3",root/"report",now=NOW,force=True,
                                  provider_names=["openalex"],providers={"openalex":RichProvider("openalex",[rows])})
                self.assertEqual(second["recommended_count"],0)
                self.assertEqual(len(list((root/"report/cards").rglob("*.md"))) if count else 0,count)

    def test_card_update_preserves_human_checkboxes(self):
        root=self.root/"cards"
        row={"paper_id":"SYN-1","doi":"10.0000/one","title":"Fictional study",
             "abstract":"Fictional abstract", "why_recommended":"Fictional method"}
        path=write_card(root,row,topic_id="fictional-alpha",topic_name="Fictional Alpha",
                        run_id="run-1",seen_on="2026-11-03",attention="MEDIUM")
        path.write_text(set_properties(path.read_text(),{"feedback_decision":"keep","review_status":"reviewed"}))
        again=write_card(root,row,topic_id="fictional-alpha",topic_name="Fictional Alpha",
                         run_id="run-2",seen_on="2026-11-10",attention="HIGH")
        self.assertEqual(path,again)
        self.assertEqual(len(list(root.rglob("*.md"))),1)
        self.assertEqual(frontmatter(again.read_text())["feedback_decision"],"keep")
        meta=frontmatter(again.read_text())
        self.assertEqual((meta["first_attention"],meta["current_attention"],meta["last_seen"]),
                         ("MEDIUM","HIGH","2026-11-10"))

    def test_journal_fields_do_not_change_weekly_results(self):
        reports=[]
        for journal, quartile, impact in (("Fictional Local", "Q4", 0.1),
                                          ("Fictional Famous", "Q1", 999.0)):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                config=initialize(root/"private",intake("fictional-weekly"))
                activate_weekly(config,skip_bootstrap=True,reason="Fictional offline scenario")
                rows=[dict(fictional(i,"alpha"),journal=journal,
                           jcr_quartile=quartile,impact_factor=impact) for i in range(1,5)]
                report=run_weekly(config,root/"state.sqlite3",root/"report",now=NOW,
                                  provider_names=["openalex"],providers={"openalex":RichProvider("openalex",[rows])})
                reports.append([(r["paper_key"],r["score"],r["priority"],r["relevance"])
                                for r in report["recommended"]])
        self.assertEqual(reports[0],reports[1])

    def test_bounded_question_evidence_and_partial_date_bounds(self):
        bundle=load_bundle(self.config_a)
        relevant=Paper("SYN-1","Signal and phase mapping with phase lattice assay",
                       "Signal and phase mapping uses a phase lattice assay to compare invented states.","fixture")
        unrelated=Paper("SYN-2","Signal and many unrelated words before phase mapping",
                        "The phase lattice assay compared invented states.","fixture")
        good,bad=recommend(relevant,bundle),recommend(unrelated,bundle)
        self.assertEqual(good["attention_ranking"]["category"],"C")
        self.assertEqual(bad["attention_ranking"]["category"],"D")
        self.assertEqual(tuple(str(x) for x in _date_bounds("2025")),("2025-01-01","2025-12-31"))
        self.assertEqual(tuple(str(x) for x in _date_bounds("2025-06")),("2025-06-01","2025-06-30"))

    def test_topic_entity_corroboration_rejects_unrelated_acronym(self):
        spec=intake("fictional-receptor")
        spec["search_terms"]=["Fictorin knockout transcriptome", "Fictorin olfactory behavior",
                              "Fictorin sensory compensation"]
        config=initialize(self.root/"private",spec)
        bundle=load_bundle(config)
        relevant=Paper("SYN-R","Fictorin silencing changes invented olfactory behavior",
                       "Fictorin silencing changes sensory compensation and olfactory behavior in the invented insect model.","fixture")
        irrelevant=Paper("SYN-U","FICTORIN: an unrelated classifier",
                         "FICTORIN is a numerical algorithm for classifying invented protein shifts and calculating probabilities across multiple simulated datasets and reference conditions.","fixture")
        self.assertEqual(recommend(relevant,bundle)["attention_ranking"]["category"],"C")
        self.assertEqual(recommend(irrelevant,bundle)["attention_ranking"]["category"],"D")

    def test_high_notification_is_scoped_to_topic_and_successfully_deduplicated(self):
        for config in (self.config_a,self.config_b):
            activate_weekly(config,skip_bootstrap=True,reason="Fictional offline scenario")
            delivery=json.loads((config/"delivery.json").read_text())
            delivery["notification"]={"enabled":True,"provider":"desktop"}
            (config/"delivery.json").write_text(json.dumps(delivery))
        shared=fictional(30,"alpha",shared=True)
        recorder=Recorder()
        for config in (self.config_a,self.config_b):
            report=run_weekly(config,self.db,self.root/"weekly"/config.name,now=NOW,
                              provider_names=["openalex"],providers={"openalex":RichProvider("openalex",[[shared]])},
                              notifiers={"desktop":recorder})
            self.assertEqual(report["recommended_count"],1)
            self.assertEqual(report["notification"]["sent"],1)
        self.assertEqual(len(recorder.sent),2)
        revised=dict(shared,abstract=shared["abstract"]+" Additional fictional validation changed the conclusion.")
        for config in (self.config_a,self.config_b):
            report=run_weekly(config,self.db,self.root/"weekly"/config.name,
                              now=datetime(2026,11,10,12,tzinfo=timezone.utc),
                              provider_names=["openalex"],providers={"openalex":RichProvider("openalex",[[revised]])},
                              notifiers={"desktop":recorder})
            self.assertEqual(report["notification"]["sent"],0)
        self.assertEqual(len(recorder.sent),2)
