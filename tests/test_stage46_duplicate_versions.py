"""Different DOI versions keep identity while producing one Topic reminder."""
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from literature_radar.models import Paper
from literature_radar.topics.manager import initialize
from literature_radar.workflow.weekly import _canonical_versions, activate_weekly, run_weekly
from tests.test_stage37_39_workflow import RichProvider, intake, record


class AuthorProvider(RichProvider):
    def normalize(self, raw):
        value = super().normalize(raw)
        fields = {**value.normalized_metadata,
                  "authors": ["Fictional Researcher"],
                  "document_type": raw.payload["document_type"]}
        return replace(value, normalized_metadata=fields)


class VersionAuthorProvider(RichProvider):
    def normalize(self, raw):
        value = super().normalize(raw)
        fields = {**value.normalized_metadata,
                  'authors': raw.payload['authors'],
                  'preprint_source': raw.payload.get('preprint_source')}
        return replace(value, normalized_metadata=fields)


class DuplicateVersionTests(unittest.TestCase):
    def test_canonical_version_prefers_published_article(self):
        title = "Fictional signal mapping with phase lattice assay in an imaginary insect"
        pre = Paper("pre", title, "Fictional evidence", "fixture", paper_id="pre",
                    doi="10.0000/fictional-pre", authors=("Fictional Researcher",),
                    document_type="posted-content", publication_date="2026-11-01")
        published = Paper("pub", title, "Fictional evidence", "fixture", paper_id="pub",
                          doi="10.0000/fictional-pub", authors=("Fictional Researcher",),
                          document_type="journal-article", publication_date="2026-11-02")
        self.assertEqual(_canonical_versions([pre, published])["pre"], "pub")

    def test_weekly_suppresses_second_version_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = initialize(root / "config", intake("fictional-versions"))
            activate_weekly(config, skip_bootstrap=True, reason="Fictional test")
            first = record(1, date="2026-11-01", doi="10.0000/fictional-pre")
            first["document_type"] = "posted-content"
            second = {**first, "source_id": "SYN-PUBLISHED",
                      "doi": "10.0000/fictional-pub", "document_type": "journal-article"}
            result = run_weekly(config, root / "state.sqlite3", root / "output",
                                provider_names=["openalex"],
                                providers={"openalex": AuthorProvider("openalex", [[first, second]])},
                                now=datetime(2026, 11, 6, 12, tzinfo=timezone.utc))
            self.assertEqual(result["duplicate_suppressed_count"], 1)
            self.assertEqual(result["recommended_count"], 1)
            self.assertEqual(len(result["cards"]), 1)

    def test_old_preprint_with_reworded_formal_title_stays_old(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = initialize(root / 'config', intake('fictional-republication'))
            activate_weekly(config, skip_bootstrap=True, reason='Fictional test')
            title = 'Imaginaryus alpha signal mapping with phase lattice assay in an imaginary insect'
            old = record(1, date='2026-02-03', doi='10.0000/fictional-old-preprint')
            old.update(source_id='PPR:SYN', title=title,
                       authors=['Researcher AJ', 'Colleague EC'],
                       preprint_source='PPR', document_type=None)
            formal = record(2, date='2026-11-03', doi='10.0000/fictional-new-formal')
            formal.update(source_id='SYN-FORMAL',
                          title='Imaginaryus alpha signal mapping with repeated phase lattice assay in an imaginary insect',
                          authors=['Andrew J. Researcher', 'Erin C. Colleague'],
                          document_type='journal-article')
            result = run_weekly(config, root / 'state.sqlite3', root / 'output',
                                provider_names=['openalex'],
                                providers={'openalex': VersionAuthorProvider('openalex', [[old, formal]])},
                                now=datetime(2026, 11, 6, 12, tzinfo=timezone.utc))
            self.assertEqual(result['duplicate_suppressed_count'], 1)
            self.assertEqual(result['recommended_count'], 0)
            row = next(item for item in result['events'] if item['doi'] == formal['doi'])
            self.assertEqual((row['date'], row['date_source'], row['event']),
                             ('2026-02-03', 'linked_preprint_first_release', 'LATE_INDEXED'))

    def test_similar_titles_with_different_second_authors_do_not_merge(self):
        old = Paper('pre', 'Signal mapping with phase lattice assay in an imaginary insect',
                    'Fictional old evidence', 'fixture', paper_id='pre',
                    authors=('Researcher AJ', 'Colleague EC'),
                    document_type='posted-content', publication_date='2026-02-03')
        new = Paper('pub', 'Signal mapping with repeated phase lattice assay in an imaginary insect',
                    'Fictional new evidence', 'fixture', paper_id='pub',
                    authors=('Andrew J. Researcher', 'Other E. Investigator'),
                    document_type='journal-article', publication_date='2026-11-03')
        self.assertNotIn('pre', _canonical_versions([old, new]))


if __name__ == "__main__":
    unittest.main()
