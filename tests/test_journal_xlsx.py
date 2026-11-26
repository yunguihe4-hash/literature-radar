"""Fictional spreadsheet import and ISSN-corroborated title matching."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from literature_radar.journal.registry import import_registry, load_registry, match_quality


HEADERS = ("issn", "issn_l", "journal_title", "aliases", "cas_edition_year",
           "cas_major_quartile", "cas_minor_quartile", "cas_major_category",
           "cas_minor_category", "cas_source", "jcr_edition_year", "jcr_categories",
           "license_or_usage_basis")
VALUES = ("1234-567X", "", "Journal of Fictional Systems", "", "2025", "1", "",
          "Synthetic Systems", "", "Fictional licensed dataset", "2025",
          '[{"category":"Synthetic Systems","quartile":"Q1"}]', "Fictional authorization")


def sheet(formula=False):
    def row(number, values):
        return '<row r="%s">%s</row>' % (number, ''.join(
            '<c r="%s%s" t="inlineStr"><is><t>%s</t></is></c>' %
            (chr(65 + index), number, value.replace('&', '&amp;').replace('<', '&lt;'))
            for index, value in enumerate(values)))
    second = row(2, VALUES)
    if formula:
        second = second.replace('<is><t>2025</t></is>', '<f>2024+1</f><v>2025</v>', 1)
    return ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>' + row(1, HEADERS) + second + '</sheetData></worksheet>')


class JournalXlsxTests(unittest.TestCase):
    def test_plain_xlsx_import_and_title_variation(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'fictional.xlsx'
            with zipfile.ZipFile(source, 'w') as book:
                book.writestr('xl/worksheets/sheet1.xml', sheet())
            destination = Path(temp) / 'registry.json'
            self.assertEqual(import_registry(source, destination), 1)
            quality = match_quality(load_registry(destination), issn=['1234-567X'],
                                    journal_title='Journal-of Fictional Systems')
            self.assertEqual(quality['combined'], 'DUAL_HIGH')

    def test_formula_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'fictional.xlsx'
            with zipfile.ZipFile(source, 'w') as book:
                book.writestr('xl/worksheets/sheet1.xml', sheet(formula=True))
            with self.assertRaisesRegex(ValueError, 'formulas'):
                import_registry(source, Path(temp) / 'registry.json')
