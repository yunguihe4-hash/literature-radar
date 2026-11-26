"""Old checkbox feedback migration uses only isolated fictional files."""
import tempfile
import unittest
from pathlib import Path

from literature_radar.candidates.processing import identity
from literature_radar.database.academic import AcademicStore
from literature_radar.discovery.incremental import discover_incremental
from literature_radar.feedback.legacy_cards import collect_legacy_cards
from literature_radar.feedback.store import load_feedback
from literature_radar.topics.manager import initialize
from tests.synthetic_provider import SyntheticProvider, fictional
from tests.test_stage37_39_workflow import intake


class LegacyFeedbackCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = initialize(self.root / "config", intake("fictional-alpha"))
        self.database = self.root / "academic.sqlite3"
        self.vault = self.root / "vault"
        provider = SyntheticProvider("openalex", [[fictional(f"paper-{i}",
            doi=f"10.0000/fictional-{i}") for i in range(4)]])
        discover_incremental(self.config, ["openalex"], database_path=self.database,
                             since="2026-01-01", until="2026-01-31",
                             providers={"openalex": provider})
        self.papers = AcademicStore(self.database).topic_papers("fictional-alpha")

    def card(self, paper, marker=None, conflict=False):
        key = identity(paper)
        path = self.vault / "Literature/2026-01-31/fictional-alpha/LOW" / f"{paper.paper_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        labels = ["important", "useful", "neutral", "irrelevant"]
        choices = [f"- [{'x' if label == marker or conflict and label == 'useful' else ' '}] "
                   f"fictional <!-- feedback:{label} -->" for label in labels]
        path.write_text("---\ntype: literature-review-card\n"
                        "topic_id: fictional-alpha\n"
                        f'paper_key: "{key}"\n'
                        f'doi: "{paper.doi}"\n'
                        'source_run: "fictional-run"\n---\n\n# Fictional title\n\n'
                        + "\n".join(choices) + "\n\n## 我的笔记\nkeep this\n")
        return path

    def test_four_markers_idempotence_change_and_conflict(self):
        markers = ["important", "useful", "neutral", "irrelevant"]
        cards = [self.card(paper, marker) for paper, marker in zip(self.papers, markers)]
        before = [path.read_bytes() for path in cards]
        first = collect_legacy_cards(self.config, self.database, self.vault,
                                     observed_at="2026-02-01T00:00:00Z")
        self.assertEqual(first["imported"], 4)
        self.assertEqual({entry["decision"] for entry in
                          load_feedback(self.config / "feedback.json")["entries"]},
                         {"must_read", "keep", "later", "discard"})
        second = collect_legacy_cards(self.config, self.database, self.vault)
        self.assertEqual((second["imported"], second["duplicates"]), (0, 4))
        self.assertEqual([path.read_bytes() for path in cards], before)
        cards[0].write_text(cards[0].read_text().replace("[x] fictional <!-- feedback:important -->",
                                                       "[ ] fictional <!-- feedback:important -->")
                            .replace("[ ] fictional <!-- feedback:useful -->",
                                     "[x] fictional <!-- feedback:useful -->"))
        changed = collect_legacy_cards(self.config, self.database, self.vault)
        self.assertEqual(changed["imported"], 1)
        self.assertEqual(len(load_feedback(self.config / "feedback.json")["entries"]), 5)
        cards[1].write_text(cards[1].read_text().replace("[ ] fictional <!-- feedback:important -->",
                                                       "[x] fictional <!-- feedback:important -->"))
        conflict = collect_legacy_cards(self.config, self.database, self.vault)
        self.assertEqual((conflict["conflicts"], conflict["imported"]), (1, 0))
