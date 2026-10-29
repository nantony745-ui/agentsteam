'use strict';

class TeamConversation {
  constructor(session, member, owner) {
    this.session = session; this.member = member; this.owner = owner;
    this.cursor = 0; this.revision = 0; this.closed = false; this.loading = true;
    this.buffer = []; this.runs = new Map(); this.permissions = new Map(); this.tools = new Map(); this.questions = new Map();
    this.events = Promise.resolve();
    this.slots = new Map(); this.replaying = 0;
    this.dirtyText = new Set(); this.textTimer = this.textFrame = null;
    this.before = null; this.pending = null;
    try {this.pending = JSON.parse(sessionStorage.getItem(this.pendingKey()));} catch {}
    this.root = this.node('section', 'team-pane');
    this.root.dataset.sessionId = session.id;
    const heading = this.node('header', 'team-pane-heading');
    heading.append(this.node('strong', '', `${member.profile.name}${member.profile.employee_id === owner.project.team.leader_id ? ' ♛' : ''}`));
    this.select = document.createElement('select'); this.select.setAttribute('aria-label', `${member.profile.name}的会话`);
    this.select.addEventListener('change', () => owner.openSession(owner.roster.sessions.find(item => item.id === this.select.value), this));
    const fresh = this.button('＋', () => owner.newMember(member.profile.employee_id, this)); fresh.title = '单开会话'; fresh.setAttribute('aria-label', '单开会话');
    this.fresh = fresh;
    const focus = this.button('⤢', () => owner.focus(this)); focus.title = '聚焦 / 恢复并排';
    const close = this.button('×', () => owner.closePane(this)); close.setAttribute('aria-label', '关闭对话窗');
    heading.append(this.select, fresh, focus, close);
    this.timeline = this.node('section', 'timeline'); this.timeline.setAttribute('aria-label', `${member.profile.name}的会话消息`);
    this.older = this.button('加载更早的对话', () => this.loadOlder()); this.older.hidden = true;
    this.timeline.append(this.older);
    try {this.savedScroll = JSON.parse(sessionStorage.getItem(`agentsteam.team.scroll.${session.id}`));} catch {}
    this.timeline.addEventListener('scroll', () => {
      if (this.loading) return;
      try {sessionStorage.setItem(`agentsteam.team.scroll.${session.id}`,JSON.stringify({top:this.timeline.scrollTop,bottom:this.timeline.scrollHeight-this.timeline.scrollTop-this.timeline.clientHeight<100}));} catch {}
    });
    this.warning = this.node('p', 'form-error');
    const form = this.node('form', 'message-form'), composer = this.node('div', 'composer');
    this.input = document.createElement('textarea'); this.input.rows = 2; this.input.placeholder = `发送给 ${member.profile.name}…`;
    this.input.setAttribute('aria-label', `发送给 ${member.profile.name}`); this.input.disabled = true;
    try {this.input.value = sessionStorage.getItem(this.draftKey()) || '';} catch {}
    this.input.addEventListener('input', () => this.saveDraft());
    this.input.addEventListener('keydown', event => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) {event.preventDefault(); this.send();}
    });
    const toolbar = this.node('div', 'composer-toolbar');
    this.model = this.node('span', 'composer-agent', agentLabel(session.harness));
    this.mode = collaborationSelector(session.id, session.harness, null);
    this.sendButton = this.button('↑', () => this.send()); this.sendButton.className = 'send-button'; this.sendButton.setAttribute('aria-label', '发送消息');
    this.stopButton = this.button('■', () => this.stop()); this.stopButton.title = '停止本轮'; this.stopButton.hidden = true;
    toolbar.append(this.model, this.mode, this.stopButton, this.sendButton); composer.append(this.input, toolbar);
    this.error = this.node('p', 'form-error'); this.error.setAttribute('role', 'alert');
    this.retry = this.button('重试原消息', () => this.send()); this.retry.hidden = true;
    this.abandon = this.button('放弃本地重试', () => {this.clearPending();}); this.abandon.hidden = true;
    if (this.pending) {this.input.value = this.pending.message; this.input.readOnly = true; this.retry.hidden = this.abandon.hidden = false;}
    form.append(this.warning, composer, this.error, this.retry, this.abandon);
    form.addEventListener('submit', event => {event.preventDefault(); this.send();});
    this.status = this.node('div', 'team-pane-stream', '加载中…'); this.status.setAttribute('role', 'status');
    this.root.append(heading, this.timeline, form, this.status);
    this.root.addEventListener('pointerdown', () => owner.activate(this));
    this.root.addEventListener('focusin', () => owner.activate(this));
    this.timer = null; this.visible = true; this.waitingRuns = new Set();
    this.visibilityChanged = () => this.syncTimer();
    document.addEventListener('visibilitychange', this.visibilityChanged);
    this.ready = this.load().catch(error => this.failure(error));
  }
  node(tag, className, text) { const value = document.createElement(tag); value.className = className; if (text != null) value.textContent = text; return value; }
  button(text, callback) {const button = this.node('button', '', text); button.type = 'button'; button.addEventListener('click', () => Promise.resolve().then(callback).catch(error => this.failure(error))); return button;}
  draftKey() {return `agentsteam.team.draft.${this.session.id}`;}
  pendingKey() {return `agentsteam.team.pending.${this.session.id}`;}
  saveDraft() {try {sessionStorage.setItem(this.draftKey(), this.input.value);} catch {}}
  clearPending(accepted = false) {
    if (accepted) this.input.value = '';
    this.pending = null; this.input.readOnly = false; this.retry.hidden = this.abandon.hidden = true;
    try {sessionStorage.removeItem(this.pendingKey());} catch {}
    this.saveDraft();
  }
  failure(error) {if (!this.closed) {this.error.textContent = error.message; this.status.textContent = '加载或操作失败';}}
  async load() {
    const version = ++this.revision;
    const [detail, page] = await Promise.all([api(`/api/sessions/${this.session.id}`), api(`/api/sessions/${this.session.id}/history`)]);
    if (this.closed || version !== this.revision) return;
    this.session = {...this.session, ...detail.session};
    const records = [...new Map([...page.runs,...page.active_runs].map(run => [run.id,run])).values()];
    for (const run of records) this.ensureRun(run.id, run);
    if (this.pending && records.some(run => run.request_id === this.pending.request_id)) this.clearPending(true);
    await this.readEvents(records, page.high_watermark, true);
    if (this.closed || version !== this.revision) return;
    for (const row of page.metadata) await this.apply(row);
    this.cursor = page.high_watermark; this.before = page.before; this.older.hidden = !page.before;
    for (const interaction of detail.interactions) if (interaction.kind === 'permission') this.permission(interaction);
    for (const interaction of detail.interactions) if (interaction.kind === 'question') this.question(interaction);
    const buffered = this.buffer.splice(0).sort((a,b) => a.id-b.id);
    this.loading = false;
    await Promise.all(buffered.map(row => this.receive(row)));
    if (this.closed) return;
    this.flushText(); this.trimHistory(); this.input.disabled = false; this.status.textContent = '已连接'; this.update();
    this.timeline.scrollTop = this.savedScroll && !this.savedScroll.bottom ? this.savedScroll.top : this.timeline.scrollHeight;
  }
  async readEvents(runs, high, includeContext) {
    let cursor = 0;
    this.replaying += 1;
    try {
    while (!this.closed) {
      const query = new URLSearchParams({project_id:this.session.project_id, session_id:this.session.id,
        after_event_id:String(cursor), through_event_id:String(high), limit:'100', include_context:String(includeContext)});
      for (const run of runs) query.append('run_id', run.id);
      if (!runs.length) query.append('run_id', '');
      const rows = await api(`/api/events?${query}`);
      if (this.closed) return;
      if (!rows.length) break;
      for (const row of rows) await this.apply(await hydrateEvent(row));
      cursor = rows.at(-1).id;
    }
    if (!this.closed) for (const record of runs) {
      const run = this.runs.get(record.id);
      if (run && TERMINAL_STATUSES.has(run.record.status)) this.finish(run);
    }
    } finally {
      this.replaying -= 1;
      this.trimHistory(new Set(runs.map(run => run.id)));
    }
  }
  async loadOlder() {
    if (!this.before || this.older.disabled) return;
    this.older.disabled = true;
    try {
      const height = this.timeline.scrollHeight;
      const page = await api(`/api/sessions/${this.session.id}/history?before=${this.before}`);
      if (this.closed) return;
      for (const run of page.runs) this.ensureRun(run.id, run);
      await this.readEvents(page.runs, page.high_watermark, false);
      if (this.closed) return;
      this.flushText();
      this.before = page.before; this.older.hidden = !page.before;
      this.timeline.scrollTop += this.timeline.scrollHeight - height;
    } finally {this.older.disabled = false;}
  }
  ensureRun(id, record = {}) {
    if (this.runs.has(id)) {const run = this.runs.get(id); Object.assign(run.record, record); return run;}
    const root = this.node('article', 'run'); root.dataset.runId = id;
    const content = this.node('div', 'run-content'), user = this.node('div', 'user-message'); user.hidden = true;
    const work = this.node('details', 'work-log'); work.open = true;
    const summary = this.node('summary', 'work-summary'), items = this.node('div', 'work-items'); work.append(summary, items);
    summary.addEventListener('click', () => {work.dataset.manual = 'true';});
    const alerts = this.node('div', 'run-alerts'), files = this.node('div', 'team-pane-files');
    content.append(user, work, alerts, files); root.append(content);
    const run = {root, user, work, summary, items, alerts, files, record:{id, ...record}, seen:new Set(), text:null, segments:[]};
    this.runs.set(id, run);
    this.slots.get(id)?.root.remove(); this.slots.delete(id);
    const sorted = [...this.runs.values(), ...this.slots.values()].sort((a,b) => (a.record.created_at || '').localeCompare(b.record.created_at || ''));
    for (const item of sorted) this.timeline.append(item.root);
    return run;
  }
  trimHistory(keep = new Set()) {
    if (this.closed || this.loading || this.replaying) return;
    const candidates = [...this.runs.values()].filter(run => TERMINAL_STATUSES.has(run.record.status));
    const viewport = this.timeline.getBoundingClientRect?.();
    if (viewport) candidates.sort((left, right) => {
      const distance = run => {const bounds = run.root.getBoundingClientRect(); return Math.abs((bounds.top + bounds.bottom - viewport.top - viewport.bottom) / 2);};
      return distance(right) - distance(left);
    });
    while (candidates.length > HISTORY_RUN_LIMIT) {
      const index = candidates.findIndex(run => !keep.has(run.record.id));
      if (index < 0) break;
      this.evictRun(candidates.splice(index, 1)[0]);
    }
  }
  evictRun(run) {
    const id = run.record.id;
    const root = this.button('重新加载这一轮历史', () => this.restoreRun(id)); root.className = 'history-placeholder';
    root.style.minHeight = `${run.root.getBoundingClientRect?.().height || 160}px`;
    this.timeline.insertBefore(root, run.root);
    this.slots.set(id, {root, record:{created_at:run.record.created_at}});
    for (const block of run.segments) this.dirtyText.delete(block);
    for (const [key, tool] of this.tools) if (tool.runId === id) this.tools.delete(key);
    for (const records of [this.permissions, this.questions]) for (const [key, item] of records) if (item.run_id === id) records.delete(key);
    run.root.remove(); this.runs.delete(id);
  }
  async restoreRun(id) {
    const slot = this.slots.get(id);
    if (!slot || slot.root.disabled || this.closed) return;
    slot.root.disabled = true;
    try {
      const record = await api(`/api/runs/${id}?summary=true`);
      if (this.closed || this.slots.get(id) !== slot) return;
      this.ensureRun(id, record);
      await this.readEvents([record], this.cursor, false);
      if (!this.closed) {this.flushText(); this.trimHistory(new Set([id])); this.update();}
    } catch (error) {
      if (!this.closed) {
        if (this.runs.has(id)) this.evictRun(this.runs.get(id));
        const failed = this.slots.get(id);
        if (failed) {failed.root.disabled = false; failed.root.textContent = `加载失败，点击重试：${error.message}`;}
      }
      throw error;
    }
  }
  receive(row) {
    const next = this.events.then(async () => {
      if (this.blockedAt && row.id > this.blockedAt) throw new Error('正在等待补拉前面的事件');
      try {await this.process(row); if (this.blockedAt === row.id) this.blockedAt = null;}
      catch (error) {this.blockedAt = row.id; throw error;}
    });
    this.events = next.catch(() => {});
    return next;
  }
  async process(row) {
    if (this.closed || row.id <= this.cursor) return;
    if (this.loading) {
      if (this.buffer.length >= LIVE_EVENT_LIMIT) throw new Error('历史加载期间事件积压，等待补拉');
      this.buffer.push(row); return;
    }
    const follow = this.timeline.scrollHeight - this.timeline.scrollTop - this.timeline.clientHeight < 100;
    if (row.run_id && !this.runs.has(row.run_id)) {
      const record = await api(`/api/runs/${row.run_id}?summary=true`);
      if (this.closed) return;
      if (record.session_id !== this.session.id) {this.cursor = row.id; return;}
      this.ensureRun(row.run_id, record);
      await this.readEvents([record], row.id - 1, false);
      if (this.pending?.request_id === record.request_id) this.clearPending(true);
    }
    await this.apply(await hydrateEvent(row));
    if (this.closed) return;
    this.cursor = row.id; if (follow && row.kind !== 'agent.text') this.timeline.scrollTop = this.timeline.scrollHeight;
  }
  async apply(row) {
    if (this.closed || row.session_id !== this.session.id) return;
    if (row.kind === 'session.model') {this.model.textContent = `${agentLabel(this.session.harness)} ${row.payload.model_id || row.payload.model || ''}`; return;}
    if (row.kind === 'session.config') {this.warning.textContent = (row.payload.warnings || []).map(item => item.reason).join('；'); return;}
    if (!row.run_id || !['user.message','agent.text','agent.tool','agent.plan','error','run.status','permission.requested','permission.answered','permission.expired','question.requested','question.answered','question.expired'].includes(row.kind)) return;
    const run = this.ensureRun(row.run_id, {created_at:this.runs.get(row.run_id)?.record.created_at || row.created_at});
    if (run.seen.has(row.id)) return;
    run.seen.add(row.id);
    const payload = row.payload;
    if (row.kind !== 'agent.text') run.text = null;
    if (row.kind === 'user.message') {run.user.textContent = payload.message; run.user.hidden = false;}
    else if (row.kind === 'agent.text') {
      if (!run.text || run.text.channel !== payload.channel) {
        let root, body;
        if (payload.channel === 'thought') {root = this.node('details', 'thought-block'); root.append(this.node('summary', '', '思考过程')); body = this.node('div', 'agent-text'); root.append(body);}
        else {root = body = this.node('div', 'agent-text');}
        run.items.append(root); run.text = {root, body, channel:payload.channel, text:''}; run.segments.push(run.text);
      }
      run.text.text += payload.text; this.dirtyText.add(run.text);
      if (!this.loading && !this.replaying) scheduleTextFlush(this, () => this.flushText());
    } else if (row.kind === 'agent.tool') {
      const key = `${row.run_id}:${payload.toolCallId}`;
      let tool = this.tools.get(key);
      if (!tool) {
        const root = this.node('details', 'tool-card'); root.dataset.toolKey = key;
        tool = {root, data:{}, runId:row.run_id}; this.tools.set(key, tool); run.items.append(root);
        root.addEventListener('toggle', () => {if (root.open) this.toolDetails(tool).catch(error => this.failure(error));});
      }
      const previousStatus = tool.data.status;
      Object.assign(tool.data, payload);
      const header = createToolHeader(tool.data); tool.root.querySelector('summary')?.remove(); tool.root.prepend(header);
      if (tool.loaded && (previousStatus !== tool.data.status || (!tool.remoteDetails && JSON.stringify(tool.data).includes('"$detail"')))) {
        tool.refreshDetails = true;
        if (!tool.loadingDetails) this.refreshToolDetails(tool);
      } else if (tool.loaded && !tool.remoteDetails) tool.root.querySelector('pre').textContent = JSON.stringify(tool.data, null, 2).slice(0,16000);
    } else if (row.kind === 'agent.plan') {
      if (payload.plan || payload.sessionUpdate === 'plan_removed') {
        renderCodexPlan(row, run.alerts, () => {
          if (this.closed || this.activeRun || this.pending || this.sending) return;
          this.pending = {message:'请按以下计划执行：\n\n' + payload.plan.content, request_id:crypto.randomUUID(), collaboration_mode:'default'};
          try {sessionStorage.setItem(this.pendingKey(),JSON.stringify(this.pending));} catch {}
          this.mode.value = 'default'; this.send();
          try {sessionStorage.setItem(`agentsteam.mode.${this.session.id}`, 'default');} catch {}
        });
      } else {
      let plan = run.items.querySelector('.plan-card'); if (!plan) {plan = this.node('div', 'plan-card'); run.items.append(plan);}
      plan.replaceChildren(...(payload.entries || []).map(item => this.node('p', '', `${item.status === 'completed' ? '✓' : '○'} ${item.content}`)));
      }
    } else if (row.kind === 'error') run.alerts.append(this.node('p', 'run-error', payload.message || payload.error || '执行错误'));
    else if (row.kind.startsWith('question.')) this.question({id:payload.question_id, run_id:row.run_id, status:payload.status, payload});
    else if (row.kind.startsWith('permission.')) this.permission({id:payload.permission_id, run_id:row.run_id, status:payload.status, payload});
    else if (row.kind === 'run.status') {
      Object.assign(run.record, payload);
      if (TERMINAL_STATUSES.has(payload.status)) {
        run.record.finished_at = row.created_at;
        this.finish(run); this.fileChanges(run).catch(error => {if (!this.closed) run.files.textContent = error.message;});
        if (payload.error) run.alerts.append(this.node('p', 'run-error', payload.error));
        this.trimHistory(new Set([row.run_id]));
      }
    }
    if (!['agent.text','agent.tool'].includes(row.kind)) this.update();
  }
  flushText() {
    cancelTextFlush(this);
    if (this.closed) return;
    const follow = this.timeline.scrollHeight - this.timeline.scrollTop - this.timeline.clientHeight < 100;
    for (const block of this.dirtyText) renderMarkdown(block.body, block.text);
    this.dirtyText.clear();
    if (follow) this.timeline.scrollTop = this.timeline.scrollHeight;
  }
  finish(run) {
    this.flushText();
    const last = run.items.lastElementChild;
    if (!run.root.querySelector('.team-final-answer') && last?.classList.contains('agent-text')) {last.classList.add('team-final-answer'); run.root.querySelector('.run-content').insertBefore(last, run.alerts);}
    if (!run.work.dataset.manual) run.work.open = run.record.status !== 'completed' || !run.root.querySelector('.team-final-answer');
    for (const block of run.segments) delete block.text;
    run.text = null;
  }
  async toolDetails(tool) {
    if (tool.loaded) return;
    tool.loaded = true; const pre = this.node('pre', '', JSON.stringify(tool.data, null, 2).slice(0,16000)); tool.root.append(pre);
    tool.remoteDetails = JSON.stringify(tool.data).includes('"$detail"');
    if (tool.remoteDetails) {
      let cursor = null, high = null;
      const next = this.button('读取完整详情', async () => {
        if (tool.loadingDetails) return;
        tool.loadingDetails = true; next.disabled = true;
        try {
        const query = new URLSearchParams(); if (cursor) query.set('cursor', cursor); if (high) query.set('high_watermark', high);
        const page = await api(`/api/runs/${tool.runId}/tools/${encodeURIComponent(tool.data.toolCallId)}/details?${query}`);
        if (this.closed) return;
        pre.textContent = page.records.map(item => item.parts.map(part => part.text).join('') || JSON.stringify(item.payload,null,2)).join('\n');
        cursor = page.next; high = page.high_watermark; next.disabled = !cursor; next.textContent = cursor ? '下一页' : '已读完';
        } catch (error) {next.disabled = false; throw error;}
        finally {tool.loadingDetails = false; if (tool.refreshDetails && !this.closed) this.refreshToolDetails(tool);}
      }); tool.root.append(next); next.click();
    }
  }
  refreshToolDetails(tool) {
    tool.refreshDetails = false; tool.loaded = false;
    tool.root.querySelectorAll('pre,button').forEach(node => node.remove());
    if (tool.root.open) this.toolDetails(tool).catch(error => this.failure(error));
  }
  permission(record) {
    if (!record.id) return;
    const previous = this.permissions.get(record.id);
    if (previous && previous.status !== 'pending' && record.status === 'pending') return;
    const payload = typeof record.payload === 'string' ? JSON.parse(record.payload) : record.payload;
    const merged = {...previous, ...record, payload:{...previous?.payload, ...payload}};
    const root = previous?.root || this.node('section', 'tool-card permission-card'); merged.root = root;
    root.replaceChildren(this.node('strong', '', `权限：${merged.payload.tool_call?.title || '工具操作'}`));
    if (merged.status === 'pending' && !merged.uncertain) for (const option of merged.payload.options || []) {
      const button = this.button(option.name, async () => {
        for (const item of root.querySelectorAll('button')) item.disabled = true;
        try {
          const result = await api(`/api/interactions/${record.id}/answer`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({option_id:option.optionId})});
          if (!this.closed) this.permission(result);
        } catch (error) {
          try {
            const detail = await api(`/api/sessions/${this.session.id}?summary=true`);
            if (!this.closed) {
              const latest = detail.interactions.find(item => item.id === record.id);
              this.permission(latest || {...merged, status:'resolved'});
            }
          } catch {
            if (!this.closed) this.permission({...this.permissions.get(record.id), uncertain:true});
          }
          throw error;
        }
      }); root.append(button);
    } else root.append(this.node('span', '', merged.uncertain ? '状态待核对，请刷新后处理' : merged.status === 'answered' ? '已回答' : '已结束'));
    this.permissions.set(record.id, merged); this.ensureRun(record.run_id).alerts.append(root);
  }
  question(record) {
    renderQuestionCard(record, this.questions, this.ensureRun(record.run_id).alerts, this.session.id, () => !this.closed);
  }
  async fileChanges(run) {
    if (run.loadingFiles) return; run.loadingFiles = true;
    try {
      const data = await api(`/api/runs/${run.record.id}/file-changes`); if (this.closed || this.runs.get(run.record.id) !== run) return;
      run.files.replaceChildren();
      if (run.record.task_id) run.files.append(this.button('任务变更', async () => {
        const diff = await api(`/api/tasks/${run.record.task_id}/diff`);
        if (!this.closed) this.owner.file('任务变更', `基线：${diff.base_sha || ''}\n${diff.status || ''}\n${diff.patch || '没有已跟踪变化'}\n未跟踪：${(diff.untracked || []).join('、')}`);
      }));
      for (const file of data.files) {
        const row = this.node('div', 'team-pane-file'); row.append(this.node('span', '', file.path), fileChangeCounts(file.additions,file.deletions));
        for (const [mode,label,enabled] of [['preview','预览',file.previewable],['diff','差异',file.has_diff]]) if (enabled) row.append(this.button(label, async () => {
          const result = await api(`/api/runs/${run.record.id}/file-changes/${mode}?path=${encodeURIComponent(file.path)}`);
          if (!this.closed) this.owner.file(file.path, result.diff ?? result.content ?? result.reason);
        }));
        run.files.append(row);
      }
    } finally {run.loadingFiles = false;}
  }
  duration(run) {
    const start = Date.parse(run.record.created_at), end = Date.parse(run.record.finished_at) || Date.now();
    const summary = `Worked for ${Number.isFinite(start) ? formatWorkDuration(Math.max(0,end-start)) : '…'} · ${this.waitingRuns.has(run.record.id) ? '等待回答' : STATUS_LABELS[run.record.status] || '加载中'}`;
    if (run.summary.textContent !== summary) run.summary.textContent = summary;
  }
  syncTimer() {
    const running = !this.closed && this.visible && !document.hidden && this.activeRun;
    if (!running && this.timer !== null) {clearInterval(this.timer); this.timer = null;}
    if (running && this.timer === null) {
      this.duration(this.activeRun);
      this.timer = setInterval(() => this.duration(this.activeRun), 1000);
    }
  }
  setVisible(visible) {
    if (this.visible === visible) return;
    this.visible = visible; this.root.hidden = !visible;
    if (visible) this.update();
    else this.syncTimer();
  }
  update() {
    if (this.closed) return;
    this.fresh.disabled = this.member.state === 'deleted';
    this.fresh.title = this.fresh.disabled ? '队员已被删除' : '单开会话';
    this.waitingRuns = new Set([...this.questions.values()].filter(item => item.status === 'pending').map(item => item.run_id));
    let active = null;
    for (const run of this.runs.values()) {
      this.duration(run);
      if (['running','waiting_permission','cancel_requested'].includes(run.record.status)) active = run;
      else if (!active && run.record.status === 'queued') active = run;
    }
    this.activeRun = active; this.stopButton.hidden = !active;
    this.syncTimer();
    for (const run of this.runs.values()) updatePlanActions(run.root, run.record.status === 'completed' && !active && !this.loading && !this.sending && !this.pending);
    this.mode.disabled = Boolean(this.pending || this.sending);
    this.sendButton.disabled = this.loading || Boolean(this.sending);
    const sessions = this.owner.roster.sessions.filter(item => item.employee_id === this.member.profile.employee_id);
    const signature = JSON.stringify(sessions.map(item => [item.id,item.title]));
    if (signature !== this.sessionOptions) {
      this.select.replaceChildren(...sessions.map(item => new Option(item.title || `会话 ${item.id.slice(0,6)}`,item.id)));
      this.select.value = this.session.id; this.sessionOptions = signature;
    }
  }
  async send() {
    if (this.closed || this.loading || this.sending) return;
    if (!this.pending) {
      if (!this.input.value.trim()) return;
      this.pending = {message:this.input.value, request_id:crypto.randomUUID()};
      if (this.session.harness === 'codex' && this.mode.value) this.pending.collaboration_mode = this.mode.value;
      try {sessionStorage.setItem(this.pendingKey(),JSON.stringify(this.pending));} catch {}
    }
    const submitted = this.pending;
    this.sending = true; this.input.readOnly = true; this.error.textContent = ''; this.update();
    try {
      const result = await api(`/api/sessions/${this.session.id}/messages`, {method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify(submitted)});
      if (this.closed) {
        try {
          const stored = JSON.parse(sessionStorage.getItem(this.pendingKey()));
          if (stored?.request_id === submitted.request_id) {sessionStorage.removeItem(this.pendingKey()); sessionStorage.removeItem(this.draftKey());}
        } catch {}
        return;
      }
      this.ensureRun(result.run_id, {status:result.status, created_at:new Date().toISOString()});
      if (this.pending?.request_id === submitted.request_id) this.clearPending(true);
    } catch (error) {this.failure(error); this.retry.hidden = this.abandon.hidden = false;}
    finally {this.sending = false; this.update();}
  }
  async stop() {if (this.activeRun) await api(`/api/runs/${this.activeRun.record.id}/cancel`, {method:'POST'});}
  destroy() {
    this.saveDraft(); this.closed = true; this.revision++; clearInterval(this.timer); cancelTextFlush(this);
    this.timer = null; document.removeEventListener('visibilitychange', this.visibilityChanged);
    for (const records of [this.runs, this.slots, this.tools, this.permissions, this.questions, this.dirtyText]) records.clear();
    this.buffer.length = 0; this.root.remove();
  }
}
