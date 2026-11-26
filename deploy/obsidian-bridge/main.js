const { Plugin, PluginSettingTab, Setting, TFile } = require('obsidian');
const fs = require('node:fs');
const path = require('node:path');

const LABELS = { must_read: '很重要', keep: '有用', later: '一般', discard: '没用' };
const ALLOWED = new Set(Object.keys(LABELS));
const OUTBOX = '.obsidian/plugins/literature-radar-bridge/outbox.json';
const SYNC_STATUS = '.obsidian/plugins/literature-radar-bridge/sync-status.json';
const REVIEW_PATH = /^(Bootstrap|Weekly)\/([a-z0-9]+(?:-[a-z0-9]+)*)\/(BOOTSTRAP_SEED|HIGH|MEDIUM|LOW)\/[^/]+\.md$/;
const DATED_WEEKLY_PATH = /^Weekly\/(\d{4}-\d{2}-\d{2})\/([a-z0-9]+(?:-[a-z0-9]+)*)\/(HIGH|MEDIUM|LOW)\/[^/]+\.md$/;
const LIVE_BEGIN = '<!-- LITERATURE-RADAR-LIVE:BEGIN -->';
const LIVE_END = '<!-- LITERATURE-RADAR-LIVE:END -->';
const SUMMARY_BEGIN = '<!-- LITERATURE-RADAR-SUMMARY:BEGIN -->';
const SUMMARY_END = '<!-- LITERATURE-RADAR-SUMMARY:END -->';

