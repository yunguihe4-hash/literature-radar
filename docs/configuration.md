# Configuration contract

The loader reads JSON only, in a fixed order from an explicit root: Topic, Focus, Profile, Rules, Ontology, Phase, then optional Attention Memory and Delivery. It does not parse YAML or TOML or silently convert formats. Each document has `schema_version: 1`. The canonical schemas are package resources in `src/literature_radar/schemas/` and load through `importlib.resources`. The separate `feedback.schema.json` validates local decision records and is not a configuration file.

| Type / file | Purpose | Required fields beyond version | Optional fields and defaults | References |
|---|---|---|---|---|
| Topic / `topic.json` | Literature discovery scope | `id`, `display_name`, nonempty unique `search_terms`, nonempty unique `sources` | `focus_file` defaults to `focus.json`; `extensions` | Focus file path |
| Focus / `focus.json` | Versioned current search focus | `topic_id`, `version` (`vNNN` or longer), unique `search_terms` | `created_at` (date-time), `extensions` | Topic ID |
| Profile / `profile.json` | Named questions, methods, applications | `topic_id`, arrays `questions`, `methods`, `applications` | Entry `aliases` defaults to its label; `extensions` | Topic ID; IDs used by Phase and Ontology |
| Rules / `rules.json` | Positive/negative evidence and scoring thresholds | `topic_id`, `positive`, `negative`, `priority_thresholds.high/medium` | `extensions` | Topic ID |
| Ontology / `ontology.json` | Concepts and directed transfer relations | `topic_id`, `concepts`, `relations` | Concept aliases default to label; `extensions` | Profile or ontology IDs |
| Phase / `phase.json` | Current priority IDs | `topic_id`, unique `priority_question_ids`, unique `priority_method_ids` | `extensions` | Profile question/method IDs |
| Memory / `attention-memory.json` | Explicitly confirmed local preferences | unique `preferred_concept_ids` | `topic_id`, `extensions`; absence means no preference boost | Profile or ontology IDs |
| Delivery / `delivery.json` | Output and notification preferences | `topic_id`, `output.format/relative_path`, `notification.enabled/provider` | `extensions`; absence leaves caller-supplied output/provider behavior | Topic ID; safe relative output path |

The only output format currently supported is `json`. Transfer relation types are `direct_reference`, `concept_transfer`, `method_transfer`, and `application_transfer`. Rule dimensions are `question`, `method`, `application`, and `general`; weights and thresholds range from 0 to 1,000,000,000. Notification provider `none` cannot be enabled. Feedback decisions are `keep`, `must_read`, `later`, and `discard`.

Ontology relations may optionally specify `scope` (`cross_species`,
`cross_domain`, or `within_domain`) and user-owned `from_domain`/`to_domain`
labels. These fields add explanation context; the engine contains no fixed
species or scientific field. A configured transfer is a candidate rationale,
not proof of biological or methodological applicability.

Unknown fields are rejected. Every document and nested entry may use `extensions`, an object with namespaced application keys for user-defined metadata; the core ignores these values. This prevents extension metadata from acquiring accidental scoring semantics.

Users should maintain their actual research configurations outside the public source repository.

Operational scan frequency is separate from scientific Topic, Focus, Profile,
Rules and Ontology. The optional one-shot scheduler reads its own JSON file;
its entries name relative configuration directories and per-provider hour
intervals. A null interval means 24 hours for a legacy Topic; newly initialized
Topics use the explicit weekly workflow and its review gate. See
[deployment](deployment.md) and [research workflow](research-workflow.md).
The eight research and delivery configuration schemas keep their v1 meaning.

Feedback records contain `topic_id`, `paper_key`, `decision`, `reason`, and an RFC 3339 `recorded_at` value. Existing records and the complete proposed update must pass the packaged schema before the feedback file is replaced. Invalid records are preserved for user correction; no automatic migration occurs.
