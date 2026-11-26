"""Combine evidence, alignment, phase and local preference signals."""
from __future__ import annotations

def assess(evidence_score: float, alignment: dict, phase: dict, rules: dict, memory: dict | None = None) -> dict:
    memory = memory or {"preferred_concept_ids": []}
    phase_hits = len(set(alignment["questions"]) & set(phase["priority_question_ids"]))
    phase_hits += len(set(alignment["methods"]) & set(phase["priority_method_ids"]))
    memory_hits = len(set(alignment["concepts"] + alignment["questions"] + alignment["methods"]) & set(memory.get("preferred_concept_ids", [])))
    score = evidence_score + 2 * len(alignment["questions"]) + len(alignment["methods"]) + len(alignment["applications"]) + 2 * phase_hits + 2 * len(alignment["transfers"]) + memory_hits
    thresholds = rules["priority_thresholds"]
    priority = "HIGH" if score >= thresholds["high"] else "MEDIUM" if score >= thresholds["medium"] else "LOW"
    return {"score": score, "priority": priority, "phase_hits": phase_hits, "memory_hits": memory_hits}
