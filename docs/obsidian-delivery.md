# Obsidian Vault 设置与文献交付

Bridge 按钮、Dashboard 和反馈流程见
[`obsidian-review.md`](obsidian-review.md)。

运行 `literature-radar obsidian setup`，选择以下一种模式：

1. **Create a new dedicated vault**（推荐）：输入父目录与 Vault 名称（默认 `Literature-Radar`），预览路径，确认后创建。
2. **Select an existing vault**：从已注册候选中选择，或输入已有 Vault 路径。必须含 `.obsidian`。
3. **Configure later**：暂不绑定；私有配置记录延期选择，运行结果标记 `OBSIDIAN_NOT_CONNECTED`。

即使仅发现一个 Vault，也不会自动绑定。新建 Vault 不会自动注册到 Obsidian；完成后请在 Obsidian 中选择“打开文件夹作为 Vault”，打开输出的 `vault_root`。首次交互式 Bootstrap 或 Weekly 也显示上述三种选择；非交互运行只暂存卡片并报告未连接。

非交互 CLI 示例（需要明确的 `--yes`）：

```sh
literature-radar obsidian setup --mode create --parent-dir ~/Documents --name Literature-Radar --yes
literature-radar obsidian setup --mode existing --vault /path/to/MyVault --yes
literature-radar obsidian setup --mode later
literature-radar obsidian status
literature-radar obsidian doctor
literature-radar obsidian open
```

`--vault` 单独使用仍按旧 CLI 习惯表示“选择已有 Vault”，但也需要 `--yes` 或交互确认。可多次传入 `--config-dir` 或 `--topic-id`，在新 Vault 中初始化对应 Topic 目录。程序不硬编码 Topic 数量。Windows 路径示例：`C:\Users\name\Documents\MyVault`；macOS/Linux 示例：`~/Documents/MyVault`。

独立 Vault 的交付根目录就是 `<vault_root>/`。选择已有 Vault 时，交付根目录为 `<vault_root>/Literature-Radar/`。两种模式的内部结构一致：

```text
Literature-Radar/
  dashboard.md
  Bootstrap/<topic_id>/BOOTSTRAP_SEED/
  Weekly/YYYY-MM-DD/<topic_id>/<HIGH|MEDIUM|LOW>/
  System/delivery/
  System/feedback/
```

已有 Vault 的 `Literature`、`Knowledge`、`Projects`、`System` 等目录不被修改。若 `Literature-Radar/` 已含其他文件且没有本程序的所有权标记，设置会拒绝并要求解决冲突。文献卡片及 Dashboard 仅在该命名空间内写入。人工评价和笔记位于卡片的 `## 我的评价` 之后；重复交付保留这一区域，更新前备份已有卡片。未知目标文件不会被覆盖。

私有绑定文件默认位于平台用户配置目录的 `literature-radar/obsidian.json`，权限为 600；可使用 `LITERATURE_RADAR_PRIVATE_CONFIG_DIR` 指定目录。字段包括 `vault_root`、`delivery_root`、`vault_mode`、`vault_identifier`、`binding_created_at`、`schema_version`。旧版仅指向 `Literature/` 的绑定不继续交付，需通过 `setup` 明确重新选择；原绑定及 manifest 在重绑前备份。Vault 移动后程序报告原路径，不会自动切换。

已有暂存卡片必须在用户明确选择 Vault 且确认迁移后，才可运行：

```sh
literature-radar obsidian migrate \
  --source-dir /path/to/bootstrap/topic-a \
  --source-dir /path/to/bootstrap/topic-b \
  --expected-per-topic 30 --database /path/to/verified-work.sqlite3 --yes
```

`migrate` 先备份每个 Topic 的完整暂存报告目录，再复制卡片，逐篇核对 Topic、论文身份、分析内容和反馈区域，校验 Dashboard，并保留原暂存文件。提供 `--database` 时，还会核对工作数据库中的论文身份并补充期刊信息。交付后的卡片可在「我的评价」中点击 Bridge 按钮；Bridge 把变更写入隐藏 Outbox，随后用 `literature-radar feedback sync-outbox` 导入正式反馈。`feedback import-cards` 保留为旧卡片兼容入口。`obsidian deliver` 用于普通卡片目录；它只会向已绑定的命名空间写入。

反馈导入的 `--config-dir`、`--cards-dir` 必须分别指向同一 Topic 的私有配置和 Vault 卡片目录。提供 `--binding-file` 时，导入后会按真实卡片状态刷新 Dashboard 和 Topic 索引；未填写评价的卡片仍为 PENDING。重复导入是幂等的，多选会报错，Memory 不会自动更新。

`status` 显示当前路径、模式、Topic 目录及数量；`doctor` 检查权限、所有权标记、路径边界和 manifest；`open` 优先使用 Obsidian URI，失败时打开本地 `Literature-Radar/`。Linux 读取常规、Flatpak 与 Snap 的 Obsidian 注册信息；Windows 读取 `%APPDATA%\obsidian\obsidian.json`；macOS 读取 `~/Library/Application Support/obsidian/obsidian.json`。这些注册信息只用于候选展示。
