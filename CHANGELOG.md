# Release notes

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
