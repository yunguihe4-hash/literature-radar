"""Conservative, explainable alert gate over the existing Attention score."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace

from literature_radar.attention.ranking import assess
from literature_radar.evaluation.engine import score_evidence
from literature_radar.intelligence.engine import align
from literature_radar.models import Paper
from literature_radar.attention.transfer import explain_transfers, direct_species_evidence
from literature_radar.attention.research_ranking import rank_research_attention
from literature_radar.attention.compatibility import assess_original, original_keyword_evidence
from literature_radar.attention.calibration import calibrated_attention

_NEGATION = re.compile(r"\b(?:no|not|without|lacks?|absent|failed|unrelated)\b", re.I)
_COMMON = frozenset({"with", "from", "using", "test", "resolve", "measure", "explain",
                     "link", "other", "study", "research", "related", "associated",
                     "specific", "target", "current", "function", "gene", "loss"})
_WEAK_DOMAIN = frozenset({"behavior", "behaviour", "expression", "application",
                          "regulation", "analysis", "response", "method", "system",
                          "experimental", "comparison", "design", "single", "cell",
                          "differential"})
_GENERIC_ENTITY_ANCHORS = frozenset({"chromosome", "assembly", "sequence", "genome",
                                     "repeat", "coverage", "haplotype", "telomere",
                                     "sequencing", "resolution", "male", "female",
                                     "linked", "specific", "sex"})


def _words(value: str) -> set[str]:
    return {word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", value)}


def _topic_corroboration(paper: Paper, bundle: dict) -> dict | None:
    """Require a repeated Topic entity and two independent article-domain cues."""
    searches = [*bundle["topic"]["search_terms"], *bundle["focus"]["search_terms"]]
    if len(set(searches)) < 2:
        return None
    counts = Counter(word for term in searches for word in _words(term) - _COMMON)
    # Repeated platform words are common across unrelated species. They cannot
    # establish a Topic entity on their own (for example, a routine fish genome
    # assembly in a sex-chromosome Topic).
    anchors = [word for word, count in counts.most_common()
               if count >= 2 and word not in _GENERIC_ENTITY_ANCHORS
               and word not in _WEAK_DOMAIN]
    if not anchors or not paper.abstract or len(paper.abstract) < 100:
        return None
    text = paper.title + " " + paper.abstract
    anchor = next((word for word in anchors if re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.I)), None)
    if anchor is None:
        return None
    domain = set().union(*(_words(term) for term in searches)) - _COMMON - {anchor}
    matched = sorted(word for word in domain if re.search(r"(?<!\w)" + re.escape(word[:6]) + r"\w*(?!\w)", text, re.I))
    if len(matched) < 2:
        return None
    strong = [word for word in matched if word not in _WEAK_DOMAIN]
    if not strong:
        return None
    in_title = bool(re.search(re.escape(anchor), paper.title, re.I))
    around_entity = [paper.abstract[max(0, hit.start()-180):hit.end()+180]
                     for hit in re.finditer(r"(?<!\w)" + re.escape(anchor) + r"(?!\w)", paper.abstract, re.I)]
    if not in_title and len(around_entity) < 2:
        return None
    if not in_title and not any(
        re.search(r"(?<!\w)" + re.escape(word[:6]) + r"\w*(?!\w)", nearby, re.I)
        for nearby in around_entity for word in strong):
        return None
    question = max(bundle["profile"]["questions"], key=lambda entry: len(
        _words(entry["label"] + " " + " ".join(entry.get("aliases", []))) & set(matched)))
    excerpt_at = re.search(r"(?<!\w)" + re.escape(anchor) + r"(?!\w)", paper.abstract, re.I)
    excerpt = paper.abstract[max(0, excerpt_at.start()-80):excerpt_at.end()+150] if excerpt_at else paper.abstract[:220]
    return {"entity": anchor, "domain_cues": matched, "question_id": question["id"],
            "evidence": {"field": "abstract", "phrase": anchor,
                         "excerpt": " ".join(excerpt.split())},
            "rationale": f"Topic entity {anchor} and article-domain evidence ({', '.join(matched[:4])}) occur in the title/abstract; transfer and interpretation require review"}


def _hits(paper: Paper, phrases: list[str], *, bounded_words: bool = False) -> list[dict]:
    found = []
    for phrase in phrases:
        patterns = [r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"]
        tokens = re.findall(r"[A-Za-z0-9]+", phrase)
        if bounded_words and 2 <= len(tokens) <= 4 and len(tokens[0]) >= 4:
            patterns.append(r"(?<!\w)" + r"(?:\W+\w+){0,3}\W+".join(
                re.escape(token) + r"[a-z]*" for token in tokens) + r"(?!\w)")
        for field, text in (("title", paper.title), ("abstract", paper.abstract or "")):
            for match in (hit for pattern in patterns for hit in re.finditer(pattern, text, re.I)):
                prefix = re.split(r"[.!?;]", text[max(0, match.start() - 65):match.start()])[-1]
                suffix = re.split(r"[.!?;]", text[match.end():match.end() + 65])[0]
                if (_NEGATION.search(" ".join(prefix.split()[-5:])) or
                        _NEGATION.search(" ".join(suffix.split()[:5]))):
                    continue
                excerpt = text[max(0, match.start() - 35):min(len(text), match.end() + 35)]
                found.append({"phrase": phrase, "field": field, "offset": match.start(),
                              "excerpt": " ".join(excerpt.split())})
                break
    return found


def _matched_ids(paper: Paper, entries: list[dict], *, bounded_words: bool = False) -> tuple[list[str], list[dict]]:
    ids, evidence = [], []
    for entry in entries:
        hits = _hits(paper, entry.get("aliases", [entry["label"]]), bounded_words=bounded_words)
        if hits:
            ids.append(entry["id"])
            evidence.extend(hits)
    return ids, evidence


def recommend(paper: Paper, bundle: dict, memory: dict | None = None,
              decision: str | None = None, *, config_dir=None) -> dict:
    """Keep the original score; apply an independent, conservative send gate."""
    scored = replace(paper, abstract=paper.abstract or "")
    score, rules_evidence = score_evidence(scored, bundle["rules"])
    alignment = align(scored, bundle["profile"], bundle["ontology"])
    attention = assess(score, alignment, bundle["phase"], bundle["rules"], memory)
    legacy_score, legacy_evidence, legacy_source = original_keyword_evidence(paper, config_dir, score)
    original = assess_original(paper, bundle, legacy_score, config_dir=config_dir)
    original["keyword_evidence_details"] = legacy_evidence
    original["keyword_evidence_source"] = legacy_source
    if config_dir is not None:
        original = calibrated_attention(config_dir, original)
    positive = _hits(paper, [rule["term"] for rule in bundle["rules"]["positive"]])
    negative = _hits(paper, [rule["term"] for rule in bundle["rules"]["negative"]])
    question_ids, questions = _matched_ids(paper, bundle["profile"]["questions"], bounded_words=True)
    method_ids, methods = _matched_ids(paper, bundle["profile"]["methods"])
    application_ids, applications = _matched_ids(paper, bundle["profile"]["applications"])
    concept_ids, concepts = _matched_ids(paper, bundle["ontology"]["concepts"])
    topic_corroboration = _topic_corroboration(paper, bundle)
    transfer = [relation for relation in alignment["transfers"]
                if relation["to"] in {entry["id"] for part in ("questions", "methods", "applications")
                                      for entry in bundle["profile"][part]}
                and relation["from"] in concept_ids + question_ids + method_ids + application_ids]
    # Keep the legacy alignment/score untouched. Research Attention may explain
    # a transfer to a question that is itself mentioned in the article.
    matched_sources = set(concept_ids + question_ids + method_ids + application_ids)
    target_questions = {entry["id"] for entry in bundle["profile"]["questions"]}
    structured_relations = [relation for relation in bundle["ontology"]["relations"]
                            if relation["from"] in matched_sources and
                            relation["to"] in target_questions]
    corroborated = bool(positive and questions and (methods or transfer)
                        and paper.abstract and not negative)
    # Keep the existing screening score as audit evidence. Weekly delivery
    # applies the original seven-dimensional Attention result separately.
    priority = ("HIGH" if decision == "must_read" else
                "MEDIUM" if decision == "keep" and attention["priority"] == "LOW" else
                attention["priority"])
    eligible = (priority == "HIGH" and corroborated and decision not in {"discard", "later"})
    if decision == "must_read":
        eligible = True
    reason = {"questions": question_ids, "methods": method_ids,
              "applications": application_ids, "transfers": transfer,
             "concepts": concept_ids,
              "evidence": positive + questions + methods + applications + concepts,
              "contrary_evidence": negative,
              "rules_evidence": rules_evidence,
              "needs_review": not corroborated,
              "topic_corroboration": topic_corroboration}
    if topic_corroboration:
        reason["evidence"].append(topic_corroboration["evidence"])
    reason["structured_transfers"] = explain_transfers(
        paper, bundle, structured_relations, questions, methods, concepts)
    attention_ranking = rank_research_attention(
        reason, memory, direct_species_match=direct_species_evidence(paper, bundle))
    # Older Topic configs can state a typed concept-to-method relation without
    # the newer structured transfer fields. Keep that explicit, evidenced
    # relation eligible for review while generic keyword matches remain D.
    if (attention_ranking["category"] == "D" and corroborated and transfer and
            any(item["field"] == "title" and len(item["phrase"].split()) >= 3
                for item in positive) and
            any(item["field"] == "abstract" for item in questions)):
        attention_ranking = {**attention_ranking, "category": "C",
                             "review_required": True,
                             "ordering": [2, *attention_ranking["ordering"][1:]]}
    if attention_ranking["category"] == "D" and decision != "must_read":
        priority = "LOW"
        eligible = False
    if original["final_attention"] == "IRRELEVANT" and decision != "must_read":
        eligible = False
    return {"base_score": attention["score"], "base_priority": attention["priority"],
            "priority": priority, "eligible": eligible, "decision": decision,
            "reason": reason, "attention_ranking": attention_ranking,
            "original_attention": original}
