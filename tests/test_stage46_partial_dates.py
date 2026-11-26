"""Real Provider date precision must survive normalization without invalid days."""
import unittest

from literature_radar.models import Paper
from literature_radar.sources.common import date_parts


class PartialDateTests(unittest.TestCase):
    def test_year_and_month_precision_are_valid_paper_dates(self):
        for value in ("2026", "2026-11", "2026-11-06"):
            with self.subTest(value=value):
                self.assertEqual(Paper("fictional-id", "Fictional title", source="fixture",
                                       publication_date=value).publication_date, value)

    def test_crossref_impossible_calendar_day_is_not_persisted(self):
        self.assertIsNone(date_parts({"date-parts": [[2026, 2, 30]]}))
        self.assertEqual(date_parts({"date-parts": [[2026, 2]]}), "2026-02")


if __name__ == "__main__":
    unittest.main()
