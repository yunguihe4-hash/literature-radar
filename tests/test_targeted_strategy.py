"""Fictional strategy tests; no private research configuration is embedded."""
from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from literature_radar.database.academic import AcademicStore
from literature_radar.providers.models import PageResult, RawRecord, RequestMetadata
from literature_radar.sources.common import source_record
from literature_radar.targeted import annual_windows, count_queries, query_plans, validate_strategy
from literature_radar.sources.http import RequestFailure


SYNTHETIC = {
    "id": "fictional-v1", "topic_id": "synthetic-acoustic", "window_start": "2021-11-04",
    "window_end": "2026-11-04", "groups": [
        {"id": "acoustic-method", "clauses": [["fictional moth", "imaginary beetle"],
                                              ["acoustic mapping", "sound phasing"]]}
    ]
}


class TargetedStrategyTests(unittest.TestCase):
    def test_annual_windows_cover_without_overlap(self):
        windows = annual_windows("2021-11-04", "2026-11-04")
        self.assertEqual(len(windows), 5)
        self.assertEqual(windows[0].start, "2021-11-04")
        self.assertEqual(windows[-1].end, "2026-11-04")
        self.assertTrue(all(date.fromisoformat(left.end) + timedelta(days=1) == date.fromisoformat(right.start)
                            for left, right in zip(windows, windows[1:])))

    def test_grouped_provider_syntax_and_isolated_signatures(self):
        validate_strategy(SYNTHETIC)
        plans = {provider: query_plans(SYNTHETIC, provider) for provider in
                 ("openalex", "europepmc", "pubmed")}
        self.assertTrue(all(len(value) == 5 for value in plans.values()))
        self.assertIn(' AND ', plans['openalex'][0].query_parameters['search'])
        self.assertEqual(plans['openalex'][0].query_parameters['search_field'], 'search.title_and_abstract')
        self.assertIn('TITLE_ABS:', plans['europepmc'][0].query_parameters['query'])
        self.assertIn('[tiab]', plans['pubmed'][0].query_parameters['term'])
        self.assertEqual(plans['europepmc'][0].query_parameters['date_field'], 'FIRST_PDATE')
        self.assertEqual(len({p.query_signature for values in plans.values() for p in values}), 15)

    def test_reject_single_broad_or_clause(self):
        value = copy.deepcopy(SYNTHETIC)
        value['groups'][0]['clauses'] = [["moth", "beetle"]]
        with self.assertRaisesRegex(ValueError, "two AND"):
            validate_strategy(value)

    def test_count_guard_marks_oversized_query(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('literature_radar.targeted._count', return_value=20001):
                rows = count_queries(SYNTHETIC, Path(temp) / 'counts.tsv', ('europepmc',))
            self.assertEqual(len(rows), 5)
            self.assertTrue(all(row['status'] == 'TOO_BROAD' for row in rows))

    def test_count_stops_provider_after_rate_limit(self):
        strategy = copy.deepcopy(SYNTHETIC)
        strategy['groups'].append({'id': 'another-question', 'clauses': [['fictional moth'], ['sonar']]})
        class Client:
            last_headers = {'retry-after': '3600'}
            @staticmethod
            def _retry_after(value): return float(value)
        class Provider:
            client = Client()
        with tempfile.TemporaryDirectory() as temp:
            with patch('literature_radar.targeted.make_provider', return_value=Provider()), \
                 patch('literature_radar.targeted._count', side_effect=RequestFailure('openalex', 'retry_later', 429)) as count:
                rows = count_queries(strategy, Path(temp) / 'counts.tsv', ('openalex',))
        self.assertEqual(count.call_count, 1)
        self.assertEqual(Provider.client.max_retries, 0)
        self.assertEqual(rows[0]['status'], 'COUNT_FAILED')
        self.assertIn('retry_after_until=', rows[0]['reason'])
        self.assertTrue(all(row['status'] == 'NOT_ATTEMPTED' for row in rows[1:]))

    def test_source_query_provenance_is_atomic_and_checkpoint_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AcademicStore(Path(temp) / 'fictional.sqlite3')
            first, second = query_plans(SYNTHETIC, 'europepmc')[:2]
            raw = RawRecord('europepmc', {'id': 'SYNTHETIC:1'}, '2026-11-04T00:00:00Z')
            record = source_record(raw, source_id='SYNTHETIC:1',
                                   fields={'title': 'Fictional moth acoustic mapping', 'abstract': 'Synthetic.'},
                                   identifiers={'doi': '10.0000/synthetic.1'})
            page = PageResult((raw,), None, False, RequestMetadata('europepmc', raw.retrieved_at))
            store.commit_page('synthetic-acoustic', first, page, [record])
            store.commit_page('synthetic-acoustic', second, page, [record])
            with closing(sqlite3.connect(store.path)) as db:
                self.assertEqual(db.execute('select count(*) from source_records').fetchone()[0], 1)
                self.assertEqual(db.execute('select count(*) from source_query_provenance').fetchone()[0], 2)
                self.assertEqual(db.execute('select count(*) from provider_checkpoints').fetchone()[0], 2)
                self.assertEqual(db.execute('pragma foreign_key_check').fetchall(), [])
