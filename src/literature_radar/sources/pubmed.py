"""PubMed ESearch plus EFetch adapter; History Server is not a local checkpoint."""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import Any, Mapping

from literature_radar.providers.models import DateWindow, PageResult, QueryPlan, RawRecord, RequestMetadata
from literature_radar.sources.common import date_only, plan, source_record, terms, utcnow
from literature_radar.sources.http import HTTPClient, RequestFailure


def _text(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    text = "".join(node.itertext()).strip()
    return text or None


def _pub_date(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    year, month, day = (node.findtext(name) for name in ("Year", "Month", "Day"))
    if year and month and day:
        months = {name: index for index, name in enumerate(
            ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
        try:
            return f"{int(year):04d}-{int(month) if month.isdigit() else months[month[:3]]:02d}-{int(day):02d}"
        except (ValueError, KeyError):
            pass
    return None


class PubMedProvider:
    supports_semantic_groups = True
    provider_id = "pubmed"
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    sync_capability = "EDAT, MDAT or PDAT windows; EDAT is not publication date"

    def __init__(self, client: HTTPClient | None = None, *, page_size: int = 25):
        self.client = client or HTTPClient(self.provider_id, rate_per_second=2.5)
        self.page_size = min(max(page_size, 1), 200)

    def validate_config(self, config: Mapping[str, Any]) -> None:
        if config.get("date_field", "mdat") not in ("edat", "mdat", "pdat"):
            raise ValueError("pubmed date_field must be edat, mdat or pdat")

    def plan_queries(self, topic: Mapping[str, Any], focus: Mapping[str, Any],
                     date_window: DateWindow | None = None) -> tuple[QueryPlan, ...]:
        options = topic.get("extensions", {}).get("provider_options", {}).get(self.provider_id, {})
        self.validate_config(options)
        expression = " OR ".join(f'({term})' for term in terms(topic, focus))
        return (plan(self.provider_id, topic, focus,
                     {"term": expression, "date_field": options.get("date_field", "mdat"),
                      "page_size": self.page_size},
                     date_window),)

    @staticmethod
    def _common_params() -> dict[str, str | None]:
        return {"api_key": os.getenv("NCBI_API_KEY"), "tool": os.getenv("NCBI_TOOL") or "literature_radar",
                "email": os.getenv("NCBI_EMAIL")}

    def fetch_page(self, query: QueryPlan, cursor: Any = None) -> PageResult:
        offset = 0 if cursor is None else cursor
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("pubmed cursor must be a nonnegative offset")
        params = {**self._common_params(), "db": "pubmed", "term": query.query_parameters["term"],
                  "retmode": "json", "retstart": offset, "retmax": self.page_size,
                  "sort": "pub date"}
        window = query.date_window
        if window:
            params["datetype"] = query.query_parameters["date_field"]
            params["mindate"] = (window.start or "").replace("-", "/")
            params["maxdate"] = (window.end or "").replace("-", "/")
        search = self.client.get_json(self.base + "esearch.fcgi", params)
        result = search.get("esearchresult") if isinstance(search, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get("idlist"), list):
            raise RequestFailure(self.provider_id, "malformed_response")
        ids = result["idlist"]
        try:
            count = int(result.get("count", 0))
        except (TypeError, ValueError) as exc:
            raise RequestFailure(self.provider_id, "malformed_response") from exc
        if (count < 0 or len(ids) > self.page_size or
                any(not isinstance(item, str) or not item.isdecimal() for item in ids) or
                len(ids) != len(set(ids))):
            raise RequestFailure(self.provider_id, "malformed_response")
        if offset + len(ids) > count:
            raise RequestFailure(self.provider_id, "malformed_response")
        if not ids and offset < count:
            raise RequestFailure(self.provider_id, "incomplete_page")
        now = utcnow()
        rows = ()
        if ids:
            response = self.client.get(self.base + "efetch.fcgi", {
                **self._common_params(), "db": "pubmed", "id": ",".join(ids),
                "retmode": "xml"})
            try:
                xml = ET.fromstring(response.body)
            except ET.ParseError as exc:
                raise RequestFailure(self.provider_id, "invalid_xml") from exc
            articles = xml.findall(".//PubmedArticle") + xml.findall(".//PubmedBookArticle")
            rows = tuple(RawRecord(self.provider_id, {"xml": ET.tostring(item, encoding="unicode")}, now)
                         for item in articles)
            fetched_ids = [item.findtext("MedlineCitation/PMID") if item.tag == "PubmedArticle"
                           else item.findtext("BookDocument/PMID") for item in articles]
            if len(rows) != len(ids) or set(fetched_ids) != set(ids):
                raise RequestFailure(self.provider_id, "incomplete_fetch")
        more = offset + len(ids) < count and bool(ids)
        next_cursor = offset + len(ids) if more else None
        return PageResult(rows, next_cursor, more, RequestMetadata(self.provider_id, now))

    def normalize(self, raw: RawRecord):
        try:
            root = ET.fromstring(raw.payload["xml"])
        except (KeyError, ET.ParseError) as exc:
            raise RequestFailure(self.provider_id, "invalid_xml") from exc
        if root.tag == "PubmedBookArticle":
            document = root.find("BookDocument")
            if document is None:
                raise RequestFailure(self.provider_id, "malformed_record")
            pmid = document.findtext("PMID")
            identifiers = {"pmid": pmid}
            for item in root.findall("PubmedBookData/ArticleIdList/ArticleId"):
                if item.attrib.get("IdType") in ("doi", "pmc") and item.text:
                    identifiers["pmcid" if item.attrib["IdType"] == "pmc" else "doi"] = item.text
            authors = []
            for author in document.findall("AuthorList/Author"):
                name = " ".join(part for part in (author.findtext("ForeName"), author.findtext("LastName")) if part)
                if name: authors.append(name)
            abstract = " ".join(text for node in document.findall("Abstract/AbstractText")
                                if (text := _text(node))) or None
            fields = {"title": _text(document.find("ArticleTitle")) or _text(document.find("Book/BookTitle")),
                      "abstract": abstract, "authors": authors,
                      "journal": _text(document.find("Book/BookTitle")),
                      "publication_date": _pub_date(document.find("Book/PubDate")),
                      "online_date": None, "indexed_date": None,
                      "created_date": None, "updated_date": None,
                      "document_type": "book-chapter", "language": document.findtext("Language"),
                      "landing_url": None}
            return source_record(raw, source_id=pmid or "", fields=fields, identifiers=identifiers,
                                 source_fields={"title": "BookDocument.ArticleTitle",
                                                "abstract": "BookDocument.Abstract.AbstractText",
                                                "pmid": "BookDocument.PMID"})
        citation = root.find("MedlineCitation")
        if citation is None:
            raise RequestFailure(self.provider_id, "malformed_record")
        article = citation.find("Article")
        if article is None:
            raise RequestFailure(self.provider_id, "malformed_record")
        identifiers = {}
        # ReferenceList also contains ArticleId elements. Only IDs in this
        # article's PubmedData belong to the current paper.
        for item in root.findall("PubmedData/ArticleIdList/ArticleId"):
            kind = item.attrib.get("IdType")
            if kind in ("doi", "pmc", "pubmed") and item.text:
                identifiers[{"pmc": "pmcid", "pubmed": "pmid"}.get(kind, kind)] = item.text
        pmid = citation.findtext("PMID")
        if pmid: identifiers["pmid"] = pmid
        authors = []
        for author in article.findall(".//AuthorList/Author"):
            name = " ".join(part for part in (author.findtext("ForeName"), author.findtext("LastName")) if part)
            if name: authors.append(name)
        abstract = " ".join(text for node in article.findall(".//Abstract/AbstractText")
                            if (text := _text(node))) or None
        history = root.find("PubmedData/History")
        indexed = None
        if history is not None:
            for date in history.findall("PubMedPubDate"):
                if date.attrib.get("PubStatus") in ("entrez", "pubmed"):
                    indexed = _pub_date(date)
                    if indexed: break
        fields = {"title": _text(article.find("ArticleTitle")), "abstract": abstract,
                  "authors": authors, "journal": article.findtext("Journal/Title"),
                  "issn": article.findtext("Journal/ISSN"),
                  "issn_l": citation.findtext("MedlineJournalInfo/ISSNLinking"),
                  "publication_date": _pub_date(article.find("Journal/JournalIssue/PubDate")),
                  "online_date": _pub_date(article.find("ArticleDate")),
                  "indexed_date": indexed,
                  "created_date": _pub_date(citation.find("DateCreated")),
                  "updated_date": _pub_date(citation.find("DateRevised")),
                  "document_type": article.findtext("PublicationTypeList/PublicationType"),
                  "language": article.findtext("Language"),
                  "landing_url": None}
        return source_record(raw, source_id=pmid or "", fields=fields,
                             identifiers=identifiers,
                             source_fields={"title": "ArticleTitle", "abstract": "Abstract.AbstractText",
                                            "pmid": "MedlineCitation.PMID"})

    def fetch_by_identifier(self, identifier: str):
        response = self.client.get(self.base + "efetch.fcgi", {
            **self._common_params(), "db": "pubmed", "id": identifier, "retmode": "xml"})
        try:
            root = ET.fromstring(response.body)
            item = root.find(".//PubmedArticle")
            if item is None: return None
            return self.normalize(RawRecord(self.provider_id, {"xml": ET.tostring(item, encoding="unicode")}, utcnow()))
        except ET.ParseError as exc:
            raise RequestFailure(self.provider_id, "invalid_xml") from exc
