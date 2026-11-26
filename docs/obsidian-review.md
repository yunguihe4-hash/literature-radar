# Obsidian 阅读与反馈

Literature Radar Bridge 0.2.3 是第一方插件，最低要求 Obsidian desktop 1.13.1。它读取由 Literature Radar 管理的 Bootstrap 和 Weekly 卡片，在 `literature-radar-feedback` 代码块中提供四个按钮，并根据卡片的 YAML Properties 更新 Dashboard。无需安装 Meta Bind、Metadata Visuals 或 Dataview；如果用户自行安装这些插件，它们也不是 Core 或 Bridge 的依赖。

先使用 `literature-radar obsidian setup` 绑定自己的 Vault，并将绑定文件放在 Topic 配置根目录的 `obsidian.json`。已有 Vault 只使用 `Literature-Radar/` 命名空间。安装脚本只写所选 Vault 的 `.obsidian/plugins/literature-radar-bridge/`：

```sh
python3 deploy/obsidian-bridge/install.py --vault /path/to/MyVault
```

已有 Bridge 的升级使用 `--upgrade`，脚本先备份旧插件目录，并保留插件自己的 `data.json` 和 `outbox.json`。它不修改其他插件，也不会替用户在 Obsidian 中启用插件；安装后请在 **设置 → Community plugins** 中手动启用。

Bootstrap 种子卡片使用 `Bootstrap/<topic>/BOOTSTRAP_SEED/`；新 Weekly 卡片使用 `Weekly/YYYY-MM-DD/<topic>/<HIGH|MEDIUM|LOW>/`。Bridge 继续识别旧的 `Weekly/<topic>/<tier>/` 路径。首次生成的卡片可用 `obsidian deliver --binding-file BINDING.json --cards-dir CARDS目录 --section Bootstrap` 复制到 Vault。程序保留人工评价和笔记区，对已有卡片更新前创建备份；没有所有权标记的冲突文件不会被覆盖。

按钮选择“很重要 / 有用 / 一般 / 没用”后，Bridge 设置 `feedback_decision`、`review_status` 和待同步状态，并向隐藏 Outbox 追加事件。正式反馈导入：

```sh
literature-radar feedback sync-outbox \
  --binding-file /path/to/private/config/obsidian.json \
  --config-dir /path/to/private/config/topic-a \
  --config-dir /path/to/private/config/topic-b \
  --database /path/to/private/radar.sqlite3
```

同步核对 Topic、论文 ID、DOI、paper key 和卡片路径。重复事件幂等，改变评价会写入历史。未评价卡片仍为 PENDING；有问题的卡片会显示异常状态。用户可用 `feedback summary --config-dir TOPIC_DIR` 和 `attention training-status --config-dir TOPIC_DIR` 查看各 Topic 的状态。种子只有经过人工评价才进入反馈；每 30 篇不同论文的真实反馈形成一个训练批次，模型激活仍有质量门槛。
