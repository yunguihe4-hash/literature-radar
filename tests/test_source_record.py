import json
import unittest

from literature_radar.models import FieldProvenance, Paper, SourceRecord


class SourceRecordTests(unittest.TestCase):
    def test_provenance_round_trip(self):
        provenance = FieldProvenance("synthetic-one", "SYN-1",
                                     "2026-01-01T00:00:00+00:00", "abstract")
        record = SourceRecord("synthetic-one", "SYN-1", "2026-01-01T00:00:00+00:00",
                              {"doi": "10.0000/fictional"},
                              {"title": "Fictional title", "abstract": None},
                              "2026-01-01", {"abstract": provenance})
        self.assertEqual(SourceRecord.from_dict(json.loads(json.dumps(record.as_dict()))), record)
        self.assertEqual(record.provenance["abstract"].source_field, "abstract")

    def test_two_source_records_attach_to_one_paper_without_merging(self):
        one = SourceRecord("synthetic-one", "SYN-A", "2026-01-01T00:00:00+00:00",
                           {"doi": "10.0000/fictional"}, {"title": "Imaginary study"})
        two = SourceRecord("synthetic-two", "SYN-B", "2026-01-01T00:00:00+00:00",
                           {"doi": "10.0000/fictional"}, {"title": "Imaginary study"})
        paper = Paper("SYN-A", "Imaginary study", None, "synthetic-one",
                      source_records=(one, two))
        restored = Paper.from_dict(paper.as_dict())
        self.assertEqual(len(restored.source_records), 2)
        self.assertEqual({item.provider_id for item in restored.source_records},
                         {"synthetic-one", "synthetic-two"})

    def test_missing_metadata_is_not_invented(self):
        record = SourceRecord("synthetic-one", "SYN-C", "2026-01-01T00:00:00+00:00")
        self.assertEqual(record.normalized_metadata, {})
        self.assertIsNone(record.source_updated_at)
        self.assertEqual(record.retrieved_at, "2026-01-01T00:00:00+00:00")

    def test_invalid_source_record_rejected(self):
        with self.assertRaisesRegex(ValueError, "provider_id"):
            SourceRecord("", "SYN-D", "2026-01-01T00:00:00+00:00")
