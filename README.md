# Literature Radar

**Core v0.3.3** · **Obsidian Bridge v0.2.4** · **MIT**

Literature Radar helps researchers discover recent papers, inspect why they were recommended, and record reading decisions. Every research Topic has its own searches, provider checkpoints, recommendations, feedback, and training history. The core runs locally; online requests occur only when you invoke discovery, Bootstrap, or Weekly.

It grew out of a literature workflow in AI_OS. This package runs independently of AI_OS, Codex, ChatGPT, and the maintainer's private data. The optional first-party Obsidian Bridge adds review controls and a Dashboard in your own Vault.

**Supported deployment:** Linux with Python 3.11 or newer. The automatic schedule uses a systemd user timer. Other operating systems have not been independently validated.

## Quick start

Use private data paths outside the checkout. These examples use `$HOME/.local/share/literature-radar`; another writable path is fine.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
export LR_DATA="$HOME/.local/share/literature-radar"
mkdir -p "$LR_DATA/config" "$LR_DATA/output" "$LR_DATA/backups"
.venv/bin/literature-radar topic init --config-root "$LR_DATA/config"
.venv/bin/literature-radar topic list --config-root "$LR_DATA/config"
.venv/bin/literature-radar database init --database "$LR_DATA/radar.sqlite3"
```

The interactive Topic wizard requests an ID, name, long-term goal, research questions, search terms, methods, organisms, transfer directions, exclusions, providers, and notification choice. You can instead copy and edit the [generic intake template](templates/topic-intake.template.json) outside the checkout, then run `topic init --config-root "$LR_DATA/config" --input "$LR_DATA/my-topic.json"`. Run it again for each additional Topic. Change current focus with `topic set-focus --config-dir TOPIC_DIR --search-term "new phrase" --reason "why it changed"`; previous Focus snapshots and feedback remain intact.

```sh
.venv/bin/literature-radar topic validate --config-dir "$LR_DATA/config/TOPIC_ID"
.venv/bin/literature-radar bootstrap run --config-dir "$LR_DATA/config/TOPIC_ID" \
  --database "$LR_DATA/radar.sqlite3" --output-dir "$LR_DATA/output/bootstrap/TOPIC_ID" --dry-run
```

Remove `--dry-run` to search the configured providers. Bootstrap selects up to 30 relevant seeds from a rolling historical range. Its temporary working database retains partial checkpoints for `bootstrap resume`; the lasting Topic database contains only selected seeds and evidence. Inspect progress with `bootstrap status --config-dir TOPIC_DIR`. Seeds are review material, not feedback or training labels. Installation and tests do not require a five-year network search.

After Bootstrap completes, you can activate Weekly while continuing to review seeds. Import explicit decisions and confirm any Attention Memory proposal using the [research workflow](docs/research-workflow.md). To deliberately skip Bootstrap, `weekly activate --config-dir TOPIC_DIR --skip-bootstrap --reason "reason"` records that choice:

```sh
.venv/bin/literature-radar weekly activate --config-dir "$LR_DATA/config/TOPIC_ID"
.venv/bin/literature-radar weekly run --config-dir "$LR_DATA/config/TOPIC_ID" \
  --database "$LR_DATA/radar.sqlite3" --output-dir "$LR_DATA/output/weekly/TOPIC_ID" --dry-run
```

Remove `--dry-run` for a manual online Weekly run. `weekly run-scheduled --config-root "$LR_DATA/config" --database "$LR_DATA/radar.sqlite3" --output-root "$LR_DATA/output/weekly"` discovers all enabled due Topics. It uses a run lock and persistent per-Topic schedule state. Later invocations catch up missed planned weeks and resume incomplete source windows without repeating completed ones.

## Obsidian and feedback

`obsidian setup` binds a dedicated or existing Vault. An existing Vault gets an isolated `Literature-Radar/` namespace; ownership checks protect unrelated files and manual notes. Install the [Bridge](deploy/obsidian-bridge/README.md) only into the selected Vault. It requires Obsidian desktop 1.13.1 or newer; no private plugin data, credentials, or feedback are bundled.

Bootstrap cards use `Bootstrap/<topic>/BOOTSTRAP_SEED/`. New Weekly cards use `Weekly/YYYY-MM-DD/<topic>/<HIGH|MEDIUM|LOW>/`; older undated paths remain readable. The Bridge presents four choices per card, refreshes the Dashboard, and writes a local outbox. Import decisions with `feedback sync-outbox --binding-file BINDING.json --config-dir TOPIC_DIR --database STATE_DB`. Training remains separate per Topic; each 30 distinct human reviewed papers forms the next batch, subject to existing model activation safeguards. Inspect with `attention training-status --config-dir TOPIC_DIR` and `attention training-history --config-dir TOPIC_DIR`.

**HIGH** calls for prompt attention, **MEDIUM** is worth reviewing, and **LOW** has weaker but valid scientific relevance. A weakly related paper may fill a LOW fallback; a wholly unrelated paper cannot. Zero recommendations is valid. Journal impact factors, JCR, and journal grades do not affect scores or notifications.

## Providers and operation

The five independent providers are OpenAlex, Crossref, Europe PMC, PubMed, and bioRxiv. Optional private environment values include `OPENALEX_API_KEY`, `CROSSREF_MAILTO`, `NCBI_API_KEY`, `NCBI_TOOL`, and `NCBI_EMAIL`. See [provider configuration and terms](docs/api-providers.md). Timeouts may yield `PARTIAL_SUCCESS` while successful providers commit useful results. `NOT_DUE` means no enabled Topic has reached its planned run. Checkpoints permit later recovery; no provider guarantees complete weekly coverage.

Paths are user-selected: Topic bundles and feedback JSON in the config root, SQLite at `--database`, reports at `--output-dir` or `--output-root`, and cards in the bound Vault. Back up SQLite with `backup --database STATE_DB --output NEW_BACKUP.sqlite3` and restore to a new path with `restore --backup BACKUP.sqlite3 --output NEW_STATE.sqlite3`. Copy Topic and feedback files separately. See [backup and recovery](docs/backup-restore.md), [deployment](docs/deployment.md), and [privacy](docs/privacy.md).

The supported timer setup, status, logs, enable, and disable steps are in [deployment](docs/deployment.md). To uninstall, disable only your own generated unit and remove the virtual environment. Keep or separately remove your data and Vault after backing them up.

## Development and release status

Examples and fixtures are fictional. The distribution contains no paper PDFs, private corpus, user models, personal Topics, or API keys. Source and release packages are licensed under [MIT](LICENSE), with upstream credits in [third-party notices](THIRD_PARTY_NOTICES.md). See the [Chinese guide](docs/README.zh-CN.md) and [release notes](CHANGELOG.md).

The maintainer has run a real Weekly on a personal installation. Fresh installation, two-Topic Bootstrap, Weekly, feedback isolation, Obsidian delivery, and systemd checks were accepted in an isolated offline mock environment. A fresh user's live five-provider run has not been independently verified. Linux/systemd is the validated deployment; other platforms are unverified. Partial Provider runs and checkpoint recovery remain supported. A Topic with no scientifically related papers may receive zero recommendations.
