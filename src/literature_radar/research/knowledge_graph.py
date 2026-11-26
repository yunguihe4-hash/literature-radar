"""Build a deterministic Obsidian topic map from exported literature notes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TOPIC_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
SPECIES = re.compile(r"\b((?:Bactrocera|Ceratitis|Anastrepha|Drosophila)\s+[a-z][a-z-]+)\b")
INVALID_SPECIES_EPITHETS = {"fly", "flies", "fruit", "pest", "pests", "species", "spp"}
GENE_PATTERNS = {
    "MoY": re.compile(r"(?<![A-Za-z0-9])MoY(?![A-Za-z0-9])", re.I),
    "Bdyof": re.compile(r"(?<![A-Za-z0-9])Bdyof(?![A-Za-z0-9])", re.I),
    "gyf": re.compile(r"(?<![A-Za-z0-9])(?:typo-)?gyf(?![A-Za-z0-9])", re.I),
    "dsx": re.compile(r"\bdsx(?:-M)?\b", re.I),
    "ATPsynβ": re.compile(r"\bATPsyn(?:β|beta)\b", re.I),
}
APPLICATION_PATTERNS = {
    "SIT": re.compile(r"(?<![A-Za-z0-9])(?:SIT|sterile insect technique)(?![A-Za-z0-9])", re.I),
    "GSS": re.compile(r"(?<![A-Za-z0-9])(?:GSS|genetic sexing strain)(?![A-Za-z0-9])", re.I),
    "pgSIT": re.compile(r"(?<![A-Za-z0-9])pgSIT(?![A-Za-z0-9])", re.I),
    "CRISPR sex control": re.compile(r"(?<![A-Za-z0-9])CRISPR(?:[- /]?Cas9)?(?![A-Za-z0-9])", re.I),
    "Pest management": re.compile(r"\bpest management\b", re.I),
}
METHOD_PATTERNS = {
    "Y chromosome assembly": re.compile(r"\b(Y chromosome assembly|Y assembly|assembl(?:y|ing).{0,24}\bY\b)\b", re.I),
    "DiscoverY": re.compile(r"(?<![A-Za-z0-9])DiscoverY(?![A-Za-z0-9])"),
    "R-CQ": re.compile(r"\bR-CQ\b", re.I),
    "KAMY": re.compile(r"\bKAMY\b", re.I),
    "chromosome quotient": re.compile(r"\bchromosome quotient\b", re.I),
    "long-read sequencing": re.compile(r"\b(long-read|PacBio|ONT|HiFi CCS)\b", re.I),
    "T2T assembly": re.compile(r"\b(T2T|telomere-to-telomere)\b", re.I),
    "FISH": re.compile(r"\bFISH\b"),
    "RNAi": re.compile(r"\bRNAi\b", re.I),
    "PCR": re.compile(r"\bPCR\b", re.I),
    "haplotype phasing": re.compile(r"\bhaplotype phasing\b", re.I),
}

METHOD_ENTITIES = frozenset({"DiscoverY", "KAMY", "R-CQ"})
GENE_ENTITIES = frozenset(GENE_PATTERNS)
APPLICATION_ENTITIES = frozenset(APPLICATION_PATTERNS)


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value.strip('"\'')


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    data: dict[str, Any] = {}
    current: str | None = None
    for line in text[4:end].splitlines():
        if line.startswith("  - ") and current:
            if not isinstance(data.get(current), list):
                data[current] = []
            data[current].append(_scalar(line[4:]))
        elif ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            current = key.strip()
            parsed = _scalar(value)
            data[current] = parsed
    return data, text[end + 4:].lstrip("\n")


def section(body: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)", body, re.M | re.S | re.I)
    return match.group(1).strip() if match else ""


def _values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)] if value not in (None, "") else []


@dataclass(frozen=True)
class LiteratureNote:
    path: Path
    title: str
    text: str
    methods: tuple[str, ...]


class KnowledgeGraphGenerator:
    def __init__(self, literature_root: Path):
        self.literature_root = Path(literature_root)

    def build(self, topic_id: str) -> Path:
        if not TOPIC_ID.fullmatch(topic_id):
            raise ValueError("topic must be a safe lowercase topic id")
        topic_dir = self.literature_root / topic_id
        papers_dir = topic_dir / "papers"
        notes = []
        for path in sorted(papers_dir.glob("*.md")) if papers_dir.exists() else []:
            text = path.read_text(encoding="utf-8")
            metadata, body = parse_frontmatter(text)
            if metadata.get("topic") != topic_id:
                continue
            methods = tuple(line[2:].strip() for line in section(body, "Methods").splitlines() if line.startswith("- ") and line[2:].strip() not in {"", "—"})
            searchable = "\n".join([
                str(metadata.get("title", path.stem)), *map(str, _values(metadata.get("tags"))),
                *map(str, _values(metadata.get("keywords"))), *map(str, _values(metadata.get("relation"))), body,
            ])
            notes.append(LiteratureNote(path, str(metadata.get("title") or path.stem), searchable, methods))

        categories: dict[str, dict[str, list[LiteratureNote]]] = {name: {} for name in ("Methods", "Species", "Applications", "Key genes")}
        for note in notes:
            method_terms = {term for term in note.methods if self._entity_class(term) in {None, "method"}}
            method_terms.update(name for name, pattern in METHOD_PATTERNS.items() if pattern.search(note.text))
            self._add(categories["Methods"], method_terms, note)
            species = {name for name in SPECIES.findall(note.text) if name.rsplit(" ", 1)[-1] not in INVALID_SPECIES_EPITHETS}
            self._add(categories["Species"], species, note)
            self._add(categories["Applications"], {name for name, pattern in APPLICATION_PATTERNS.items() if pattern.search(note.text)}, note)
            self._add(categories["Key genes"], {name for name, pattern in GENE_PATTERNS.items() if pattern.search(note.text)}, note)

        lines = ["# Topic Knowledge Map", "", f"- Topic: `{topic_id}`", f"- Literature notes: {len(notes)}", ""]
        for heading in ("Methods", "Species", "Applications", "Key genes"):
            lines += [f"## {heading}", ""]
            entries = categories[heading]
            if not entries:
                lines += ["No concepts identified.", ""]
                continue
            for concept in sorted(entries, key=str.casefold):
                paper_links = [f"[[papers/{note.path.stem}|{note.title}]]" for note in entries[concept]]
                lines.append(f"- [[{concept}]] — " + "; ".join(paper_links))
            lines.append("")
        topic_dir.mkdir(parents=True, exist_ok=True)
        output = topic_dir / "index.md"
        output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return output

    @staticmethod
    def _add(target: dict[str, list[LiteratureNote]], concepts: set[str], note: LiteratureNote) -> None:
        for concept in sorted((item.strip() for item in concepts if item.strip()), key=str.casefold):
            target.setdefault(concept, []).append(note)

    @staticmethod
    def _entity_class(value: str) -> str | None:
        normalized = value.strip().casefold()
        if any(normalized == entity.casefold() for entity in METHOD_ENTITIES):
            return "method"
        if any(pattern.fullmatch(value.strip()) for pattern in GENE_PATTERNS.values()):
            return "gene"
        if any(pattern.fullmatch(value.strip()) for pattern in APPLICATION_PATTERNS.values()):
            return "application"
        return None
