# Literature Radar Bridge

This is the first-party Obsidian desktop plugin (version 0.2.4; Obsidian
1.13.1 or newer). From the source checkout, install it into a Vault that
already contains a `.obsidian` directory:

```sh
python3 deploy/obsidian-bridge/install.py --vault /path/to/MyVault
```

Enable **Literature Radar Bridge** in Obsidian Community plugins. The
installer does not change the enabled-plugins list, other plugins, or user
notes. If the Bridge already exists, pass `--upgrade`; the installer first
backs up its directory under `.obsidian/literature-radar-backups/` and
preserves private `data.json` and `outbox.json`. The distribution contains
only `manifest.json`, `main.js`, and `styles.css`, plus this installer.

This first-party Obsidian plugin listens only to Literature Radar cards under
`Bootstrap/<topic>/<BOOTSTRAP_SEED|HIGH|MEDIUM|LOW>/`, historical
`Weekly/<topic>/<HIGH|MEDIUM|LOW>/`, and dated
`Weekly/<delivery-date>/<topic>/<HIGH|MEDIUM|LOW>/`.
It writes an append-only event list to its hidden `outbox.json`. The private
`literature-radar feedback sync-outbox` CLI validates paper identity and imports
events into Topic-scoped Feedback JSON. The plugin never accesses the database,
API credentials, or Attention Memory.

Set the local Topic configuration folder in the plugin settings after selecting
the Vault in Obsidian. The optional `LITERATURE_RADAR_CONFIG_ROOT` environment
variable provides an initial value. No machine path is bundled into the plugin.
The plugin renders its own four feedback buttons; Meta Bind and other
third-party Obsidian plugins are not required.
On Literature Radar cards, the Bridge shows the six reading Properties by
default. Use the **Toggle full card Properties** command to inspect every
internal YAML field. Other notes and the global Obsidian setting are unaffected.
