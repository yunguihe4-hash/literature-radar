# Literature Radar 中文使用指南

Literature Radar 为每个科研 Topic 独立检索、归并和推荐文献，保留来源证据与检索 checkpoint，并在本地记录人工评价和训练历史。它可独立于 AI_OS、Codex、ChatGPT 和开发者私人资料运行。普通使用需要 Linux 与 Python 3.11+；自动调度当前验证的是 systemd 用户服务。其他操作系统未完成独立验收。

## 1. 安装和初始化

在源码根目录执行。把私有配置和数据放在源码外：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
export LR_DATA="$HOME/.local/share/literature-radar"
mkdir -p "$LR_DATA/config" "$LR_DATA/output" "$LR_DATA/backups"
.venv/bin/literature-radar database init --database "$LR_DATA/radar.sqlite3"
.venv/bin/literature-radar topic init --config-root "$LR_DATA/config"
```

`topic init` 向导填写 Topic ID、名称、长期方向、研究问题、核心检索词、方法、物种、跨领域迁移方向、排除条件、五个 Provider 和通知选择。也可以先复制并编辑 [通用模板](../templates/topic-intake.template.json)，用 `--input 私有模板路径` 非交互创建。重复执行可创建任意数量 Topic；每个 Topic 单独运行、推荐和训练。当前 Focus 可通过 `topic set-focus --config-dir TOPIC_DIR --search-term "词组" --reason "修改原因"` 更新，历史 Focus 和反馈保留。

## 2. Bootstrap：首次约 30 篇

```sh
.venv/bin/literature-radar topic validate --config-dir "$LR_DATA/config/TOPIC_ID"
.venv/bin/literature-radar bootstrap run \
  --config-dir "$LR_DATA/config/TOPIC_ID" \
  --database "$LR_DATA/radar.sqlite3" \
  --output-dir "$LR_DATA/output/bootstrap/TOPIC_ID" --dry-run
```

先看 dry-run，去掉 `--dry-run` 才会真实访问 Provider。Bootstrap 在较长历史窗口内寻找高价值种子，默认最多保留 30 篇及必要证据；临时检索库在完成后清理。未完成时使用相同路径执行 `bootstrap resume`，并用 `bootstrap status --config-dir TOPIC_DIR` 查看状态。种子仅供阅读，不算人工反馈，也不直接参与 Trainer。安装与验收无需重新下载五年文献库。

打开卡片审查文献后，用 Bridge 按钮给出“很重要 / 有用 / 一般 / 没用”选择，或使用兼容的卡片导入命令。`feedback sync-outbox --binding-file BINDING.json --config-dir TOPIC_DIR --database STATE_DB` 将 Bridge Outbox 中的选择写入对应 Topic。Bootstrap 已完成并进入待审查状态时即可激活 Weekly；人工反馈和 Attention Memory 审查可随后逐步积累，详见[研究工作流](research-workflow.md)。如决定跳过 Bootstrap，可执行 `weekly activate --config-dir TOPIC_DIR --skip-bootstrap --reason "原因"`，该选择会留下记录。

## 3. Obsidian 与 Weekly

`obsidian setup --mode create --parent-dir 目录 --name Vault名 --binding-file "$LR_DATA/config/obsidian.json" --yes` 创建独立 Vault；已有 Vault 使用 `--mode existing --vault VAULT路径 --binding-file "$LR_DATA/config/obsidian.json" --yes`。Weekly 从 `CONFIG_ROOT/obsidian.json` 读取绑定，因此这里必须使用该路径。已有 Vault 只写自己的 `Literature-Radar/` 命名空间，并检查冲突。使用 `python3 deploy/obsidian-bridge/install.py --vault VAULT路径` 将 Bridge 0.2.3 装入所选 Vault，再在 Obsidian 社区插件设置中启用。Bridge 提供反馈按钮、Dashboard 和反馈状态，不需要 Meta Bind、Dataview 或 Metadata Visuals。安装脚本遇到现有 Bridge 时需要显式 `--upgrade`，先备份旧目录并保留私有 data.json 和 Outbox。

Bootstrap 卡片位于 `Bootstrap/<topic>/BOOTSTRAP_SEED/`；Weekly 新卡片位于 `Weekly/YYYY-MM-DD/<topic>/<HIGH|MEDIUM|LOW>/`。旧的无日期目录继续兼容。人工笔记区不会被重复交付覆盖。首次 Bootstrap 的暂存卡片可在绑定 Vault 后执行 `obsidian deliver --binding-file BINDING.json --cards-dir CARDS目录 --section Bootstrap` 交付。

```sh
.venv/bin/literature-radar weekly activate --config-dir "$LR_DATA/config/TOPIC_ID"
.venv/bin/literature-radar weekly run \
  --config-dir "$LR_DATA/config/TOPIC_ID" \
  --database "$LR_DATA/radar.sqlite3" \
  --output-dir "$LR_DATA/output/weekly/TOPIC_ID" --dry-run
```

去掉 `--dry-run` 执行真实 Weekly。`weekly run-scheduled` 会自动发现已启用且到期的 Topic；无须每个 Topic 一个 timer。默认纽约时区周一 09:00，可用 `--timezone`、`--weekday`、`--local-time` 设置自己的时间。Linux systemd 单元生成、启停和日志命令见[部署指南](deployment.md)。

HIGH 表示应优先阅读，MEDIUM 表示值得审查，LOW 表示有较弱但有效的科学关联。完全无关的论文不会为了凑数进入推荐，零篇推荐是正常结果。期刊影响因子、JCR 与期刊等级不参与推荐评分或通知。人工反馈按 Topic 独立累计，每 30 篇不同文献形成新训练批次；模型激活仍受现有保护规则约束。

Provider 超时可能出现 `PARTIAL_SUCCESS`；已成功的来源结果仍保留，下次根据 checkpoint 恢复。`NOT_DUE` 表示当前没有到期 Topic。任何 Provider 都不保证每周完全返回。参见[Provider 说明](api-providers.md)。

## 4. 数据、备份、升级和卸载

Topic、Focus、反馈和 Memory 在私有 config 目录；SQLite 在 `--database` 指定位置；报告在输出目录；卡片在所选 Vault。不要把这些文件放进公开仓库。

```sh
.venv/bin/literature-radar backup --database "$LR_DATA/radar.sqlite3" \
  --output "$LR_DATA/backups/radar-copy.sqlite3"
.venv/bin/literature-radar restore --backup "$LR_DATA/backups/radar-copy.sqlite3" \
  --output "$LR_DATA/backups/restore-check.sqlite3"
```

备份还需单独复制私有 config、反馈和 Vault；SQLite 备份并不包含它们。升级前保存并验证备份，再在新虚拟环境安装 wheel，验证配置和 CLI 后切换定时服务。卸载时只禁用自己的 timer、删除自己的虚拟环境；数据和 Vault 由用户决定是否保留。详情见[备份与恢复](backup-restore.md)。

Core 版本为 0.3.0，Bridge 独立版本为 0.2.3。源码和发行包采用 [MIT 许可证](../LICENSE)；[第三方说明](../THIRD_PARTY_NOTICES.md)列出了依赖和 Provider 内容边界。维护者的个人环境曾完成真实 Weekly；新用户全新安装、双 Topic Bootstrap、Weekly、反馈隔离、Obsidian 交付和 systemd 在隔离 mock 环境通过验收。尚未在全新用户环境验证五源真实联网运行；Linux/systemd 已验证，其他平台未验证。Provider 部分成功和 checkpoint 恢复属于支持范围；没有科学关联论文时可以推荐 0 篇。
