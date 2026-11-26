import unittest
from urllib.parse import parse_qs, urlsplit
from literature_radar.providers.models import DateWindow
from literature_radar.sources.http import HTTPResponse, RequestFailure
from literature_radar.sources.pubmed import PubMedProvider
from tests.mock_api import client

TOPIC = {"id": "synthetic-topic", "search_terms": ["imaginary"]}
FOCUS = {"version": "v001", "search_terms": []}
XML = b"""<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID>
<DateCreated><Year>2026</Year><Month>01</Month><Day>02</Day></DateCreated>
<Article><ArticleTitle>Imaginary paper</ArticleTitle><Abstract><AbstractText>Fictional abstract</AbstractText></Abstract>
<AuthorList><Author><ForeName>Fictional</ForeName><LastName>Author</LastName></Author></AuthorList>
<Journal><Title>Synthetic Journal</Title><JournalIssue><PubDate><Year>2026</Year><Month>Jan</Month><Day>01</Day></PubDate></JournalIssue></Journal>
<ArticleDate><Year>2026</Year><Month>01</Month><Day>03</Day></ArticleDate></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">10.0000/fictional</ArticleId><ArticleId IdType="pmc">PMC123</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>"""
BOOK_XML = b"""<PubmedArticleSet><PubmedBookArticle><BookDocument><PMID>456</PMID>
<ArticleTitle>Imaginary acoustic chapter</ArticleTitle>
<Abstract><AbstractText>Fictional chapter abstract</AbstractText></Abstract>
<AuthorList><Author><ForeName>Fictional</ForeName><LastName>Scholar</LastName></Author></AuthorList>
<Book><BookTitle>Synthetic methods book</BookTitle><PubDate><Year>2026</Year><Month>01</Month><Day>04</Day></PubDate></Book>
</BookDocument><PubmedBookData><ArticleIdList><ArticleId IdType="doi">10.0000/fictional-book</ArticleId></ArticleIdList></PubmedBookData>
</PubmedBookArticle></PubmedArticleSet>"""

