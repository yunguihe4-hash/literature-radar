"""Original AI_OS Research Attention functions, copied without rule changes.

Source: skills/literature-intelligence/intelligence.py
SHA256: 47a489e15c34aae14628a08fb15f67d9bc6b9876ec0c83119cfde5e8c805240d
Only the selected pure functions are included; staging adapters supply their data.
"""
from __future__ import annotations
import html
import re
import unicodedata
from typing import Any


def clean_text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


def normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", clean_text(value)).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).split())


def contains(text: str, phrase: str) -> bool:
    phrase = normalized_text(phrase)
    return bool(phrase) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def match_aliases(text: str, aliases: list[str]) -> str | None:
    return next((alias for alias in aliases if contains(text, alias)), None)


# The seventh Attention feature is the original Intelligence keyword score.
# Keep its topic rules and ontology scoring separate from the newer screening
# score, which has different weights and cannot be substituted here.
CLASSIFICATION_ORDER = (
    "Technology transfer",
    "Experimental design transfer",
    "Direct project relevance",
    "Biological concept transfer",
)
STANDALONE_METHOD_ALIGNMENT = {
    "T2T assembly": 0.65,
    "k-mer sex identification": 0.5,
    "ultra-long ONT": 0.4,
    "haplotype-resolved assembly": 0.4,
    "repeat resolution": 0.65,
    "chromosome-scale assembly": 0.1,
    "PacBio HiFi": 0.2,
    "graph genome": 0.1,
    "RNA-seq": 0.0,
    "single-cell transcriptomics": 0.2,
    "spatial transcriptomics": 0.2,
    "functional genomics": 0.1,
    "CRISPR knockout": 0.3,
}


def collect_evidence(
    text: str,
    rules: dict[str, Any],
    ontologies: list[dict[str, Any]],
) -> tuple[dict[str, int], set[str], list[dict[str, Any]]]:
    scores = {name: 0 for name in CLASSIFICATION_ORDER}
    qualified: set[str] = set()
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for dimension, entries in rules["dimensions"].items():
        scores.setdefault(dimension, 0)
        for entry in entries:
            alias = match_aliases(text, entry.get("aliases", [entry["keyword"]]))
            key = (dimension, entry["keyword"])
            if alias is None or key in seen:
                continue
            seen.add(key)
            score = int(entry["score"])
            scores[dimension] += score
            role = entry.get("role", "scientific_signal")
            if role == "scientific_signal":
                qualified.add(dimension)
            evidence.append({
                "dimension": dimension,
                "keyword": entry["keyword"],
                "matched_alias": alias,
                "score": score,
                "role": role,
                "source": "topic_rule",
            })
    for ontology in ontologies:
        dimension = ontology["classification"]
        scores.setdefault(dimension, 0)
        for concept in ontology["concepts"]:
            alias = match_aliases(text, concept["aliases"])
            key = (dimension, concept["keyword"])
            if alias is None or key in seen:
                continue
            seen.add(key)
            score = int(concept["score"])
            scores[dimension] += score
            qualified.add(dimension)
            evidence.append({
                "dimension": dimension,
                "keyword": concept["keyword"],
                "matched_alias": alias,
                "score": score,
                "role": "ontology_method",
                "source": f"ontology:{ontology['name']}",
                "method": concept["method"],
                "possible_application": concept["possible_application"],
                "standalone_alignment": concept.get(
                    "standalone_alignment", STANDALONE_METHOD_ALIGNMENT.get(concept["keyword"], 0.1)
                ),
            })
    return scores, qualified, evidence


def named_alias_matches(text: str, entries: list[dict[str, Any]]) -> list[str]:
    return [entry["name"] for entry in entries if match_aliases(text, entry.get("aliases", [entry["name"]]))]


def attention_memory_matches(
    topic: str,
    questions: list[str],
    methods: list[str],
    transfer_type: str | None,
    memory: dict[str, Any],
) -> list[str]:
    matches: list[str] = []
    for entry in memory.get("entries", []):
        if entry.get("topic") != topic:
            continue
        known_questions = {normalized_text(value) for value in entry.get("important_questions", [])}
        known_methods = {normalized_text(value) for value in entry.get("important_methods", [])}
        preferred = set(entry.get("preferred_transfer", []))
        if any(normalized_text(value) in known_questions for value in questions):
            matches.append("important_question")
        if any(normalized_text(value) in known_methods for value in methods):
            matches.append("important_method")
        if transfer_type and transfer_type in preferred:
            matches.append("preferred_transfer")
    return list(dict.fromkeys(matches))


