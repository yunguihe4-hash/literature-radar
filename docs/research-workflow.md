# Private Topic, Bootstrap review and weekly monitoring

The commands below describe the workflow. Replace every `PRIVATE_*` placeholder
with a user-owned directory outside the source checkout and installed release.
No command in this document starts a daemon. Online searches occur only when a
user explicitly runs `bootstrap run`, `bootstrap resume` or `weekly run`
without `--dry-run`.

## 1. Create a Topic

Interactive intake:

```sh
literature-radar topic init --config-root PRIVATE_CONFIG_ROOT
```

The wizard asks for a name, goal, scientific questions, target and model
organisms, methods, applications, transfer directions, exclusions, journal
subject categories and notification choice. A Latin-letter
Topic ID is required. For a noninteractive run, pass repeated `--question`,
`--target-species`, `--method` and other options shown by `topic init --help`,
or use `--input PRIVATE_INTAKE.json`. The import format uses keys such as
`id`, `name`, `goal`, `questions`, `target_species`, `model_species`, `methods`,
`applications`, `transfer_directions`, `exclusions`, `journal_categories`,
`search_terms`, `sources` and `notification`. An old `weekly_alert_limit`
field remains readable for compatibility but never limits Weekly output.

`topic init` writes eight Schema v1 configuration files, empty Memory and
Feedback, and a private workflow state. It validates all files and references
before installing them. It refuses an existing Topic ID. Files use mode 600
and directories mode 700. The source and installed release are rejected as
private destinations.
Target species receive candidate `direct_reference` ontology links; model
species receive candidate `method_transfer` links to the entered questions.
These links guide review and never prove transfer on their own. Non-direct
recommendations require separate abstract evidence, as described below.

```sh
literature-radar topic validate --config-dir PRIVATE_CONFIG_ROOT/fictional-signal
literature-radar topic show --config-dir PRIVATE_CONFIG_ROOT/fictional-signal
literature-radar topic list --config-root PRIVATE_CONFIG_ROOT
```

The ontology accepts `direct_reference`, `method_transfer`,
`concept_transfer` and `application_transfer`. A relation is a candidate
explanation. A shared method name alone is insufficient: missing article
species, question, method or excerpt remains null and requires review.
For a non-direct relation, `extensions.transfer_evidence_phrase` must also
appear in the article abstract as a separate, checkable transfer premise;
otherwise its status remains `NEEDS_REVIEW` even when method names match.
Research Attention adds a separate A/B/C/D relevance order without changing
legacy score fields or stored paper identities.

## 2. Supply journal editions, if licensed

The generic [CSV template](../templates/journal-quality.template.csv) contains
headers only. A user may import a licensed CSV or JSON file:

```sh
literature-radar journal import --input PRIVATE_JOURNALS.csv --registry PRIVATE_JOURNAL_REGISTRY.json
literature-radar journal show --registry PRIVATE_JOURNAL_REGISTRY.json --issn 1234-567X
```

The importer validates the complete input before changing the registry. Each
entry retains ISSN/ISSN-L, aliases, CAS major and minor categories and
partitions, JCR categories with individual quartiles, edition years and an
explicit usage basis. ISSN/ISSN-L is required for verified matching. A title
or alias match alone remains `UNVERIFIED` pending identity review. Preprints
are `UNVERIFIED`; that label is not a low-quality grade. CAS and JCR editions
from different years are retained separately and receive no combined claim.
The combined classes are `DUAL_HIGH`, `DUAL_UPPER`, `SINGLE_SYSTEM_HIGH`,
`VERIFIED_OTHER`, `MIXED` and `UNVERIFIED`.
An optional private `topic.extensions.journal_quality_policy` object may set
`dual_high_cas_max`, `dual_high_jcr_max`, `upper_cas_max`, `upper_jcr_max`
and `conflict_gap` (integer values 1–4). The default thresholds are 1, 1,
2, 2 and 2 respectively. The report retains both original systems and
explains the combined class.

The program does not fetch or bundle CAS/JCR data. With no user-supplied
registry, both systems and the combined quality are unverified.
Separate CAS and JCR rows for the same verified ISSN and edition may be
combined for display. The program keeps each raw category and edition. If an
ISSN maps to conflicting journal titles or two records disagree within one
edition, identity or partition status remains `CONFLICT`/`UNVERIFIED` for
manual review; no favorable entry is chosen. CAS 1 + JCR Q3 and CAS 3 + JCR
Q1 are `MIXED` under the default policy. A name match without ISSN evidence
remains unverified even when the alias is exact.
When several JCR subject categories are relevant, the combined class uses
the least favorable of those quartiles while retaining every original
category. Without a Topic category selection, it uses the least favorable
listed category, avoiding a silent best-category choice.

## 3. Build a Topic-specific initial review

```sh
literature-radar bootstrap run --config-dir PRIVATE_CONFIG_ROOT/fictional-signal \
  --database PRIVATE_DATABASE --output-dir PRIVATE_REVIEW_DIR --dry-run
```

