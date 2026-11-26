"""Validate and read licensed CAS/JCR editions without fetching data."""
from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from literature_radar.private_paths import ensure_private_path


QUALITY = ("DUAL_HIGH", "DUAL_UPPER", "SINGLE_SYSTEM_HIGH", "VERIFIED_OTHER", "MIXED", "UNVERIFIED")
_ISSN = re.compile(r"^\d{4}-\d{3}[\dX]$", re.I)
_QUARTILES = {"Q1", "Q2", "Q3", "Q4"}
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _title_key(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _xlsx_rows(path: Path) -> list[dict[str, str]]:
    """Read a plain data worksheet without requiring an office package."""
    try:
        with zipfile.ZipFile(path) as book:
            names = set(book.namelist())
            if "xl/worksheets/sheet1.xml" not in names:
                raise ValueError("XLSX needs its import table in the first worksheet")
            shared = []
            if "xl/sharedStrings.xml" in names:
                strings = ET.fromstring(book.read("xl/sharedStrings.xml"))
                shared = ["".join(node.itertext()) for node in strings.findall(f"{_SHEET_NS}si")]
            sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValueError("Invalid XLSX file") from exc
    table = []
    for row in sheet.findall(f".//{_SHEET_NS}sheetData/{_SHEET_NS}row"):
        cells = {}
        for cell in row.findall(f"{_SHEET_NS}c"):
            address = cell.get("r", "")
            letters = re.match(r"[A-Z]+", address)
            if not letters:
                raise ValueError("XLSX cell lacks an address")
            column = 0
            for letter in letters.group():
                column = column * 26 + ord(letter) - 64
            if cell.find(f"{_SHEET_NS}f") is not None:
                raise ValueError("XLSX formulas are not accepted in journal imports")
            value = cell.find(f"{_SHEET_NS}v")
            inline = cell.find(f"{_SHEET_NS}is")
            text = "".join(inline.itertext()) if inline is not None else value.text if value is not None else ""
            if cell.get("t") == "s":
                try:
                    text = shared[int(text)]
                except (ValueError, IndexError) as exc:
                    raise ValueError("Invalid XLSX shared string index") from exc
            cells[column - 1] = text or ""
        if cells:
            table.append(cells)
    if not table:
        return []
    headers = {index: name.strip() for index, name in table[0].items()}
    if len(set(headers.values())) != len(headers) or not all(headers.values()):
        raise ValueError("XLSX column names must be unique and nonempty")
    return [{name: row.get(index, "") for index, name in headers.items()} for row in table[1:]]


def empty_quality(reason: str = "No licensed journal quality data") -> dict:
    return {"cas": {"status": "未核实", "edition_year": None, "major_quartile": None,
                    "minor_quartile": None, "major_category": None, "minor_category": None,
                    "source": None},
            "jcr": {"status": "未核实", "edition_year": None, "categories": []},
            "combined": "UNVERIFIED", "identity_status": "UNVERIFIED",
            "explanation": reason}


def _year(value, field: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not str(value).isdigit() or not 1900 <= int(value) <= 2200:
        raise ValueError(f"{field} must be a four-digit edition year")
    return int(value)


def _cas_quartile(value, field: str) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).upper().removeprefix("Q")
    if text not in ("1", "2", "3", "4"):
        raise ValueError(f"{field} must be 1, 2, 3 or 4")
    return int(text)


def validate_entry(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Journal entry must be an object")
    issn = value.get("issn") or []
    if isinstance(issn, str):
        issn = [part.strip() for part in issn.split(";") if part.strip()]
    if not isinstance(issn, list) or any(not isinstance(item, str) or not _ISSN.fullmatch(item) for item in issn):
        raise ValueError("issn must be an array of valid ISSNs")
    issn = list(dict.fromkeys(item.upper() for item in issn))
    issn_l = value.get("issn_l") or None
    if issn_l is not None and (not isinstance(issn_l, str) or not _ISSN.fullmatch(issn_l)):
        raise ValueError("issn_l must be a valid ISSN")
    title = value.get("journal_title") or None
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise ValueError("journal_title must be a nonempty string")
    aliases = value.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [part.strip() for part in aliases.split(";") if part.strip()]
    if not isinstance(aliases, list) or any(not isinstance(item, str) or not item.strip() for item in aliases):
        raise ValueError("aliases must be an array of titles")
    if not issn and not issn_l:
        raise ValueError("A verified journal entry requires ISSN or ISSN-L")
    cas = value.get("cas") or {}
    jcr = value.get("jcr") or {}
    if not isinstance(cas, dict) or not isinstance(jcr, dict):
        raise ValueError("cas and jcr must be objects")
    cas_out = {"edition_year": _year(cas.get("edition_year"), "cas.edition_year"),
               "major_quartile": _cas_quartile(cas.get("major_quartile"), "cas.major_quartile"),
               "minor_quartile": _cas_quartile(cas.get("minor_quartile"), "cas.minor_quartile"),
               "major_category": cas.get("major_category") or None,
               "minor_category": cas.get("minor_category") or None,
               "source": cas.get("source") or None}
    categories = jcr.get("categories") or []
    if isinstance(categories, str):
        categories = json.loads(categories) if categories else []
    if not isinstance(categories, list):
        raise ValueError("jcr.categories must be an array")
    for category in categories:
        if not isinstance(category, dict) or not isinstance(category.get("category"), str) or not category["category"].strip() or category.get("quartile") not in _QUARTILES:
            raise ValueError("Each JCR category needs a name and Q1-Q4 quartile")
    jcr_out = {"edition_year": _year(jcr.get("edition_year"), "jcr.edition_year"),
               "categories": categories}
    cas_present = any(cas_out[key] is not None for key in ("major_quartile", "minor_quartile"))
    jcr_present = bool(categories)
    if cas_present and (cas_out["edition_year"] is None or not cas_out["source"]):
        raise ValueError("CAS values require edition_year and source")
    if cas_out["major_quartile"] is not None and not cas_out["major_category"]:
        raise ValueError("CAS major partition requires major_category")
    if cas_out["minor_quartile"] is not None and not cas_out["minor_category"]:
        raise ValueError("CAS minor partition requires minor_category")
    if jcr_present and jcr_out["edition_year"] is None:
        raise ValueError("JCR categories require edition_year")
    basis = value.get("license_or_usage_basis") or None
    if (cas_present or jcr_present) and (not isinstance(basis, str) or not basis.strip()):
        raise ValueError("Verified partitions require license_or_usage_basis")
    if not cas_present and not jcr_present:
        raise ValueError("An imported entry needs at least one verified partition")
    return {"issn": issn, "issn_l": issn_l.upper() if issn_l else None,
            "journal_title": title.strip() if title else None,
            "aliases": list(dict.fromkeys(item.strip() for item in aliases)),
            "cas": cas_out, "jcr": jcr_out, "license_or_usage_basis": basis.strip()}


def _read_input(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        return value.get("entries", []) if isinstance(value, dict) else value
    if path.suffix.lower() in (".csv", ".xlsx"):
        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        else:
            rows = _xlsx_rows(path)
        return [{"issn": row.get("issn", ""), "issn_l": row.get("issn_l"),
                 "journal_title": row.get("journal_title"), "aliases": row.get("aliases", ""),
                 "cas": {"edition_year": row.get("cas_edition_year"),
                         "major_quartile": row.get("cas_major_quartile"),
                         "minor_quartile": row.get("cas_minor_quartile"),
                         "major_category": row.get("cas_major_category"),
                         "minor_category": row.get("cas_minor_category"),
                         "source": row.get("cas_source")},
                 "jcr": {"edition_year": row.get("jcr_edition_year"),
                         "categories": row.get("jcr_categories", "")},
                 "license_or_usage_basis": row.get("license_or_usage_basis")}
                for row in rows]
    raise ValueError("Journal import must be CSV, XLSX or JSON")


def load_registry(path: Path | None) -> list[dict]:
    if path is None or not Path(path).exists():
        return []
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("entries"), list):
        raise ValueError("Invalid journal registry")
    return [validate_entry(entry) for entry in value["entries"]]


def import_registry(source: Path, destination: Path) -> int:
    entries = _read_input(Path(source))
    if not isinstance(entries, list) or not entries:
        raise ValueError("Journal import has no entries")
    additions = [validate_entry(entry) for entry in entries]
    destination = ensure_private_path(destination)
    existing = load_registry(destination)
    all_entries = existing + additions
    for index, item in enumerate(all_entries):
        identifiers = set(item["issn"]) | ({item["issn_l"]} if item["issn_l"] else set())
        editions = {item["cas"]["edition_year"], item["jcr"]["edition_year"]} - {None}
        for prior in all_entries[:index]:
            prior_identifiers = set(prior["issn"]) | ({prior["issn_l"]} if prior["issn_l"] else set())
            prior_editions = {prior["cas"]["edition_year"], prior["jcr"]["edition_year"]} - {None}
            if not identifiers & prior_identifiers or not editions & prior_editions:
                continue
            first_title = _title_key(item["journal_title"] or "")
            other_title = _title_key(prior["journal_title"] or "")
            if first_title and other_title and first_title != other_title:
                raise ValueError("Conflicting journal titles share ISSN and edition")
            if item["cas"] == prior["cas"] and item["jcr"] == prior["jcr"]:
                raise ValueError("Duplicate journal identity and edition")
            if (item["cas"]["edition_year"] == prior["cas"]["edition_year"] and
                    item["cas"]["major_quartile"] is not None and prior["cas"]["major_quartile"] is not None and
                    item["cas"] != prior["cas"]):
                raise ValueError("Conflicting CAS partition for journal and edition")
            if (item["jcr"]["edition_year"] == prior["jcr"]["edition_year"] and
                    item["jcr"]["categories"] and prior["jcr"]["categories"] and
                    item["jcr"] != prior["jcr"]):
                raise ValueError("Conflicting JCR partition for journal and edition")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".journal-registry-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "entries": existing + additions}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return len(additions)


def classify(entry: dict, categories: list[str] | None = None,
             policy: dict | None = None) -> dict:
    defaults = {"dual_high_cas_max": 1, "dual_high_jcr_max": 1,
                "upper_cas_max": 2, "upper_jcr_max": 2, "conflict_gap": 2}
    if policy is not None and (not isinstance(policy, dict) or set(policy) - set(defaults)):
        raise ValueError("Unknown journal quality policy setting")
    policy = {**defaults, **(policy or {})}
    if any(type(value) is not int or not 1 <= value <= 4 for value in policy.values()):
        raise ValueError("Journal quality policy limits must be integers from 1 to 4")
    cas = dict(entry["cas"])
    jcr = dict(entry["jcr"])
    selected = jcr["categories"]
    if categories:
        selected = [item for item in selected if item["category"].casefold() in {name.casefold() for name in categories}]
    cas_q = cas["major_quartile"]
    # Do not cherry-pick the most favorable JCR subject. A Topic may narrow
    # the relevant categories; within that set, use the conservative quartile.
    jcr_q = max((int(item["quartile"][1]) for item in selected), default=None)
    if cas_q is not None and jcr_q is not None and cas["edition_year"] != jcr["edition_year"]:
        combined, explanation = "UNVERIFIED", "CAS and JCR editions differ; compare each year separately"
    elif (cas_q is not None and jcr_q is not None and
          cas_q <= policy["dual_high_cas_max"] and jcr_q <= policy["dual_high_jcr_max"]):
        combined, explanation = "DUAL_HIGH", (f"CAS major <= {policy['dual_high_cas_max']} and relevant JCR <= Q{policy['dual_high_jcr_max']} under selected policy")
    elif (cas_q is not None and jcr_q is not None and
          cas_q <= policy["upper_cas_max"] and jcr_q <= policy["upper_jcr_max"]):
        combined, explanation = "DUAL_UPPER", (f"CAS major <= {policy['upper_cas_max']} and relevant JCR <= Q{policy['upper_jcr_max']} under selected policy")
    elif cas_q is not None and jcr_q is not None and abs(cas_q - jcr_q) >= policy["conflict_gap"]:
        combined, explanation = "MIXED", "CAS and JCR show divergent positions"
    elif ((cas_q is not None and cas_q <= policy["dual_high_cas_max"] and jcr_q is None) or
          (jcr_q is not None and jcr_q <= policy["dual_high_jcr_max"] and cas_q is None)):
        combined, explanation = "SINGLE_SYSTEM_HIGH", "Only one verified system is high"
    elif cas_q is not None or jcr_q is not None:
        combined, explanation = "VERIFIED_OTHER", "Verified partitions retained without a high-quality claim"
    else:
        combined, explanation = "UNVERIFIED", "No relevant category partition verified"
    cas["status"] = "VERIFIED" if cas_q is not None else "未核实"
    jcr["status"] = "VERIFIED" if jcr_q is not None else "未核实"
    return {"cas": cas, "jcr": jcr, "combined": combined,
            "identity_status": "ISSN_VERIFIED", "explanation": explanation}


def match_quality(registry: list[dict], *, issn: list[str] | None = None,
                  issn_l: str | None = None, journal_title: str | None = None,
                  preprint: bool = False, categories: list[str] | None = None,
                  edition_year: int | None = None,
                  policy: dict | None = None) -> dict:
    if preprint:
        return empty_quality("Preprint has no journal partition")
    identifiers = {item.upper() for item in issn or []}
    if issn_l:
        identifiers.add(issn_l.upper())
    candidates = [entry for entry in registry if identifiers & (set(entry["issn"]) | {entry["issn_l"]})]
    if edition_year is not None:
        candidates = [entry for entry in candidates if edition_year in
                      (entry["cas"]["edition_year"], entry["jcr"]["edition_year"])]
    if not candidates:
        if journal_title and any(_title_key(journal_title) in
                                 {_title_key(str(entry["journal_title"] or "")),
                                  *(_title_key(alias) for alias in entry["aliases"])} for entry in registry):
            return empty_quality("Journal title or alias matches, but ISSN identity needs review")
        return empty_quality()
    titles = {_title_key(entry["journal_title"]) for entry in candidates if entry["journal_title"]}
    if len(titles) > 1:
        result = empty_quality("Conflicting journal identities share an ISSN; manual review required")
        result["identity_status"] = "CONFLICT"
        return result
    if journal_title and titles and _title_key(journal_title) not in {
            *(_title_key(entry["journal_title"]) for entry in candidates if entry["journal_title"]),
            *(_title_key(alias) for entry in candidates for alias in entry["aliases"])}:
        result = empty_quality("ISSN and journal title disagree; manual review required")
        result["identity_status"] = "CONFLICT"
        return result
    cas_rows = [entry["cas"] for entry in candidates if entry["cas"]["major_quartile"] is not None or
                entry["cas"]["minor_quartile"] is not None]
    jcr_rows = [entry["jcr"] for entry in candidates if entry["jcr"]["categories"]]
    if edition_year is not None:
        cas_rows = [row for row in cas_rows if row["edition_year"] == edition_year]
        jcr_rows = [row for row in jcr_rows if row["edition_year"] == edition_year]
    def newest(rows: list[dict]) -> dict | None:
        if not rows:
            return None
        year = max(row["edition_year"] for row in rows)
        current = [row for row in rows if row["edition_year"] == year]
        if any(row != current[0] for row in current[1:]):
            raise ValueError("Conflicting partition records for the same journal and edition")
        return current[0]
    try:
        cas = newest(cas_rows)
        jcr = newest(jcr_rows)
    except ValueError:
        result = empty_quality("Conflicting partition records; manual review required")
        result["identity_status"] = "CONFLICT"
        return result
    if cas is None and jcr is None:
        return empty_quality("No verified partition for the requested edition")
    merged = {"cas": cas or {"edition_year": None, "major_quartile": None,
                              "minor_quartile": None, "major_category": None,
                              "minor_category": None, "source": None},
              "jcr": jcr or {"edition_year": None, "categories": []}}
    return classify(merged, categories, policy)