def y_research_question_matches(text: str, profile: dict[str, Any] | None = None) -> list[str]:
    """Recognize Y structure and the other explicit questions in this profile."""
    explicit_y = any(contains(text, value) for value in (
        "Y chromosome", "Y-chromosomal", "Y linked", "Y-linked", "Y scaffold", "Y scaffolds", "Y repeat",
        "male-specific sequence", "sex-specific sequence", "male-specific k-mer",
        "sex-specific k-mer",
    ))
    sex_chromosome_subject = any(contains(text, value) for value in (
        "sex-linked chromosome", "sex chromosome",
    ))
    assembly_action = any(contains(text, value) for value in (
        "assembly", "assembled", "sequence assembly", "structural resolution",
        "chromosome structure", "T2T", "telomere-to-telomere", "gap-free",
        "repeat resolution", "repeat-rich", "haplotype-resolved", "chromosome engineering",
    ))
    y_discovery_action = any(contains(text, value) for value in (
        "identification", "identified", "discovery", "assignment", "scaffold identification",
        "k-mer", "kmer", "chromosome quotient", "coverage difference",
        "read-depth comparison", "male/female coverage", "male female coverage",
    ))
    sex_chromosome_identification = any(contains(text, value) for value in (
        "sex chromosome identification", "sex-linked chromosome assignment",
        "chromosome quotient", "coverage difference", "read-depth comparison",
        "male/female coverage", "male female coverage", "sex-specific read depth",
        "candidate X chromosome", "candidate Y chromosome",
    ))
    nonstructural = any(contains(text, value) for value in (
        "loss of Y chromosome", "Y chromosome loss", "aneuploidy", "trisomy",
        "cancer", "carcinoma", "tumor", "autism", "disease association",
        "transcript dosage", "methylation", "prenatal diagnosis", "genotype imputation",
    ))
    matches: list[str] = []
    if explicit_y and assembly_action:
        matches.append("Y chromosome assembly")
    if (explicit_y and y_discovery_action) or (sex_chromosome_subject and sex_chromosome_identification):
        matches.append("sex chromosome identification")
    if any(contains(text, value) for value in ("male-specific sequence", "Y-linked sequence")) and y_discovery_action:
        matches.append("male-specific sequence discovery")
    if contains(text, "sex chromosome engineering"):
        matches.append("sex determination")
    if nonstructural and not assembly_action and not y_discovery_action and not sex_chromosome_identification:
        return []
    # The old Y matcher covered only chromosome structure even when the same
    # profile explicitly listed sex determination and SIT/GSS/pgSIT. Retain the
    # original Y rules above, then read these other profile questions through
    # their own curated aliases. This never treats a bare T2T/repeat word as a
    # sex-chromosome question.
    for question in (profile or {}).get("important_questions", []):
        name = question["name"]
        if name in {"Y chromosome assembly", "sex chromosome identification",
                    "male-specific sequence discovery"}:
            continue
        if match_aliases(text, question.get("aliases", [name])):
            matches.append(name)
        elif normalized_text(name) == "sit gss pgsit" and any(contains(text, signal) for signal in (
                "male fertility", "spermatogenesis", "white pupae phenotype",
                "genetic sexing", "sterile insect technique", "sterile male", "sexing trait")):
            # These are specific biological outcomes of the profile's existing
            # genetic sexing / sterile-male application, not generic repeats.
            matches.append(name)
    return list(dict.fromkeys(matches))


def linked_attention_methods(
    text: str,
    questions: list[str],
    methods_ontology: dict[str, Any],
) -> list[str]:
    question_names = {normalized_text(value) for value in questions}
    linked: list[str] = []
    for method in methods_ontology.get("methods", []):
        if match_aliases(text, method.get("aliases", [method["name"]])) is None:
            continue
        targets = {normalized_text(value) for value in method.get("transferable_to", [])}
        if question_names & targets:
            linked.append(method["name"])
        elif "y chromosome assembly" in question_names and method["name"] in {
            "T2T assembly", "HiFi", "ONT ultra-long", "repeat resolution", "haplotype phasing",
        }:
            linked.append(method["name"])
        elif question_names & {"sex chromosome identification", "male-specific sequence discovery"} and method["name"] in {
            "k-mer identification", "chromosome quotient", "sex-specific coverage",
        }:
            linked.append(method["name"])
    return list(dict.fromkeys(linked))


