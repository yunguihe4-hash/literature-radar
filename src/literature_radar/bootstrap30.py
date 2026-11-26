"""Evidence-only 10 direct + 20 transferable first-review allocation."""
from __future__ import annotations

from collections import Counter

TRANSFER_FIELDS = ("source_species", "source_question", "method", "result",
                   "borrowable_detail", "original_evidence", "target_question")
QUOTAS = {"A": 10, "B": 20}
SCORE_MAX = {"topic_relevance": 30, "research_question": 25,
             "method_value": 25, "evidence_quality": 15, "time_value": 5}
MIN_REVIEW_SCORE = 70


def _norm(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def score_scientific_review(row: dict) -> dict:
    """Validate five recorded judgments; every dimension needs a reason and source."""
    scores = row.get("dimension_scores") or {}
    reasons = row.get("dimension_reasons") or {}
    sources = row.get("dimension_evidence") or {}
    missing = [name for name, maximum in SCORE_MAX.items()
               if (not isinstance(scores.get(name), (int, float)) or
                   isinstance(scores.get(name), bool) or
                   not 0 <= scores[name] <= maximum or
                   not reasons.get(name) or not sources.get(name))]
    return {"total": sum(scores[name] for name in SCORE_MAX) if not missing else None,
            "complete": not missing, "missing_dimensions": missing,
            "scores": {name: scores.get(name) for name in SCORE_MAX}}


def screen_candidate(row: dict, target_species: str | None) -> list[str]:
    issues = []
    group = row.get("group")
    if group not in QUOTAS:
        issues.append("INVALID_GROUP")
    if not target_species:
        issues.append("TARGET_SPECIES_UNSPECIFIED")
    elif group == "A" and _norm(row.get("species")) != _norm(target_species):
        issues.append("NOT_EXACT_TARGET_SPECIES")
    elif group == "B" and _norm(row.get("species")) == _norm(target_species):
        issues.append("NOT_CROSS_SPECIES")
    if row.get("identity_status") not in {"CLEAR", "SUPPORTED_WITH_HISTORIC_CONFLICT"} or (
            row.get("identity_status") == "SUPPORTED_WITH_HISTORIC_CONFLICT" and
            not row.get("current_identity_evidence")):
        issues.append("IDENTITY_REVIEW")
    if row.get("publication_status") not in {"FORMAL", "PREPRINT"}:
        issues.append("PUBLICATION_STATUS_UNVERIFIED")
    if row.get("publication_status") == "PREPRINT" and not row.get("preprint_version_reviewed"):
        issues.append("PREPRINT_VERSION_REVIEW")
    if not row.get("doi"):
        issues.append("DOI_MISSING")
    if not row.get("relevance_verified") or not row.get("relevance_evidence"):
        issues.append("RELEVANCE_UNVERIFIED")
    if group == "B":
        if _norm(row.get("source_species")) != _norm(row.get("species")):
            issues.append("SOURCE_SPECIES_MISMATCH")
        if not row.get("method_category"):
            issues.append("METHOD_CATEGORY_MISSING")
        if not row.get("transfer_verified") or any(not row.get(field) for field in TRANSFER_FIELDS):
            issues.append("TRANSFER_EVIDENCE_INCOMPLETE")
    if row.get("outside_five_year_window") and not row.get("foundational_exception"):
        issues.append("OLDER_PAPER_NO_EXCEPTION")
    scoring = score_scientific_review(row)
    if not scoring["complete"]:
        issues.append("SCORE_EVIDENCE_INCOMPLETE")
    elif scoring["total"] < MIN_REVIEW_SCORE:
        issues.append("SCIENTIFIC_SCORE_BELOW_THRESHOLD")
    return issues


def _rank(row: dict) -> tuple:
    scores = row["dimension_scores"]
    return (-score_scientific_review(row)["total"],
            -scores["research_question"], -scores["method_value"],
            -scores["evidence_quality"], -scores["time_value"],
            _norm(row.get("title")))


def scientific_candidate_issues(row: dict, *, target_species: str,
                                target_aliases: tuple[str, ...] = ()) -> list[str]:
    """Scientific gates for a review pool; JCR evidence is deliberately separate."""
    issues = []
    group = row.get("group")
    if group not in QUOTAS:
        return ["INVALID_GROUP"]
    aliases = {_norm(value) for value in (target_species, *target_aliases)}
    species = _norm(row.get("species"))
    if group == "A" and species not in aliases:
        issues.append("NOT_EXACT_TARGET_SPECIES")
    if group == "B" and (not species or species in aliases):
        issues.append("NOT_CROSS_SPECIES")
    if not row.get("doi"):
        issues.append("DOI_MISSING")
    if not row.get("relevance_verified") or not row.get("relevance_evidence"):
        issues.append("RELEVANCE_EVIDENCE_MISSING")
    if group == "B":
        required = TRANSFER_FIELDS + ("technical_step", "limitations")
        if not row.get("evidence_matched") or any(not row.get(field) for field in required):
            issues.append("TRANSFER_EVIDENCE_INCOMPLETE")
    return issues


def allocate_candidate_pool(candidates: list[dict], *, target_species: str,
                            target_aliases: tuple[str, ...] = ()) -> dict:
    """Build an evidence-bearing 10+20 pool without calling JCR a scientific gate."""
    selected = {"A": [], "B": []}
    rejected = []
    seen_studies = set()
    methods = Counter()
    for group in ("A", "B"):
        ranked = sorted((row for row in candidates if row.get("group") == group),
                        key=lambda row: (-float(row.get("scientific_relevance") or 0),
                                         -float(row.get("methodological_value") or 0),
                                         -float(row.get("evidence_completeness") or 0),
                                         _norm(row.get("title"))))
        if group == "B":
            representatives = []
            represented = set()
            for row in ranked:
                method = row.get("method_category")
                if (method and method not in represented and
                        not scientific_candidate_issues(row, target_species=target_species,
                                                        target_aliases=target_aliases)):
                    representatives.append(row)
                    represented.add(method)
                    if len(representatives) == 5:
                        break
            ranked = representatives + ranked
        processed_candidates = set()
        for row in ranked:
            candidate_id = row.get("paper_id") or id(row)
            if candidate_id in processed_candidates:
                continue
            processed_candidates.add(candidate_id)
            issues = scientific_candidate_issues(row, target_species=target_species,
                                                 target_aliases=target_aliases)
            if issues:
                rejected.append({"paper_id": row.get("paper_id"), "issues": issues})
                continue
            study = _norm(row.get("study_key") or row["doi"])
            if study in seen_studies:
                rejected.append({"paper_id": row.get("paper_id"), "issues": ["DUPLICATE_STUDY"]})
                continue
            if len(selected[group]) >= QUOTAS[group]:
                continue
            method = row.get("method_category")
            if group == "B" and (not method or methods[method] >= 5):
                rejected.append({"paper_id": row.get("paper_id"), "issues": ["METHOD_DIVERSITY_CAP"]})
                continue
            selected[group].append(row)
            seen_studies.add(study)
            if group == "B":
                methods[method] += 1
    gaps = {group: QUOTAS[group] - len(selected[group]) for group in QUOTAS}
    return {"status": "COMPLETE_REVIEW_POOL" if not any(gaps.values()) and len(methods) >= 5
            else "PARTIAL_REVIEW_POOL", "selected_A": selected["A"],
            "selected_B": selected["B"], "gaps": gaps, "method_counts": dict(methods),
            "rejected": rejected}


def allocate_review(candidates: list[dict], *, target_species: str | None) -> dict:
    """Return partial selections plus explicit deficits; never fill a quota by relaxing gates."""
    if not isinstance(candidates, list):
        raise TypeError("candidates must be a list")
    eligible = []
    excluded = []
    for row in candidates:
        issues = screen_candidate(row, target_species)
        if issues:
            excluded.append({"paper_id": row.get("paper_id"), "issues": issues})
        else:
            eligible.append(row)
    selected = {"A": [], "B": []}
    used_studies = set()
    methods = Counter()
    for group in ("A", "B"):
        ranked = sorted((item for item in eligible if item["group"] == group), key=_rank)
        # Reserve one study from each of five methods when the verified pool permits it.
        # A purely greedy rank could fill twenty places using only four methods.
        if group == "B":
            representatives = []
            represented = set()
            for row in ranked:
                method = row["method_category"]
                study = row.get("study_key") or row["doi"].strip().casefold()
                if method not in represented and study not in used_studies:
                    representatives.append(row)
                    represented.add(method)
                    if len(representatives) == 5:
                        break
            ranked = representatives + ranked
        for row in ranked:
            if len(selected[group]) >= QUOTAS[group]:
                break
            # study_key must come from DOI or an explicit preprint/published relation.
            study = row.get("study_key") or row["doi"].strip().casefold()
            if study in used_studies:
                continue
            method = row.get("method_category")
            if group == "B" and methods[method] >= 5:
                continue
            selected[group].append(row)
            used_studies.add(study)
            if group == "B":
                methods[method] += 1
    gaps = {group: QUOTAS[group] - len(selected[group]) for group in QUOTAS}
    diversity = len(methods)
    return {"status": "PASS" if not any(gaps.values()) and diversity >= 5 else "BLOCKED",
            "selected_A": selected["A"], "selected_B": selected["B"],
            "gaps": gaps, "method_counts": dict(methods), "method_diversity": diversity,
            "excluded": excluded}
