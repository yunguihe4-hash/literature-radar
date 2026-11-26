"""Score positive and negative evidence from user rules."""
from __future__ import annotations
from literature_radar.models import Paper

def score_evidence(paper: Paper, rules: dict) -> tuple[float, list[dict]]:
    text = f"{paper.title} {paper.abstract}".casefold()
    total = 0.0
    evidence = []
    for direction, sign in (("positive", 1), ("negative", -1)):
        for rule in rules[direction]:
            if rule["term"].casefold() in text:
                weight = float(rule["weight"]) * sign
                total += weight
                evidence.append({"term": rule["term"], "dimension": rule["dimension"], "weight": weight})
    return total, evidence
