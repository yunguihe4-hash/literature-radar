"""Evidence-bound transfer explanations; absent article facts remain null."""
from __future__ import annotations

import re
from typing import Any

from literature_radar.models import Paper


FIELDS = ("source_species", "source_research_question", "source_method",
          "target_research_question", "transfer_type", "transfer_rationale",
          "evidence_source", "evidence_field", "evidence_excerpt")


def direct_species_evidence(paper: Paper, bundle: dict) -> bool:
    extensions = bundle["topic"].get("extensions", {})
    values = [*(extensions.get("target_species") or []),
              *(extensions.get("source_roles", {}).get("direct", []) or [])]
    text = f"{paper.title} {paper.abstract or ''}"
    return any(isinstance(value, str) and re.search(
        r"(?<!\w)" + re.escape(value) + r"(?!\w)", text, re.I) for value in values)


def _species_hit(paper: Paper, bundle: dict, relation: dict) -> tuple[str | None, str | None, str | None]:
    """Recognize only an explicit species phrase in metadata, title or abstract."""
    for record in paper.source_records:
        for key in ("species", "organism", "taxon"):
            value = record.normalized_metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), f"{record.provider_id}:{record.source_id}", key
    extensions = bundle["topic"].get("extensions", {})
    roles = extensions.get("source_roles", {})
    candidates = []
    domain = relation.get("from_domain")
    if isinstance(domain, str) and domain:
        if "/" in domain:
            candidates.extend(part.strip() for part in domain.split("/") if part.strip())
        elif domain.casefold() == "model insects" and isinstance(roles, dict):
            candidates.extend(value for value in roles.get("model", []) if isinstance(value, str))
        elif domain.casefold() == "mammalian y":
            concepts = bundle.get("ontology", {}).get("concepts", [])
            source = next((item for item in concepts if item.get("id") == relation.get("from")), {})
            candidates.extend(alias.split()[0] for alias in source.get("aliases", [])
                              if alias and alias.split()[0].casefold() not in {"mammalian", "other"})
        else:
            candidates.append(domain)
    else:
        if isinstance(roles, dict):
            for values in roles.values():
                if isinstance(values, list):
                    candidates.extend(value for value in values if isinstance(value, str))
        for key in ("target_species", "model_species"):
            values = extensions.get(key, [])
            if isinstance(values, list):
                candidates.extend(value for value in values if isinstance(value, str))
    for field, text in (("title", paper.title), ("abstract", paper.abstract or "")):
        for candidate in sorted(set(candidates), key=len, reverse=True):
            # Ontology domains such as "Mammalian Y" name a research area,
            # not the species studied by an individual article.
            if candidate.casefold().startswith(("other ", "mammalian ", "model insects")):
                continue
            if re.search(r"(?<!\w)" + re.escape(candidate) + r"(?!\w)", text, re.I):
                return candidate[:-2] if candidate.casefold().endswith(" y") else candidate, paper.source, field
    return None, None, None


def explain_transfers(paper: Paper, bundle: dict, relations: list[dict],
                      question_evidence: list[dict], method_evidence: list[dict],
                      concept_evidence: list[dict]) -> list[dict[str, Any]]:
    profile = {item["id"]: item["label"] for item in bundle["profile"]["questions"]}
    explanations = []
    for relation in relations:
        species, species_source, species_field = _species_hit(paper, bundle, relation)
        source_question = next((item for item in question_evidence if item.get("excerpt")), None)
        source_method = next((item for item in method_evidence if item.get("excerpt")), None)
        concept = next((item for item in concept_evidence if item.get("excerpt")), None)
        evidence = source_method or source_question or concept
        target = profile.get(relation["to"])
        complete = bool(species and source_question and source_method and target and evidence)
        missing = []
        if not species: missing.append("source_species")
        if not source_question: missing.append("source_research_question")
        if not source_method: missing.append("source_method")
        if not target: missing.append("target_research_question")
        proof = None
        phrase = None
        if relation["type"] != "direct_reference":
            phrase = relation.get("extensions", {}).get("transfer_evidence_phrase")
            if isinstance(phrase, str) and len(phrase.split()) >= 3 and paper.abstract:
                match = re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", paper.abstract, re.I)
                if match:
                    proof = {"field": "abstract", "excerpt": " ".join(
                        paper.abstract[max(0, match.start() - 35):min(len(paper.abstract), match.end() + 35)].split())}
            complete = complete and proof is not None
            if proof is None:
                missing.append("transfer_evidence_phrase_absent_or_unmatched")
        rationale = None
        if complete:
            detail = relation.get("extensions", {}).get("borrowable_detail")
            if isinstance(detail, str) and detail.strip():
                rationale = "Candidate to review: " + detail.strip()
            elif proof:
                rationale = ("Candidate to review: abstract supports '" + phrase +
                             "' for target question '" + target + "'")
            elif relation["type"] == "direct_reference":
                rationale = (f"Direct reference: {species} examines '{source_question['phrase']}' "
                             f"using '{source_method['phrase']}' for '{target}'")
        explanations.append({
            "source_species": species,
            "source_research_question": source_question["phrase"] if source_question else None,
            "source_method": source_method["phrase"] if source_method else None,
            "target_research_question": target,
            "transfer_type": relation["type"],
            "transfer_rationale": rationale,
            "evidence_source": paper.source if (proof or evidence) else species_source,
            "evidence_field": (proof or evidence)["field"] if (proof or evidence) else species_field,
            "evidence_excerpt": (proof or evidence)["excerpt"] if (proof or evidence) else None,
            "transfer_evidence_phrase": phrase if proof else None,
            "missing_evidence": missing,
            "status": "EVIDENCE_MATCHED" if complete else "NEEDS_REVIEW",
            "review_required": not complete,
        })
    return explanations