function parseFrontmatter(text) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---/.exec(text);
  const values = {};
  if (!match) return values;
  for (const line of match[1].split(/\r?\n/)) {
    const part = /^([a-z_][a-z0-9_]*):\s*(.*)$/.exec(line);
    if (!part) continue;
    try { values[part[1]] = JSON.parse(part[2]); }
    catch { values[part[1]] = part[2].replace(/^['"]|['"]$/g, ''); }
  }
  return values;
}

function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch { return fallback; }
}

function topicCounts() {
  return { total: 0, pending: 0, reviewed: 0, invalid: 0, must_read: 0, keep: 0,
    later: 0, discard: 0, pending_sync: 0, synced: 0, failed: 0,
    seed_count: 0, duplicate_count: 0,
    imported_feedback: 0, eligible_feedback: 0, completed_batches: 0,
    pending_batches: 0, processed_feedback: 0, unprocessed_feedback: 0,
    next_batch_threshold: 30, last_calibration_time: null };
}

function reviewPath(filePath) {
  const match = REVIEW_PATH.exec(filePath);
  if (match) return match[1] === 'Bootstrap' || match[3] !== 'BOOTSTRAP_SEED' ? match : null;
  const dated = DATED_WEEKLY_PATH.exec(filePath);
  if (!dated) return null;
  const date = new Date(dated[1] + 'T00:00:00Z');
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === dated[1]
    ? [filePath, 'Weekly', dated[2], dated[3], dated[1]] : null;
}

function addCardStatus(counts, meta) {
  counts.total++;
  if (ALLOWED.has(meta.feedback_decision)) {
    counts.reviewed++; counts[meta.feedback_decision]++;
    if (meta.feedback_sync_status === 'synced') counts.synced++;
    else if (meta.feedback_sync_status === 'failed') counts.failed++;
    else counts.pending_sync++;
  } else if (meta.feedback_decision || ['reviewed', 'invalid'].includes(meta.review_status)) counts.invalid++;
  else counts.pending++;
}

function addOfficialStatus(counts, topic, configRoot) {
  if (!configRoot) return;
  const base = path.join(configRoot, topic);
  const feedback = readJson(path.join(base, 'feedback.json'), { entries: [] });
  const state = readJson(path.join(base, 'attention-batch-state.json'), { batches: [] });
  const latest = new Map();
  for (const entry of feedback.entries || []) {
    if (entry.topic_id === topic && typeof entry.paper_key === 'string' && ALLOWED.has(entry.decision) &&
        !['automatic', 'bootstrap', 'discovery', 'weekly', 'system'].includes(entry.extensions?.source))
      latest.set(entry.paper_key, entry);
  }
  const batches = state.topic_id && state.topic_id !== topic ? [] : (state.batches || []);
  const completed = batches.filter(batch =>
    batch.status === 'COMPLETED' && batch.training_version && batch.completed_at);
  counts.imported_feedback = latest.size;
  counts.eligible_feedback = latest.size;
  counts.completed_batches = completed.length;
  counts.processed_feedback = completed.reduce((count, batch) => count + (batch.feedback_count || 30), 0);
  counts.unprocessed_feedback = Math.max(0, latest.size - counts.processed_feedback);
  counts.pending_batches = Math.floor(counts.unprocessed_feedback / 30);
  counts.next_batch_threshold = counts.processed_feedback + 30;
  counts.last_calibration_time = completed.at(-1)?.completed_at || null;
}

function renderLive(topics) {
  const lines = [LIVE_BEGIN, ''];
  let total = 0;
  for (const [topic, item] of [...topics.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const c = item.counts;
    total += c.total;
    const name = String(item.name).replace(/[\[\]|]/g, ' ');
    lines.push(`- [[Bootstrap/${topic}/index|${name}]]：总文献 ${c.total}（初次调研种子 ${c.seed_count}；旧版卡片 ${c.total - c.seed_count}）；待评价 ${c.pending}；已评价 ${c.reviewed}。`);
    lines.push(`  - 很重要 ${c.must_read}；有用 ${c.keep}；一般 ${c.later}；没用 ${c.discard}；无效反馈 ${c.invalid}。`);
    lines.push(`  - 待同步 ${c.pending_sync}；同步失败 ${c.failed}；正式导入反馈 ${c.imported_feedback}。`);
    lines.push(`  - Attention：卡片评价进度 ${c.reviewed}/${c.total}；有效正式反馈 ${c.eligible_feedback}（累计）；已完成 ${c.completed_batches} 批；待处理 ${c.pending_batches} 批；已处理反馈 ${c.processed_feedback}；待处理反馈 ${c.unprocessed_feedback}；下一批训练阈值 ${c.next_batch_threshold} 条。`);
  }
  lines.push(`- **总计：${total} 篇**`, '', LIVE_END);
  return lines.join('\n');
}

function renderSummary(topics) {
  const total = topicCounts();
  for (const item of topics.values()) {
    for (const key of ['pending', 'reviewed', 'invalid', 'must_read', 'keep', 'later', 'discard'])
      total[key] += item.counts[key];
  }
  const duplicates = [...topics.values()].reduce((count, item) => count + item.counts.duplicate_count, 0);
  return [SUMMARY_BEGIN, '', `- 待阅读：${total.pending} 篇`,
    `- 已阅读 / 已反馈：${total.reviewed} 篇`, `- 反馈异常：${total.invalid} 篇`,
    `- 重复卡片路径：${duplicates} 篇`,
    `- 必读：${total.must_read} 篇`, `- 保留：${total.keep} 篇`,
    `- 稍后：${total.later} 篇`, `- 不相关：${total.discard} 篇`, '',
    '反馈入库后才计入正式导入和 Attention 进度。', '', SUMMARY_END].join('\n');
}

function replaceLive(text, block, summaryBlock) {
  const begin = text.indexOf(LIVE_BEGIN), end = text.indexOf(LIVE_END);
  let updated;
  if (begin >= 0 || end >= 0) {
    if (begin < 0 || end < begin || text.indexOf(LIVE_BEGIN, begin + 1) >= 0 ||
        text.indexOf(LIVE_END, end + 1) >= 0) throw new Error('Dashboard live markers conflict');
    updated = text.slice(0, begin) + block + text.slice(end + LIVE_END.length);
  } else {
    const heading = '## Bootstrap 初次调研';
    const start = text.indexOf(heading), next = text.indexOf('## 阅读状态视图', start + heading.length);
    if (start < 0 || next < 0) throw new Error('Dashboard Bootstrap section is missing');
    updated = text.slice(0, start + heading.length) + '\n\n' + block + '\n\n' + text.slice(next);
  }
  const summaryStart = updated.indexOf(SUMMARY_BEGIN), summaryEnd = updated.indexOf(SUMMARY_END);
  if (summaryStart >= 0 || summaryEnd >= 0) {
    if (summaryStart < 0 || summaryEnd < summaryStart) throw new Error('Dashboard summary markers conflict');
    updated = updated.slice(0, summaryStart) + summaryBlock + updated.slice(summaryEnd + SUMMARY_END.length);
  } else {
    const heading = updated.indexOf('## 阅读进度');
    const deliveryEnd = updated.indexOf('<!-- LITERATURE-RADAR-DELIVERY:END -->', heading);
    if (heading >= 0 && deliveryEnd > heading)
      updated = updated.slice(0, heading) + '## 阅读进度\n\n' + summaryBlock + '\n\n' + updated.slice(deliveryEnd);
  }
  return updated;
}

module.exports = class LiteratureRadarBridge extends Plugin {
  async onload() {
    this.queue = Promise.resolve();
    const settings = await this.loadData() || {};
    this.configRoot = settings.configRoot || process.env.LITERATURE_RADAR_CONFIG_ROOT || '';
    this.addSettingTab(new class extends PluginSettingTab {
      constructor(app, plugin) { super(app, plugin); this.plugin = plugin; }
      display() {
        this.containerEl.empty();
        new Setting(this.containerEl).setName('Literature Radar config folder')
          .setDesc('Local folder containing the Topic configuration and feedback files.')
          .addText(input => input.setPlaceholder('Choose a local config folder')
            .setValue(this.plugin.configRoot).onChange(async value => {
              this.plugin.configRoot = value.trim();
              await this.plugin.saveData({ configRoot: this.plugin.configRoot });
              this.plugin.scheduleDashboard();
            }));
      }
    }(this.app, this));
    this.busyFiles = new Set();
    this.registerMarkdownCodeBlockProcessor('literature-radar-feedback', (_source, element, context) => {
      const file = this.app.vault.getAbstractFileByPath(context.sourcePath);
      if (file instanceof TFile && reviewPath(file.path)) this.renderControls(element, file);
    });
    this.registerEvent(this.app.metadataCache.on('changed', file => this.enqueue(file)));
    this.registerEvent(this.app.vault.on('modify', file => {
      if (file.path !== 'dashboard.md') this.enqueue(file);
    }));
    this.app.workspace.onLayoutReady(() => {
      this.scheduleDashboard();
      for (const file of this.app.vault.getMarkdownFiles()) this.enqueue(file);
    });
    this.registerInterval(setInterval(() => this.scheduleDashboard(), 30000));
    try { this.watcher = this.configRoot ? fs.watch(this.configRoot, { recursive: true }, () => this.scheduleDashboard()) : null; }
    catch { this.watcher = null; }
    this.register(() => this.watcher?.close());
    try {
      const pluginDir = path.join(this.app.vault.adapter.basePath, '.obsidian/plugins/literature-radar-bridge');
      this.outboxWatcher = fs.watch(pluginDir, () => this.scheduleDashboard());
    } catch { this.outboxWatcher = null; }
    this.register(() => this.outboxWatcher?.close());
  }

  renderControls(element, file) {
    let optimisticDecision = null;
    const redraw = (override) => {
      const decision = override || optimisticDecision || this.app.metadataCache.getFileCache(file)?.frontmatter?.feedback_decision;
      element.replaceChildren();
      const row = element.createDiv({ cls: 'lr-feedback-controls' });
      for (const [value, label] of Object.entries(LABELS)) {
        const button = row.createEl('button', {
          text: (decision === value ? '✓ ' : '') + label,
          cls: `lr-feedback-button lr-feedback-${value}${decision === value ? ' is-selected' : ''}`
        });
        button.type = 'button';
        button.setAttribute('aria-pressed', String(decision === value));
        button.addEventListener('click', async () => {
          if (button.disabled || this.busyFiles.has(file.path)) return;
          this.busyFiles.add(file.path);
          button.disabled = true;
          optimisticDecision = value;
          redraw(value);
          try {
            await this.app.fileManager.processFrontMatter(file, metadata => {
              if (metadata.feedback_decision === value && metadata.review_status === 'reviewed') return;
              metadata.feedback_decision = value;
              metadata.review_status = 'reviewed';
              metadata.feedback_sync_status = 'pending';
            });
            optimisticDecision = null;
            redraw(value);
            this.enqueue(file);
            this.scheduleDashboard();
          } catch (error) {
            optimisticDecision = null;
            redraw();
            console.error('Literature Radar feedback write failed', error);
          }
          finally { button.disabled = false; this.busyFiles.delete(file.path); }
        });
      }
      row.createDiv({ text: '当前评价：' + (LABELS[decision] || '未评价'), cls: 'lr-feedback-current' });
    };
    redraw();
    const ref = this.app.metadataCache.on('changed', changed => {
      if (changed.path === file.path) redraw();
    });
    this.register(() => ref.off());
  }

  enqueue(file) {
    if (!(file instanceof TFile) || !reviewPath(file.path)) return;
    this.queue = this.queue.then(() => this.inspect(file))
      .catch(error => console.error('Literature Radar feedback queue failed', error));
    this.scheduleDashboard();
  }

  scheduleDashboard() {
    if (this.dashboardTimer) clearTimeout(this.dashboardTimer);
    this.dashboardTimer = setTimeout(() => {
      this.dashboardTimer = null;
      this.queue = this.queue.then(() => this.refreshDashboard())
        .catch(error => console.error('Literature Radar Dashboard refresh failed', error));
    }, 250);
  }

  async refreshDashboard() {
    const dashboard = this.app.vault.getAbstractFileByPath('dashboard.md');
    if (!(dashboard instanceof TFile)) return;
    const topics = new Map();
    const allTopics = new Map();
    const currentCards = new Map();
    const seen = new Set();
    for (const file of [...this.app.vault.getMarkdownFiles()].sort((a, b) => {
      const oldFirst = Number(Boolean(DATED_WEEKLY_PATH.exec(a.path))) - Number(Boolean(DATED_WEEKLY_PATH.exec(b.path)));
      return oldFirst || a.path.localeCompare(b.path);
    })) {
      const match = reviewPath(file.path);
      if (!match) continue;
      const topic = match[2], meta = parseFrontmatter(await this.app.vault.read(file));
      if (meta.type !== 'literature-review-card' || meta.topic_id !== topic ||
          typeof meta.paper_key !== 'string' || !meta.paper_key.startsWith(topic + '|')) continue;
      const allItem = allTopics.get(topic) || { name: meta.topic_display_name || topic, counts: topicCounts() };
      allTopics.set(topic, allItem);
      const identity = match[1] + '|' + meta.paper_key;
      if (seen.has(identity)) {
        allItem.counts.duplicate_count++;
        continue;
      }
      seen.add(identity);
      addCardStatus(allItem.counts, meta);
      if (match[1] !== 'Bootstrap') continue;
      const item = topics.get(topic) || { name: meta.topic_display_name || topic, counts: topicCounts() };
      topics.set(topic, item);
      const c = item.counts;
      addCardStatus(c, meta);
      if (match[3] === 'BOOTSTRAP_SEED') c.seed_count++;
      if (ALLOWED.has(meta.feedback_decision)) {
        currentCards.set(topic + '|' + meta.paper_key, { counts: c, decision: meta.feedback_decision,
          sync_status: meta.feedback_sync_status });
      }
    }
    try {
      const outbox = await this.readOutbox();
      const status = await this.app.vault.adapter.exists(SYNC_STATUS) ?
        JSON.parse(await this.app.vault.adapter.read(SYNC_STATUS)) : {};
      const failures = new Set(status.failed_event_ids || []);
      const counted = new Set();
      for (const event of outbox.events) {
        if (!failures.has(event.id)) continue;
        const key = event.topic_id + '|' + event.paper_key;
        if (counted.has(key)) continue;
        const card = currentCards.get(key);
        if (card && card.decision === event.decision && card.sync_status !== 'synced' && card.sync_status !== 'failed') {
          card.counts.pending_sync--; card.counts.failed++; counted.add(key);
        }
      }
    } catch (error) { console.error('Literature Radar sync status read failed', error); }
    for (const [topic, item] of topics) addOfficialStatus(item.counts, topic, this.configRoot);
    const old = await this.app.vault.read(dashboard);
    const updated = replaceLive(old, renderLive(topics), renderSummary(allTopics));
    if (updated !== old) await this.app.vault.modify(dashboard, updated);
  }

  async readOutbox() {
    if (!(await this.app.vault.adapter.exists(OUTBOX))) return { version: 1, events: [] };
    const value = JSON.parse(await this.app.vault.adapter.read(OUTBOX));
    if (value.version !== 1 || !Array.isArray(value.events)) throw new Error('Invalid Bridge Outbox');
    return value;
  }

  async inspect(file) {
    const match = reviewPath(file.path);
    if (!match) return;
    const front = this.app.metadataCache.getFileCache(file)?.frontmatter;
    if (!front || front.type !== 'literature-review-card') return;
    const topic = match[2];
    if (front.topic_id !== topic || front.topic !== topic ||
        typeof front.paper_key !== 'string' || !front.paper_key.startsWith(topic + '|') ||
        typeof front.paper_id !== 'string' || typeof front.doi !== 'string') return;
    const decision = front.feedback_decision;
    if (front.review_status !== 'reviewed') return;
    if (!ALLOWED.has(decision)) {
      await this.app.fileManager.processFrontMatter(file, metadata => {
        if (metadata.review_status === 'reviewed' && !ALLOWED.has(metadata.feedback_decision)) {
          metadata.review_status = 'invalid'; metadata.feedback_sync_status = 'invalid';
        }
      });
      return;
    }
    const outbox = await this.readOutbox();
    const last = [...outbox.events].reverse().find(item =>
      item.topic_id === topic && item.paper_key === front.paper_key);
    if (!last && front.feedback_sync_status === 'synced' &&
        front.feedback_synced_decision === decision) return;
    if (last?.decision === decision) {
      if (front.feedback_sync_status === 'synced' &&
          front.feedback_synced_decision === decision) return;
      if (front.feedback_updated_at === last.recorded_at &&
          front.feedback_sync_status === 'pending') return;
      await this.app.fileManager.processFrontMatter(file, metadata => {
        if (metadata.feedback_decision === decision && metadata.review_status === 'reviewed') {
          metadata.feedback_updated_at = last.recorded_at;
          metadata.feedback_sync_status = 'pending';
        }
      });
      return;
    }
    const recordedAt = new Date().toISOString();
    outbox.events.push({
      id: crypto.randomUUID(), topic_id: topic, paper_key: front.paper_key,
      paper_id: front.paper_id, doi: front.doi, decision,
      recorded_at: recordedAt, path: file.path
    });
    await this.app.vault.adapter.write(OUTBOX, JSON.stringify(outbox, null, 2) + '\n');
    await this.app.fileManager.processFrontMatter(file, metadata => {
      if (metadata.feedback_decision === decision && metadata.review_status === 'reviewed') {
        metadata.feedback_updated_at = recordedAt;
        metadata.feedback_sync_status = 'pending';
      }
    });
  }
};

module.exports._test = { parseFrontmatter, reviewPath, topicCounts, addOfficialStatus, renderLive, renderSummary, replaceLive };
