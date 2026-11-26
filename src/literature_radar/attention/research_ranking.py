"""A separate, inspectable Research Attention order over legacy scores."""
from __future__ import annotations


def rank_research_attention(reason: dict, memory: dict | None = None,
                            *, direct_species_match: bool = False) -> dict:
    memory = memory or {"preferred_concept_ids": []}
    transfers = reason.get("structured_transfers", [])
    matched = [item for item in transfers if item["status"] == "EVIDENCE_MATCHED"]
    kinds = {item["transfer_type"] for item in matched}
    questions = reason.get("questions", [])
    methods = reason.get("methods", [])
    evidence = reason.get("evidence", [])
    abstract_support = any(item.get("field") == "abstract" for item in evidence)
    direct = bool(questions and methods and abstract_support and
                  ("direct_reference" in kinds or direct_species_match))
    if direct:
        category = "A"
    elif "method_transfer" in kinds and questions and methods and abstract_support:
        category = "B"
    elif kinds & {"concept_transfer", "application_transfer"} and questions and abstract_support:
        category = "C"
    elif not transfers and questions and methods and abstract_support and not reason.get("contrary_evidence"):
        # The scientific question and method both appear in article evidence;
        # transfer to the target system still needs a human check.
        category = "C"
    elif not transfers and reason.get("topic_corroboration") and abstract_support and not reason.get("contrary_evidence"):
        category = "C"
    else:
        category = "D"
    confirmed = set(memory.get("preferred_concept_ids", []))
    memory_hits = len(confirmed & set(questions + methods + reason.get("concepts", [])))
    confirmed_transfer_types = set(memory.get("extensions", {}).get("confirmed_transfer_types", []))
    memory_hits += len(confirmed_transfer_types & kinds)
    completeness = sum(bool(item.get(field)) for item in matched for field in
                       ("source_species", "source_research_question", "source_method",
                        "target_research_question", "evidence_excerpt"))
    components = {"direct_question_match": len(questions),
                  "cross_species_transfer": len(matched),
                  "method_transfer": sum(item["transfer_type"] == "method_transfer" for item in matched),
                  "concept_application_transfer": sum(item["transfer_type"] in ("concept_transfer", "application_transfer") for item in matched),
                  "confirmed_memory": memory_hits,
                  "evidence_completeness": completeness}
    return {"category": category, "components": components,
            "review_required": category == "D" or not abstract_support or
                               (category == "C" and not matched) or
                               any(item["review_required"] for item in transfers),
            "ordering": ["ABCD".index(category), -components["direct_question_match"],
                         -components["method_transfer"], -components["confirmed_memory"],
                         -components["evidence_completeness"]]}
