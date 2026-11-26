# Linux installation and automatic scheduling

This is the supported deployment path for a new user: Linux, Python 3.11+, and a systemd user manager. The source, virtual environment, private data, and Obsidian Vault may live in different locations. No AI_OS or Codex installation is needed.

## Install and initialize

From a source checkout or unpacked release:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
export LR_CLI="$(pwd)/.venv/bin/literature-radar"
export LR_DATA="$HOME/.local/share/literature-radar"
mkdir -p "$LR_DATA/config" "$LR_DATA/output/bootstrap" \
  "$LR_DATA/output/weekly" "$LR_DATA/output/cards" "$LR_DATA/backups"
"$LR_CLI" database init --database "$LR_DATA/radar.sqlite3"
"$LR_CLI" topic init --config-root "$LR_DATA/config"
"$LR_CLI" topic list --config-root "$LR_DATA/config"
```

Do not place user Topics, SQLite, feedback, models, backups, or credentials in the source tree. A new Topic may be created from the [generic intake template](../templates/topic-intake.template.json) copied to a private path, or with the interactive wizard. The database initializer is safe to call again on the same database; it creates the schema without fetching papers.

## Connect Obsidian

The optional Bridge works with Obsidian desktop 1.13.1 or newer. Bind your own Vault, then install only the first-party plugin assets. A dedicated Vault is the simplest path:

```sh
"$LR_CLI" obsidian setup --mode create --parent-dir "$HOME/Documents" \
  --binding-file "$LR_DATA/config/obsidian.json" \
  --name Literature-Radar --topic-id TOPIC_ID --yes
python3 deploy/obsidian-bridge/install.py --vault "$HOME/Documents/Literature-Radar"
"$LR_CLI" obsidian status --binding-file "$LR_DATA/config/obsidian.json"
"$LR_CLI" obsidian doctor --binding-file "$LR_DATA/config/obsidian.json"
```

Open that folder as a Vault in Obsidian and enable **Literature Radar Bridge** in Community plugins. For an existing Vault, use `obsidian setup --mode existing --vault VAULT_PATH --binding-file "$LR_DATA/config/obsidian.json" --yes` and `install.py --vault VAULT_PATH`. The application checks namespace conflicts. The installer refuses an unrelated plugin directory and never changes other plugins or Obsidian's enabled plugin list. An existing Bridge requires `--upgrade`; it backs up the plugin folder and retains private `data.json` and outbox. The plugin code is independently distributable as `manifest.json`, `main.js`, and `styles.css`.

The standalone Obsidian command defaults to `$HOME/.config/literature-radar/obsidian.json` on Linux. **Weekly expects the binding at `CONFIG_ROOT/obsidian.json`**, so the commands above explicitly put it there. Use that same `--binding-file` for status, delivery, and feedback. Generated cards go first to the user-selected output/cards path; a bound run delivers the touched cards into the Vault's Literature Radar namespace. Bootstrap cards can be delivered after binding with `obsidian deliver --binding-file "$LR_DATA/config/obsidian.json" --cards-dir "$LR_DATA/output/bootstrap/TOPIC_ID/cards" --section Bootstrap`.

## Bootstrap and a first Weekly

Run a dry-run before allowing provider requests:

```sh
"$LR_CLI" bootstrap run --config-dir "$LR_DATA/config/TOPIC_ID" \
  --database "$LR_DATA/radar.sqlite3" \
  --output-dir "$LR_DATA/output/bootstrap/TOPIC_ID" --dry-run
"$LR_CLI" weekly status --config-dir "$LR_DATA/config/TOPIC_ID"
```

Repeat Bootstrap without `--dry-run` when ready. If a Provider times out, `bootstrap resume` uses the same paths and checkpoints. Review only the selected seed cards; they are not training labels. Weekly can be activated when Bootstrap reaches its review-ready state; [feedback and Memory review](research-workflow.md) may continue afterward:

```sh
"$LR_CLI" weekly activate --config-dir "$LR_DATA/config/TOPIC_ID"
"$LR_CLI" weekly run --config-dir "$LR_DATA/config/TOPIC_ID" \
  --database "$LR_DATA/radar.sqlite3" \
  --output-dir "$LR_DATA/output/weekly/TOPIC_ID" --dry-run
```

Run without `--dry-run` for a real manual Weekly. For a planned all-Topic run, use `weekly run-scheduled`. It discovers enabled Topics under one config root, keeps separate per-Topic state, locks concurrent invocations, catches up missed windows, and resumes partial Provider coverage. `NOT_DUE` is a normal result. `PARTIAL_SUCCESS` means some work succeeded while other work remains pending.

## Generate and manage a user timer

Generate a uniquely named unit pair after a manual dry-run. The generator refuses to overwrite existing service or timer files. The default calendar is Monday at 09:00 in `America/New_York`. Set your own IANA timezone, weekday (0=Monday through 6=Sunday), and 24-hour local time; pass the same values to manual `weekly run-scheduled` if using that command separately.

```sh
"$LR_CLI" weekly systemd-generate \
  --unit-dir "$HOME/.config/systemd/user" --unit-name literature-radar-open-weekly \
  --cli "$LR_CLI" --config-root "$LR_DATA/config" \
  --database "$LR_DATA/radar.sqlite3" \
  --output-root "$LR_DATA/output/weekly" \
  --cards-dir "$LR_DATA/output/cards" \
  --timezone America/New_York --weekday 0 --local-time 09:00
systemctl --user daemon-reload
systemctl --user enable --now literature-radar-open-weekly.timer
systemctl --user status literature-radar-open-weekly.timer
systemctl --user list-timers literature-radar-open-weekly.timer
journalctl --user -u literature-radar-open-weekly.service -n 100 --no-pager
```

Disable only your own unit with `systemctl --user disable --now literature-radar-open-weekly.timer`. The timer uses `Persistent=true` so a missed host trigger is run after the user manager resumes. The CLI also recovers any missed per-Topic planned windows. No separate timer is needed per Topic. Timer and service are generated in your user unit directory and are not installed or enabled by the Python package.

To change schedule settings, disable the timer, save and review the existing unit pair, remove only that pair, regenerate with the new values, then reload and re-enable. A new unit name also works and avoids replacing files. Do not enable both schedules for the same Topics.

## Backup, upgrade, uninstall

`backup --database STATE_DB --output NEW_BACKUP.sqlite3` and `restore --backup BACKUP.sqlite3 --output NEW_STATE.sqlite3` refuse to overwrite existing destinations and verify database integrity. Also copy private Topic, Focus, feedback, Memory, workflow state, and Vault files; SQLite alone is incomplete. See [backup and restore](backup-restore.md).

For an upgrade, disable your timer, verify a full backup, install the new wheel into a new virtual environment, validate your Topics, and point only your generated service at the new CLI. To uninstall, disable your timer, remove its two user unit files and virtual environment, then run `systemctl --user daemon-reload`. Keep or delete private data and Vault separately according to your own retention decision. Scheduling has been verified on Linux/systemd; other platforms have not been independently validated.