class PubMedTests(unittest.TestCase):
    def test_search_fetch_mapping_and_dates(self):
        http, fake = client("pubmed", [{"esearchresult": {"count": "2", "idlist": ["123"]}},
                                       HTTPResponse(200, {}, XML),
                                       {"esearchresult": {"count": "1", "idlist": []}}])
        provider = PubMedProvider(http, page_size=1)
        query = provider.plan_queries(TOPIC, FOCUS, DateWindow("2026-01-01", "2026-01-31"))[0]
        page = provider.fetch_page(query)
        self.assertTrue(page.has_more)
        record = provider.normalize(page.records[0])
        self.assertEqual(record.identifiers["pmid"], "123")
        self.assertEqual(record.identifiers["pmcid"], "PMC123")
        self.assertEqual(record.normalized_metadata["publication_date"], "2026-01-01")
        self.assertEqual(record.normalized_metadata["online_date"], "2026-01-03")
        self.assertEqual(record.provenance["abstract"].provider_id, "pubmed")
        self.assertIn("datetype=mdat", fake.calls[0][0])
        self.assertFalse(provider.fetch_page(query, page.next_cursor).has_more)
        first_params = parse_qs(urlsplit(fake.calls[0][0]).query)
        second_params = parse_qs(urlsplit(fake.calls[2][0]).query)
        self.assertNotEqual(first_params.pop("retstart"), second_params.pop("retstart"))
        self.assertEqual(first_params, second_params)

    def test_missing_fields_and_errors(self):
        provider = PubMedProvider()
        query = provider.plan_queries(TOPIC, FOCUS)[0]
        for response, category in ((HTTPResponse(429, {}, b""), "rate_limited"),
                                   (HTTPResponse(500, {}, b""), "server_error"),
                                   (HTTPResponse(200, {}, b"bad"), "invalid_json"),
                                   (TimeoutError(), "timeout")):
            with self.subTest(category=category):
                http, _ = client("pubmed", [response])
                with self.assertRaises(RequestFailure) as raised:
                    PubMedProvider(http).fetch_page(query)
                self.assertEqual(raised.exception.category, category)
        http, _ = client("pubmed", [{"esearchresult": {"count": "1", "idlist": ["123"]}},
                                    HTTPResponse(200, {}, b"<bad")])
        with self.assertRaisesRegex(RequestFailure, "invalid_xml"):
            PubMedProvider(http).fetch_page(query)
        empty_xml = XML.replace(b"<Abstract><AbstractText>Fictional abstract</AbstractText></Abstract>", b"")
        raw = type("R", (), {"provider_id": "pubmed", "payload": {"xml": empty_xml.decode().replace("<PubmedArticleSet>", "").replace("</PubmedArticleSet>", "")},
                             "retrieved_at": "2026-01-01T00:00:00+00:00"})()
        self.assertIsNone(provider.normalize(raw).normalized_metadata["abstract"])

    def test_incomplete_efetch_does_not_return_a_partial_page(self):
        http, _ = client("pubmed", [
            {"esearchresult": {"count": "2", "idlist": ["123", "456"]}},
            HTTPResponse(200, {}, XML),
        ])
        query = PubMedProvider().plan_queries(TOPIC, FOCUS)[0]
        with self.assertRaises(RequestFailure) as raised:
            PubMedProvider(http).fetch_page(query)
        self.assertEqual(raised.exception.category, "incomplete_fetch")

    def test_empty_esearch_page_before_total_does_not_finish_checkpoint(self):
        http, _ = client("pubmed", [{"esearchresult": {"count": "3", "idlist": []}}])
        query = PubMedProvider(page_size=1).plan_queries(TOPIC, FOCUS)[0]
        with self.assertRaises(RequestFailure) as raised:
            PubMedProvider(http, page_size=1).fetch_page(query, 1)
        self.assertEqual(raised.exception.category, "incomplete_page")

    def test_wrong_efetch_identity_does_not_commit_page(self):
        http, _ = client("pubmed", [
            {"esearchresult": {"count": "1", "idlist": ["456"]}},
            HTTPResponse(200, {}, XML),
        ])
        query = PubMedProvider().plan_queries(TOPIC, FOCUS)[0]
        with self.assertRaises(RequestFailure) as raised:
            PubMedProvider(http).fetch_page(query)
        self.assertEqual(raised.exception.category, "incomplete_fetch")

    def test_book_article_counts_and_normalizes(self):
        http, _ = client("pubmed", [
            {"esearchresult": {"count": "1", "idlist": ["456"]}},
            HTTPResponse(200, {}, BOOK_XML),
        ])
        provider = PubMedProvider(http)
        page = provider.fetch_page(provider.plan_queries(TOPIC, FOCUS)[0])
        self.assertEqual(len(page.records), 1)
        record = provider.normalize(page.records[0])
        self.assertEqual(record.identifiers["pmid"], "456")
        self.assertEqual(record.identifiers["doi"], "10.0000/fictional-book")
        self.assertEqual(record.normalized_metadata["publication_date"], "2026-01-04")
        self.assertEqual(record.normalized_metadata["document_type"], "book-chapter")

    def test_reference_doi_cannot_replace_current_article_identity(self):
        from literature_radar.providers.models import RawRecord
        xml = XML.decode().replace(
            "</PubmedData>",
            "<ReferenceList><Reference><ArticleIdList>"
            "<ArticleId IdType=\"doi\">10.0000/unrelated-reference</ArticleId>"
            "<ArticleId IdType=\"pmc\">PMC999</ArticleId>"
            "</ArticleIdList></Reference></ReferenceList></PubmedData>")
        article = xml.replace("<PubmedArticleSet>", "").replace("</PubmedArticleSet>", "")
        result = PubMedProvider().normalize(RawRecord("pubmed", {"xml": article}, "2026-01-01T00:00:00+00:00"))
        self.assertEqual(result.identifiers["doi"], "10.0000/fictional")
        self.assertEqual(result.identifiers["pmcid"], "PMC123")
