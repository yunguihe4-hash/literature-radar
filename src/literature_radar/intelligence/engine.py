"""Match configurable profile concepts and ontology relations."""
from __future__ import annotations
from literature_radar.models import Paper

def _matched(entries: list[dict], text: str) -> list[str]:
    return [entry["id"] for entry in entries if any(alias.casefold() in text for alias in entry.get("aliases", [entry["label"]]))]

def align(paper: Paper, profile: dict, ontology: dict) -> dict:
    text = f"{paper.title} {paper.abstract}".casefold()
    questions = _matched(profile["questions"], text)
    methods = _matched(profile["methods"], text)
    applications = _matched(profile["applications"], text)
    concepts = _matched(ontology["concepts"], text)
    seen = set(questions + methods + applications + concepts)
    transfers = [r for r in ontology["relations"] if r["from"] in seen and
                 ((r["type"] == "direct_reference" and r["to"] in seen) or
                  (r["type"] != "direct_reference" and r["to"] not in seen))]
    return {"questions": questions, "methods": methods, "applications": applications,
            "concepts": concepts, "transfers": transfers}
