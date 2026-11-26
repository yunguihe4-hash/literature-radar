# Privacy boundary

The distributable source and packaged examples contain only fictional demonstration data. Internal audits, development artifacts, private configurations, feedback, attention memory, candidate packets, run records, databases, user notes, paths and credentials belong outside the public project. No runtime default points to a private home directory or workspace.

Before each publication, review every selected file and generated artifact, scan for secrets and absolute paths, inspect examples and tests, and check all reachable Git history. `.gitignore` is a guard for new files, not a substitute for inspecting tracked files or release archives. Historical private development material stays outside the public Git history.

The production database also contains paper events, selected recommendation
evidence, a notification outbox and schedule state. These are user-owned data
and must stay outside any public repository, CI artifact and release package.
The normal online summary omits full abstracts. Abstract persistence is
opt-in. The public CI contains only independent fictional fixtures and sets
`LITERATURE_RADAR_OFFLINE_TEST=1` to prevent academic requests.
