# Release notes

## v0.3.3 — Historical card migration stability

- Preserve the generated research question and alignment text on a second migration pass; normalize legacy JSON evidence into readable source excerpts. Repeated dry runs now report zero changes after application.
- Obsidian Bridge remains at 0.2.4. Discovery, Attention, feedback, and scheduling are unchanged.

## v0.3.2 — Obsidian card reading layout

- Show the verified journal name and a single evidence based “为什么推给我” section in new cards. Keep publication date precision and full text verification state in the pipeline while removing them from the reading body.
- Carry Research Intelligence question, phase and method evidence through Weekly and Bootstrap card delivery without changing ranking or selection.
- Show six core Properties on Literature Radar cards through Bridge 0.2.4. A Bridge command reveals all internal YAML fields; other notes retain their Obsidian display settings.
- Add an offline, dry run first migration for managed historical cards. It backs up changed Vault and archive cards, preserves feedback and everything outside the generated block, and is idempotent.

## v0.3.1 — CI compatibility hotfix

Fix Python 3.11 parsing of alert notifications and isolate the Obsidian
registry path test from runner environment variables. Correct disabled-Topic
fixtures to use the supported top-level flag. All supported Python
versions now finish the offline CI matrix independently. Update checkout and
setup-python Actions to supported Node.js 24 releases. Obsidian Bridge remains
at 0.2.3. CI installs the declared setuptools build backend explicitly for
offline wheel checks; notification content and runtime registry-path precedence
are unchanged.

## v0.3.0 — Initial public release

Literature Radar provides independent research Topics, five-source discovery
(OpenAlex, Crossref, Europe PMC, PubMed, and bioRxiv), a roughly 30-paper
Bootstrap seed strategy, and weekly retrieval. Its seven-dimension Attention
ranking produces HIGH, MEDIUM, and LOW recommendations. Related papers with
weak evidence may receive a LOW fallback; zero recommendations is valid.

Per-Topic Provider checkpoints recover incomplete windows. Weekly scheduling
supports `NOT_DUE`, partial success, catch-up windows, and persistent Linux
systemd user timers. Obsidian Bridge 0.2.3 supplies cards, a Dashboard, and
human feedback controls; Topic-scoped feedback can train later ranking batches.

Linux with Python 3.11+ and systemd is the validated deployment. The
maintainer has accepted a real Weekly in a personal environment. Fresh-user
installation and two-Topic Bootstrap, Weekly, feedback, Obsidian delivery, and
timer behavior were validated with isolated offline fixtures. Other platforms
and a new user's live five-provider run have not been independently verified.
Provider timeouts can leave a partial checkpoint for a later run. Source APIs
do not guarantee complete coverage, and papers with no valid scientific
relation yield no recommendation. No full-text papers, licensed journal
rankings, personal data, or historical search corpus are shipped.