Remove `--dry-run` only when an online search is authorized. Bootstrap searches
a rolling five-calendar-year range in a temporary working SQLite database.
After complete source coverage it retains only the selected A/B/C papers
(up to 30 by default), their identity/source evidence and review cards in the
Topic database, then removes the working database. An incomplete run keeps
that temporary database so its exact Provider checkpoints can resume. Do not
ship or import a maintainer's private database. The code uses paginated SQLite checkpoints
and marks incomplete or failed source coverage explicitly. Resume the same
window, database and output directory with `bootstrap resume`. Use
`bootstrap status` to inspect lifecycle state and `bootstrap export-review`
to regenerate machine and Markdown reports. Export never overwrites an
existing review TSV, because it may contain human edits.

For multiple independent Topics, repeat `--config-dir` with `bootstrap run-many`
and supply one `--output-root`. Each Topic receives its own report directory;
failure in one Topic is recorded without cancelling the others. `--dry-run`
plans queries without online requests.

The private output includes JSON, Markdown, coverage JSON, a Markdown card
index and one Obsidian review card per selected paper. TSV is retained only
for machine export and historical compatibility. Publication, first-online and preprint release dates are distinguished
from indexing and update dates. Missing or implausible publication dates are
not inferred from indexing dates. A related preprint and formal article are
linked and not recommended twice. D records never fill the 30-paper target.
If fewer than 30 relevant papers are found, the report says `PARTIAL` when
source coverage is complete or `INCOMPLETE` when a source remains unresolved.
Bootstrap does not queue a batch of HIGH notifications.

## 4. Import human decisions and confirm Memory

Open the generated Markdown cards in Obsidian with Literature Radar Bridge
enabled. Each card keeps its Properties, abstract, recommendation evidence and
four Bridge buttons under “我的评价”. Select one: 很重要 (`must_read`), 有用
(`keep`), 一般 (`later`) or 没用 (`discard`). An unselected card remains
`PENDING`. Bridge records changes in a local Outbox; the older card importer
below remains available for historical checkbox cards.
New seed cards live under `date/topic_id/BOOTSTRAP_SEED` in the generated
output and `Bootstrap/topic_id/BOOTSTRAP_SEED` in the Vault. They do not receive
Weekly HIGH/MEDIUM/LOW labels and their presence is not human feedback. Older
Bootstrap cards already in HIGH/MEDIUM/LOW remain in place. Import explicit
human decisions with:

```sh
literature-radar feedback import-cards --config-dir PRIVATE_CONFIG_ROOT/fictional-signal \
  --cards-dir PRIVATE_REVIEW_DIR/cards --database PRIVATE_DATABASE
literature-radar feedback summary --config-dir PRIVATE_CONFIG_ROOT/fictional-signal
literature-radar memory propose --config-dir PRIVATE_CONFIG_ROOT/fictional-signal \
  --output PRIVATE_MEMORY_PROPOSAL.json
```

Import verifies Topic, paper ID, paper key and DOI against the Topic database.
Repeating the same decision is idempotent; a changed decision conflicts with
the prior import and requires explicit resolution. Individual
decisions remain in Feedback. Only repeated positive decisions from at least
two different papers create a Memory candidate; `later` and `discard` do not
create global negative rules. A proposal changes no Memory. Edit its
`confirmed` array to include only candidates you approve, then run:

```sh
literature-radar memory apply --config-dir PRIVATE_CONFIG_ROOT/fictional-signal \
  --proposal PRIVATE_MEMORY_PROPOSAL.json --confirm
```

This explicit confirmation is required to apply a Memory proposal. Topic,
Focus, Rules and the legacy score remain unchanged. Weekly activation can
proceed once Bootstrap is review-ready; feedback and Memory review may
continue afterward.

## 5. Activate weekly monitoring

Completed Bootstrap in the review-ready state permits normal activation.
A user may explicitly skip Bootstrap, but must provide a reason recorded
in the private workflow state:

```sh
literature-radar weekly activate --config-dir PRIVATE_CONFIG_ROOT/fictional-signal
literature-radar weekly run --config-dir PRIVATE_CONFIG_ROOT/fictional-signal \
  --database PRIVATE_DATABASE --output-dir PRIVATE_WEEKLY_DIR --dry-run
```

The same `weekly run` command is used manually; the supported all-Topic
user timer runs `weekly run-scheduled` after `weekly systemd-generate`.
No unit is installed or enabled by the program. The default primary window is seven days, with a
14-day overlapping query window. Every A/B/C paper with a new, late indexed
or material update event is included, with no fixed weekly count. Journal
rankings and impact factors are never consulted by Weekly. Each Topic gets
its own Markdown report and cards; the `--cards-dir` option can point to an
staged card directory. A bound run then delivers cards to the selected Vault.
HIGH papers get desktop notifications when enabled;
successful sends are deduplicated by Topic and paper. MEDIUM and LOW keep
cards with feedback controls. Failed deliveries remain available for explicit
retry. The legacy `schedule` command remains for
older schedules; it will not run a new workflow Topic behind the weekly gate.
`weekly run-many` accepts repeated `--config-dir` values and writes separate
Topic reports under `--output-root`; `--cards-root` can direct all Topic cards
to one staged card root while keeping `date/topic_id/HIGH|MEDIUM|LOW` isolation.
