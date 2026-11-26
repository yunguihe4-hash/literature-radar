import unittest
from literature_radar.candidates.identity import IdentityConflict, identifiers, merge, normalize_doi
from literature_radar.models import FieldProvenance, SourceRecord

def record(provider, source, ids, title="Imaginary paper", abstract=None, published=None):
    stamp = "2026-01-01T00:00:00+00:00"
    fields = {"title": title, "abstract": abstract, "authors": ["Fictional Author"],
              "publication_date": published}
    provenance = {key: FieldProvenance(provider, source, stamp, key) for key in fields if fields[key] is not None}
    return SourceRecord(provider, source, stamp, ids, fields, None, provenance)

class IdentityTests(unittest.TestCase):
    def test_doi_variants_and_multiple_identifiers(self):
        first = record("crossref", "c1", {"doi": " DOI:10.0000/FICTIONAL. ", "pmid": "123"})
        second = record("openalex", "o1", {"doi": "https://doi.org/10.0000/fictional", "openalex_id": "https://openalex.org/W42"})
        self.assertEqual(normalize_doi(first.identifiers["doi"]), "10.0000/fictional")
        self.assertEqual(normalize_doi(" http://dx.doi.org/10.0000%2FTwo) "), "10.0000/two")
        paper = merge(None, first, "paper-1")
        paper = merge(paper, second, "paper-1")
        self.assertEqual(paper.identifiers["openalex_id"], "W42")
        self.assertEqual(paper.identifiers["pmid"], "123")
        self.assertEqual(len(paper.source_records), 2)

    def test_conflicting_doi_does_not_merge_by_title(self):
        paper = merge(None, record("crossref", "c1", {"doi": "10.0000/one"}), "p1")
        with self.assertRaises(IdentityConflict):
            merge(paper, record("pubmed", "p1", {"doi": "10.0000/two"}), "p1")
        self.assertIsNone(normalize_doi("not-a-doi"))
        self.assertEqual(identifiers(record("europepmc", "e1", {"pmcid": "pmc123"})), {"pmcid": "PMC123"})
        self.assertEqual(identifiers(record("openalex", "o1", {"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC123/"})), {"pmcid": "PMC123"})

    def test_field_priority_and_nonempty(self):
        paper = merge(None, record("crossref", "c1", {"doi": "10.0000/one"}, abstract="First"), "p1")
        paper = merge(paper, record("europepmc", "e1", {"doi": "10.0000/one"}, abstract=None), "p1",
                      {"abstract": ["europepmc", "crossref"]})
        self.assertEqual(paper.abstract, "First")
        paper = merge(paper, record("europepmc", "e1", {"doi": "10.0000/one"}, abstract="Preferred"), "p1",
                      {"abstract": ["europepmc", "crossref"]})
        self.assertEqual(paper.abstract, "Preferred")
        self.assertEqual(paper.provenance["abstract"].provider_id, "europepmc")

    def test_same_source_content_update_replaces_stale_fields(self):
        first = record("crossref", "c1", {"doi": "10.0000/one"},
                       title="Imaginary first title", abstract="Fictional first abstract",
                       published="2026-01-01")
        revised = record("crossref", "c1", {"doi": "10.0000/one"},
                         title="Imaginary revised title", abstract="Fictional revised abstract",
                         published="2026-01-02")
        paper = merge(merge(None, first, "p1"), revised, "p1")
        self.assertEqual(paper.title, "Imaginary revised title")
        self.assertEqual(paper.abstract, "Fictional revised abstract")
        self.assertEqual(paper.publication_date, "2026-01-02")
        self.assertEqual(paper.provenance["title"].source_id, "c1")

    def test_preprint_newer_version_updates_same_provider_fields(self):
        first = record("biorxiv", "10.0000/preprint#v1", {"doi": "10.0000/preprint"},
                       abstract="Fictional v1")
        second = record("biorxiv", "10.0000/preprint#v2", {"doi": "10.0000/preprint"},
                        abstract="Fictional v2")
        first = SourceRecord(first.provider_id, first.source_id, first.retrieved_at,
                             first.identifiers, {**first.normalized_metadata, "version": "1"},
                             first.source_updated_at, first.provenance)
        second = SourceRecord(second.provider_id, second.source_id, second.retrieved_at,
                              second.identifiers, {**second.normalized_metadata, "version": "2"},
                              second.source_updated_at, second.provenance)
        paper = merge(merge(None, first, "p1"), second, "p1")
        self.assertEqual(paper.abstract, "Fictional v2")
        self.assertEqual(len(paper.source_records), 2)

    def test_conflicting_strong_identifier_is_reviewed(self):
        first = record("crossref", "c1", {"doi": "10.0000/one", "pmid": "123"})
        second = record("europepmc", "e1", {"doi": "10.0000/one", "pmid": "456"})
        with self.assertRaises(IdentityConflict):
            merge(merge(None, first, "p1"), second, "p1")