def assess_attention(
    paper: dict[str, Any],
    topic: str,
    profile: dict[str, Any],
    cross_species: dict[str, Any],
    methods_ontology: dict[str, Any],
    memory: dict[str, Any],
    phase: dict[str, Any],
) -> dict[str, Any]:
    text = normalized_text(f"{paper['title']} {paper.get('_abstract', '')}")
    excluded = [value for value in profile.get("excluded_directions", []) if contains(text, value)]
    # The legacy Y-chromosome question matcher belongs to the research
    # profile, not to a particular user's Topic identifier. This preserves
    # its established behavior when the same profile is reused under a new ID.
    y_research_profile = any(
        normalized_text(item.get("name", "")) == "y chromosome assembly"
        for item in profile.get("important_questions", []))
    if y_research_profile:
        questions = y_research_question_matches(text, profile)
    else:
        questions = [
            item["name"] for item in profile.get("important_questions", [])
            if match_aliases(text, item.get("aliases", [item["name"]]))
        ]
    methods = named_alias_matches(text, methods_ontology.get("methods", []))
    linked_methods = linked_attention_methods(text, questions, methods_ontology)
    applications = [value for value in profile.get("important_applications", []) if contains(text, value)]
    if y_research_profile and contains(text, "sex chromosome engineering"):
        applications.append("sex chromosome engineering")
    targets = named_alias_matches(text, cross_species.get("target_species", []))
    models = named_alias_matches(text, cross_species.get("model_species", []))
    technology_sources = named_alias_matches(text, cross_species.get("technology_source", []))
    if contains(text, "human") and contains(text, "Y chromosome"):
        technology_sources.append("human chromosome assembly")
    if any(contains(text, value) for value in ("mammalian", "mouse", "murine")) and contains(text, "Y chromosome"):
        technology_sources.append("mammalian Y chromosome")
    technology_sources = list(dict.fromkeys(technology_sources))
    accepted_sources = {normalized_text(value) for value in profile.get("accepted_cross_species_sources", [])}
    models = [value for value in models if normalized_text(value) in accepted_sources]
    technology_sources = [value for value in technology_sources if normalized_text(value) in accepted_sources]
    active_phase_signals = {normalized_text(value) for value in phase.get("active_signals", [])}
    phase_signals = []
    for method in methods_ontology.get("methods", []):
        if method["name"] not in linked_methods:
            continue
        method_terms = {normalized_text(method["name"]), *(
            normalized_text(value) for value in method.get("aliases", [])
        )}
        if method_terms & active_phase_signals:
            phase_signals.append(method["name"])

    transfer_type: str | None = None
    if not excluded and questions and targets:
        transfer_type = "direct_reference"
    elif not excluded and questions and linked_methods and (models or technology_sources or targets):
        transfer_type = "method_transfer"
    elif not excluded and questions and applications and (models or targets):
        transfer_type = "application_transfer"
    elif (
        not excluded and questions and (models or technology_sources)
        and any(contains(text, value) for value in ("Y chromosome", "Y-chromosomal", "Y-linked", "Y scaffold"))
    ):
        transfer_type = "concept_transfer"

    memory_matches = attention_memory_matches(topic, questions, linked_methods, transfer_type, memory)
    problem_match = bool(questions) and not excluded
    transfer_value = transfer_type is not None
    method_match = transfer_type == "method_transfer"
    application_match = transfer_type == "application_transfer"
    eligible = problem_match and transfer_value
    source_domain = targets or models or technology_sources
    target_problem = questions[0] if questions else None
    why = []
    if excluded:
        why.append(f"明确排除方向：{'、'.join(excluded)}")
    elif questions:
        why.append(f"与当前研究问题相关：{'、'.join(questions)}")
    else:
        why.append("未匹配当前 Research Profile 中的科学问题")
    if linked_methods:
        why.append(f"与当前问题形成明确连接的方法：{'、'.join(linked_methods)}")
    elif transfer_type == "concept_transfer":
        why.append("提供跨物种的科学概念参考，但未识别到具体方法")
    elif transfer_type == "direct_reference":
        why.append("研究对象与当前目标物种及问题直接对应")
    if not transfer_value and not excluded:
        why.append("未形成直接、方法、应用或概念迁移链条")

    return {
        "paper": {"paper_id": paper["paper_id"], "title": paper["title"], "doi": paper.get("doi")},
        "category": paper["category"],
        "attention_level": "LOW",
        "why_attention": why,
        "research_question_matches": questions,
        "method_matches": linked_methods,
        "application_matches": applications,
        "memory_matches": memory_matches,
        "research_phase": phase.get("current_phase"),
        "research_phase_matches": phase_signals,
        "transfer": {
            "type": transfer_type,
            "source_domain": source_domain,
            "target_problem": target_problem,
        },
        "attention_rank": {
            "research_question_match": problem_match,
            "research_phase_match": bool(phase_signals) and not excluded,
            "transfer_value": transfer_value,
            "method_match": method_match,
            "application_match": application_match,
            "attention_memory": bool(memory_matches),
            "keyword_evidence": paper["evidence_score"],
        },
        "eligible": eligible,
    }


def attention_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    rank = item["attention_rank"]
    return (
        -int(rank["research_question_match"]),
        -int(rank["research_phase_match"]),
        -int(rank["transfer_value"]),
        -int(rank["method_match"]),
        -int(rank["application_match"]),
        -int(rank["attention_memory"]),
        -int(rank["keyword_evidence"]),
        item["paper"]["title"].casefold(),
    )


def classified_attention(item: dict[str, Any]) -> str:
    """Map unchanged v0.3.2 gate evidence to intrinsic, delivery-independent attention."""
    if item["eligible"]:
        return "HIGH"
    rank = item["attention_rank"]
    if rank["research_question_match"] or rank["research_phase_match"]:
        return "MEDIUM"
    return "LOW"
