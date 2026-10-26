'use strict';

/* 2.6 三栏单 Agent 工作台：原生 JS，fetch 操作 HTTP，EventSource 订阅 lab 事件。
   历史与实时共用 applyEvent；数据库 id 升序为事实来源，DOM 应用成功才推进游标。 */

const $ = (selector) => document.querySelector(selector);

const STATUS_LABELS = {
  queued: '排队中',
  running: '运行中',
  waiting_permission: '等待权限',
  cancel_requested: '取消请求中',
  completed: '已完成',
  failed: '失败',
  canceled: '已取消',
  interrupted: '已中断',
};
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'canceled', 'interrupted']);
const CANCELABLE_STATUSES = new Set(['queued', 'running', 'waiting_permission']);
const HISTORY_PAGE_SIZE = 100;
const RECONNECT_DELAY_MS = 3000;
const SELECTION_KEY = 'agentsteam.selection';
const MODEL_CACHE_KEY = 'agentsteam.models.v1';
const SIDEBAR_KEY = 'agentsteam.sidebar.v1';
const SIDEBAR_PAGE_SIZE = 5;
const TOOL_KIND_LABELS = { execute: '执行命令', read: '读取文件', edit: '编辑文件', delete: '删除文件', move: '移动文件', search: '搜索', fetch: '获取内容', think: '思考' };
const AGENT_ICONS = {
  codex: { file: 'codex.svg' },
  opencode: { file: 'opencode.svg', mono: true },
  codebuddy: { file: 'codebuddy.svg', mono: true },
  claude_code: { file: 'claude-code.svg' },
  pi: { file: 'pi.svg' },
  grok: { file: 'grok.svg', mono: true },
  deepseek: { file: 'deepseek.svg' },
  cursor: { file: 'cursor.svg', mono: true },
  antigravity: { file: 'antigravity.svg' },
  tiny_agents: { file: 'tiny-agents.svg' },
};

const state = {
  generation: 0,
  projectId: null,
  context: null, // {projectId, sessionId, generation, lastEventId}
  projects: [],
  sessions: [],
  draft: null,
  collapsed: new Set(),
  sidebarLimits: new Map(),
  sidebarTimer: null,
  sidebarPoll: null,
  sidebarVersion: 0,
  sidebarRevision: 0,
  projectCreating: false,
  source: null,
  reconnectTimer: null,
  runs: new Map(), // run_id -> <article class="run">
  tools: new Map(), // `${sessionId}:${toolCallId}` -> <article class="tool-card">
  toolData: new Map(), // 同上 key -> 合并后的工具数据对象
  runRecords: new Map(), // run_id -> 权威 Run 记录
  runItems: new Map(), // run_id -> 右栏节点
  runFetchVersions: new Map(), // run_id -> 请求版本，旧响应不覆盖新请求
  permissions: new Map(), // permission_id -> 交互事实与提交状态
  permissionCards: new Map(),
  pendingBySession: new Map(), // sessionId -> {projectId, sessionId, message, requestId}
  sendingBySession: new Map(),
  nativeId: null,
  harness: null,
  modelId: null,
  employeeName: null,
  textBlocks: new Map(),
  dirtyText: new Set(),
  textFrame: null,
  textTimer: null,
  runSlots: new Map(),
  historyObserver: null,
  followingBottom: true,
  scrollFrame: null,
  cancelingRuns: new Set(),
  workTimer: null,
  sessionMenuId: null,
  sessionMenuKey: null,
  deleteTarget: null,
  deleting: false,
  fileView: null,
};

let markdownRenderer;

function renderMarkdown(element, text) {
  if (!markdownRenderer) {
    markdownRenderer = globalThis.markdownit({ html: false, linkify: false, typographer: false });
    markdownRenderer.disable('image');
    markdownRenderer.validateLink = url => /^(https?:|mailto:)/i.test(url);
    markdownRenderer.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
      tokens[index].attrSet('target', '_blank');
      tokens[index].attrSet('rel', 'noopener noreferrer');
      return renderer.renderToken(tokens, index, options);
    };
    markdownRenderer.renderer.rules.table_open = () => '<div class="table-scroll"><table>\n';
    markdownRenderer.renderer.rules.table_close = () => '</table></div>\n';
  }
  element.innerHTML = markdownRenderer.render(text);
}

function cancelTextFlush(target) {
  if (target.textTimer !== null) clearTimeout(target.textTimer);
  if (target.textFrame !== null) cancelAnimationFrame(target.textFrame);
  target.textTimer = target.textFrame = null;
}

function scheduleTextFlush(target, flush) {
  if (target.textTimer !== null || target.textFrame !== null) return;
  target.textTimer = setTimeout(() => {
    target.textTimer = null;
    target.textFrame = requestAnimationFrame(flush);
  }, 100);
}

function flushTextUpdates() {
  cancelTextFlush(state);
  for (const block of state.dirtyText) {
    const data = state.textBlocks.get(block);
    if (!data || data.text == null) continue;
    data.hasText = Boolean(data.text.trim());
    renderMarkdown(data.body, data.text);
    if (data.closed) delete data.text;
  }
  state.dirtyText.clear();
  followTimeline();
}

function followTimeline() {
  if (state.scrollFrame !== null) return;
  state.scrollFrame = requestAnimationFrame(() => {
    state.scrollFrame = null;
    const timeline = $('#timeline');
    if (state.followingBottom) timeline.scrollTop = timeline.scrollHeight;
    $('#scroll-bottom').hidden = state.followingBottom;
  });
}

function currentExecution() {
  const records = [...state.runRecords.values()];
  return records.find(run => ['running', 'waiting_permission', 'cancel_requested'].includes(run.status))
    || records.find(run => run.status === 'queued') || records.at(-1);
}

function updateExecutionStatus() {
  const run = currentExecution();
  const waiting = [...state.permissions.values()].filter(item => item.status === 'pending');
  const status = $('#execution-status');
  status.textContent = waiting.length ? `待授权 ${waiting.length}` : run ? STATUS_LABELS[run.status] || run.status : '尚无执行';
  status.dataset.status = waiting.length ? 'waiting_permission' : run?.status || '';
  const stop = $('#stop-button');
  stop.hidden = !run || (!CANCELABLE_STATUSES.has(run.status) && run.status !== 'cancel_requested');
  stop.disabled = !run || run.status === 'cancel_requested' || state.cancelingRuns.has(run.id);
  stop.textContent = run && (run.status === 'cancel_requested' || state.cancelingRuns.has(run.id)) ? '取消中' : '停止本轮';
  updateSendButton();
}

function renderProjectContext() {
  const project = state.projects.find(item => item.id === state.projectId);
  $('#project-title').textContent = project?.name || 'Agentsteam';
  $('#composer-project').textContent = project?.name || '未选择项目';
  $('#session-cwd').textContent = project?.path || '';
  $('#session-cwd').title = project?.path || '';
  renderProjectGit();
}

function renderProjectGit() {
  if (typeof ProjectGitUI !== 'undefined') ProjectGitUI.setContext({
    projectId: state.projectId, sessionId: state.context?.sessionId || null, path: $('#session-cwd').textContent,
  });
}

/* ---------- 通用 ---------- */

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  }
  return data;
}

function clearText(...elements) {
  for (const element of elements) element.textContent = '';
}

function setStreamStatus(text, className) {
  $('#stream-status').textContent = text;
  $('#stream-status').className = `stream-status${className ? ` ${className}` : ''}`;
}

/* ---------- 项目 ---------- */

function renderProjectOptions() {
  const select = $('#project-select');
  const previous = state.projectId;
  select.replaceChildren();
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '尚未选择项目';
  select.append(placeholder);
  for (const project of state.projects) {
    const option = document.createElement('option');
    option.value = project.id;
    option.textContent = project.name;
    option.title = project.path;
    select.append(option);
  }
  select.value = previous && state.projects.some((item) => item.id === previous) ? previous : '';
}

async function loadProjects() {
  const generation = state.generation;
  const projects = await api('/api/projects'); // 先局部接收
  if (generation !== state.generation) return; // 核对代次后才赋全局
  state.projects = projects;
  renderProjectOptions();
  renderProjectContext();
  renderSessionList();
}

function selectProject(projectId) {
  state.generation += 1;
  state.projectId = projectId || null;
  $('#project-select').value = state.projectId || ''; // 程序选中时同步下拉控件显示
  $('#create-session-button').disabled = false;
  resetConversation();
  state.draft = { projectId: state.projectId, harness: $('#harness').value || 'codex', creating: false };
  $('#session-title').textContent = '新会话';
  renderComposerAgent();
  renderProjectContext();
  renderSessionList();
  saveSelection();
  updateMessageForm();
  if (typeof TeamWorkspace !== 'undefined') TeamWorkspace.openProject(state.projects.find(item => item.id === state.projectId));
}

async function loadSessions() {
  const version = ++state.sidebarVersion;
  const revision = state.sidebarRevision;
  const sessions = await api('/api/sessions');
  if (version !== state.sidebarVersion || revision !== state.sidebarRevision) return;
  const present = new Set(sessions.map(session => session.id));
  const removed = state.sessions.filter(session => !present.has(session.id)).map(session => session.id);
  if (removed.length) applyDeletedSessions(removed);
  state.sessions = sessions;
  renderSessionList();
  const selected = sessions.find(session => session.id === state.context?.sessionId);
  if (selected) $('#session-title').textContent = selected.title;
}

function scheduleSidebarRefresh() {
  state.sidebarRevision += 1;
  if (state.sidebarTimer !== null) return;
  state.sidebarTimer = setTimeout(() => {
    state.sidebarTimer = null;
    loadSessions().catch(() => {}); // 保留现有摘要，下次轮询重试。
  }, 500);
}

function startSidebarPolling() {
  if (state.sidebarPoll !== null) clearTimeout(state.sidebarPoll);
  state.sidebarPoll = setTimeout(async () => {
    state.sidebarPoll = null;
    if (!document.hidden) {
      try { await loadSessions(); } catch { /* 下次轮询恢复，不清空现有列表。 */ }
    }
    startSidebarPolling();
  }, 5000);
}

function loadSidebarPreferences() {
  try { state.collapsed = new Set(JSON.parse(localStorage.getItem(SIDEBAR_KEY) || '[]')); }
  catch { state.collapsed = new Set(); }
}

function saveSidebarPreferences() {
  try { localStorage.setItem(SIDEBAR_KEY, JSON.stringify([...state.collapsed])); }
  catch { /* 布局存储不可用时仍可折叠。 */ }
}

function sidebarIcon(name) {
  const icon = document.createElement('span');
  icon.className = `sidebar-icon sidebar-icon-${name}`;
  icon.setAttribute('aria-hidden', 'true');
  return icon;
}

function sidebarAction(key, label, icon, handler) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'sidebar-action';
  button.dataset.sidebarKey = key;
  button.title = label;
  button.setAttribute('aria-label', label);
  button.append(sidebarIcon(icon));
  button.addEventListener('click', handler);
  return button;
}

function sidebarToggle(key, label, target) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'tree-toggle';
  button.dataset.sidebarKey = key;
  const open = !state.collapsed.has(key);
  button.append(sidebarIcon('chevron'));
  button.setAttribute('aria-expanded', String(open));
  button.setAttribute('aria-controls', target);
  button.setAttribute('aria-label', `${open ? '收起' : '展开'}${label}`);
  button.addEventListener('click', () => {
    if (state.collapsed.has(key)) state.collapsed.delete(key);
    else state.collapsed.add(key);
    saveSidebarPreferences();
    renderSessionList();
  });
  return button;
}

function renderSidebarSection(key, label, headingId, contentId) {
  const button = sidebarToggle(key, label, contentId);
  button.classList.add('sidebar-section-toggle');
  const text = document.createElement('span');
  text.textContent = label;
  button.append(text);
  $(`#${headingId}`).replaceChildren(button);
  if (key === 'section:projects') {
    const action = sidebarAction('add-project', '新建项目', 'new', openProjectDialog);
    action.setAttribute('aria-haspopup', 'dialog');
    action.setAttribute('aria-controls', 'project-dialog');
    $(`#${headingId}`).append(action);
  }
  $(`#${contentId}`).hidden = state.collapsed.has(key);
}

function sessionTree() {
  const nodes = new Map(state.sessions.map(session => [session.id, { session, children: [], activity: session }]));
  const roots = [];
  for (const node of nodes.values()) {
    const parent = nodes.get(node.session.parent_id);
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  const compare = (a, b) => Date.parse(b.updated_at || b.created_at) - Date.parse(a.updated_at || a.created_at)
    || (b.activity_event_id || 0) - (a.activity_event_id || 0) || a.id.localeCompare(b.id);
  const settle = node => {
    for (const child of node.children) {
      settle(child);
      if (compare(child.activity, node.activity) < 0) node.activity = child.activity;
    }
    node.children.sort((a, b) => compare(a.activity, b.activity));
    node.selectedBranch = node.session.id === state.context?.sessionId || node.children.some(child => child.selectedBranch);
  };
  roots.forEach(settle);
  return roots.sort((a, b) => compare(a.activity, b.activity));
}

async function openSidebarSession(session) {
  if (typeof TeamWorkspace !== 'undefined' && state.projects.find(item => item.id === session.project_id)?.team && session.employee_id) {
    if (state.projectId !== session.project_id) selectProject(session.project_id);
    await TeamWorkspace.openSession(session);
    return;
  }
  state.collapsed.delete('section:projects');
  state.collapsed.delete(`project:${session.project_id}`);
  const sessions = new Map(state.sessions.map(item => [item.id, item]));
  let ancestor = sessions.get(session.parent_id);
  while (ancestor) {
    state.collapsed.delete(`children:${ancestor.id}`);
    ancestor = sessions.get(ancestor.parent_id);
  }
  saveSidebarPreferences();
  state.projectId = session.project_id;
  $('#project-select').value = state.projectId;
  renderProjectContext();
  await selectSession(session.id);
}

function sessionButton(node, recent) {
  const { session, activity } = node;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'session-button';
  button.dataset.sessionId = session.id;
  button.dataset.sidebarKey = `${recent ? 'recent' : 'session'}:${session.id}`;
  if (state.context?.sessionId === session.id) button.classList.add('selected');
  button.setAttribute('aria-current', state.context?.sessionId === session.id ? 'page' : 'false');
  const definition = AGENT_ICONS[session.harness];
  const icon = document.createElement(definition.mono ? 'span' : 'img');
  icon.className = 'session-agent-icon';
  icon.setAttribute('aria-hidden', 'true');
  const iconUrl = `/icons/agents/${definition.file}`;
  if (definition.mono) {
    icon.classList.add('session-agent-icon-mono');
    icon.style.setProperty('--agent-icon', `url("${iconUrl}")`);
  } else {
    icon.src = iconUrl;
    icon.alt = '';
  }
  const main = document.createElement('span');
  main.className = 'session-button-title';
  main.textContent = session.title || '新会话';
  const meta = document.createElement('span');
  meta.className = 'session-button-meta';
  meta.textContent = formatSessionDate(activity.updated_at || activity.created_at);
  meta.title = new Date(activity.updated_at || activity.created_at).toLocaleString('zh-CN', { hour12: false });
  const project = state.projects.find(item => item.id === session.project_id);
  button.title = `${main.textContent} · ${agentLabel(session.harness)} · ${project?.path || ''}`;
  button.append(icon, main, meta);
  button.addEventListener('click', () => openSidebarSession(session));
  button.addEventListener('contextmenu', event => openSessionMenu(event, session, button));
  button.addEventListener('keydown', event => {
    if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) openSessionMenu(event, session, button);
  });
  return button;
}

function closeSessionMenu(restoreFocus = false) {
  const key = state.sessionMenuKey;
  $('#session-menu').hidden = true;
  state.sessionMenuId = null;
  if (restoreFocus) [...$('#sidebar').querySelectorAll('button')].find(button => button.dataset.sidebarKey === key)?.focus();
}

function openSessionMenu(event, session, button) {
  event.preventDefault();
  if (state.deleting) return;
  const menu = $('#session-menu');
  const keyboard = event.type === 'keydown' || !event.clientX && !event.clientY;
  const position = keyboard ? button.getBoundingClientRect() : { left: event.clientX, bottom: event.clientY };
  state.sessionMenuId = session.id;
  state.sessionMenuKey = button.dataset.sidebarKey;
  menu.style.left = `${Math.max(8, Math.min(position.left, window.innerWidth - 184))}px`;
  menu.style.top = `${Math.max(8, Math.min(position.bottom, window.innerHeight - 58))}px`;
  menu.hidden = false;
  $('#session-menu-delete').focus();
}

function openDeleteDialog() {
  const session = state.sessions.find(item => item.id === state.sessionMenuId);
  closeSessionMenu();
  if (!session) return;
  state.deleteTarget = session.id;
  $('#session-delete-description').textContent = `删除“${session.title || '新会话'}”及全部子会话？正在执行的任务会停止，本应用的消息、任务、反馈和历史记录将永久删除。项目文件与 Harness 原生记录保留。`;
  $('#session-delete-error').textContent = '';
  $('#session-delete-dialog').showModal();
  $('#session-delete-cancel').focus();
}

function applyDeletedSessions(sessionIds) {
  const removed = new Set(sessionIds);
  state.sidebarRevision += 1;
  state.sessions = state.sessions.filter(session => !removed.has(session.id));
  const cache = readModelCache();
  const events = new Set(sessionIds.map(id => cache[`session:${id}`]?.eventId).filter(id => id != null));
  for (const id of sessionIds) {
    state.pendingBySession.delete(id);
    state.sendingBySession.delete(id);
    state.collapsed.delete(`children:${id}`);
    state.sidebarLimits.delete(`children:${id}`);
    delete cache[`session:${id}`];
  }
  for (const key of Object.keys(cache)) if (key.startsWith('agent:') && events.has(cache[key].eventId)) delete cache[key];
  try { localStorage.setItem(MODEL_CACHE_KEY, JSON.stringify(cache)); } catch { /* 本地缓存不可用时仍完成删除。 */ }
  saveSidebarPreferences();
  if (removed.has(state.context?.sessionId)) resetConversation();
  if (removed.has(state.sessionMenuId)) closeSessionMenu();
  if (removed.has(state.deleteTarget)) $('#session-delete-dialog').close();
  saveSelection();
  renderSessionList();
}

async function confirmSessionDelete(event) {
  event.preventDefault();
  if (state.deleting || !state.deleteTarget) return;
  const id = state.deleteTarget;
  state.deleting = true;
  $('#session-delete-confirm').disabled = true;
  $('#session-delete-cancel').disabled = true;
  $('#session-delete-confirm').textContent = '删除中…';
  $('#session-delete-error').textContent = '';
  try {
    const result = await api(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    applyDeletedSessions(result.deleted_session_ids);
    $('#session-delete-dialog').close();
    scheduleSidebarRefresh();
  } catch (error) {
    $('#session-delete-error').textContent = `删除失败：${error.message}`;
  } finally {
    state.deleting = false;
    $('#session-delete-confirm').disabled = false;
    $('#session-delete-cancel').disabled = false;
    $('#session-delete-confirm').textContent = '删除会话';
  }
}

function bindSessionActions() {
  const menu = $('#session-menu');
  const dialog = $('#session-delete-dialog');
  $('#session-menu-delete').addEventListener('click', openDeleteDialog);
  document.addEventListener('pointerdown', event => { if (!menu.hidden && !menu.contains(event.target)) closeSessionMenu(); });
  document.addEventListener('keydown', event => {
    if (menu.hidden) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeSessionMenu(true); }
    if (event.key === 'Tab') closeSessionMenu();
  }, true);
  window.addEventListener('resize', () => closeSessionMenu());
  $('#sidebar').addEventListener('scroll', () => closeSessionMenu(), true);
  $('#session-delete-form').addEventListener('submit', confirmSessionDelete);
  $('#session-delete-cancel').addEventListener('click', () => dialog.close());
  dialog.addEventListener('keydown', event => event.stopPropagation());
  dialog.addEventListener('cancel', event => { if (state.deleting) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    state.deleteTarget = null;
    const key = state.sessionMenuKey;
    ([...$('#sidebar').querySelectorAll('button')].find(button => button.dataset.sidebarKey === key) || $('#create-session-button')).focus();
  });
}

function appendSessionPage(parent, nodes, key) {
  const limit = state.sidebarLimits.get(key) || SIDEBAR_PAGE_SIZE;
  const visible = nodes.filter((node, index) => index < limit || node.selectedBranch);
  for (const node of visible) appendSessionTree(parent, node);
  const remaining = nodes.length - visible.length;
  const canReset = limit > SIDEBAR_PAGE_SIZE && nodes.length > SIDEBAR_PAGE_SIZE;
  if (!remaining && !canReset) return;
  const controls = document.createElement('div');
  controls.className = 'session-pagination';
  const addControl = (action, label, nextLimit) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'session-page-button';
    button.dataset.sidebarKey = `${action}:${key}`;
    button.textContent = label;
    button.setAttribute('aria-controls', parent.id);
    button.addEventListener('click', () => {
      state.sidebarLimits.set(key, nextLimit);
      renderSessionList();
    });
    controls.append(button);
  };
  if (remaining) addControl('more', `Show more (${remaining})`, limit + SIDEBAR_PAGE_SIZE);
  if (canReset) addControl('less', 'Show less', SIDEBAR_PAGE_SIZE);
  parent.append(controls);
}

function appendSessionTree(parent, node) {
  const branch = document.createElement('div');
  branch.className = 'session-branch';
  const row = document.createElement('div');
  row.className = 'session-tree-row';
  const childId = `children-${node.session.id}`;
  if (node.children.length) row.append(sidebarToggle(`children:${node.session.id}`, node.session.title, childId));
  else {
    const marker = document.createElement('span');
    marker.className = 'tree-leaf';
    marker.setAttribute('aria-hidden', 'true');
    row.append(marker);
  }
  row.append(sessionButton(node, false));
  branch.append(row);
  if (node.children.length) {
    const children = document.createElement('div');
    children.id = childId;
    children.className = 'session-children';
    children.hidden = state.collapsed.has(`children:${node.session.id}`);
    appendSessionPage(children, node.children, `children:${node.session.id}`);
    branch.append(children);
  }
  parent.append(branch);
}

function sidebarHint(parent, text) {
  const hint = document.createElement('p');
  hint.className = 'empty-hint';
  hint.textContent = text;
  parent.append(hint);
}

function renderSessionList() {
  const projects = $('#project-list');
  const recent = $('#session-list');
  const focusKey = document.activeElement?.dataset.sidebarKey;
  const scroll = $('#sidebar').scrollTop;
  projects.replaceChildren();
  recent.replaceChildren();
  renderSidebarSection('section:projects', '项目', 'projects-heading', 'projects-content');
  renderSidebarSection('section:recent', '最近会话', 'recent-heading', 'session-list');
  const roots = sessionTree();
  for (const project of state.projects) {
    const group = document.createElement('section');
    group.className = 'project-group';
    const heading = document.createElement('div');
    heading.className = 'project-row';
    const childId = `project-sessions-${project.id}`;
    heading.append(sidebarToggle(`project:${project.id}`, project.name, childId));
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'project-button';
    button.dataset.sidebarKey = `folder:${project.id}`;
    const label = document.createElement('span');
    label.className = 'project-label';
    label.textContent = project.name;
    button.append(sidebarIcon('folder'), label);
    button.title = project.path;
    button.setAttribute('aria-current', state.projectId === project.id ? 'true' : 'false');
    button.addEventListener('click', () => selectProject(project.id));
    heading.append(button);
    heading.append(sidebarAction(`new-session:${project.id}`, `在 ${project.name} 中新建会话`, 'compose', event => createSession(event, project.id)));
    const children = document.createElement('div');
    children.id = childId;
    children.className = 'project-sessions';
    children.hidden = state.collapsed.has(`project:${project.id}`);
    const sessions = roots.filter(node => node.session.project_id === project.id);
    appendSessionPage(children, sessions, `project:${project.id}`);
    if (!sessions.length) sidebarHint(children, '尚无会话');
    group.append(heading, children);
    projects.append(group);
  }
  if (!state.projects.length) sidebarHint(projects, '添加项目目录后开始对话。');
  for (const root of roots.filter(node => !node.session.parent_id)) recent.append(sessionButton(root, true));
  if (!recent.children.length) sidebarHint(recent, '尚无会话。');
  if (focusKey) {
    const buttons = [...$('#sidebar').querySelectorAll('button')];
    const alternate = focusKey.startsWith('more:') ? focusKey.replace(/^more:/, 'less:') : focusKey.replace(/^less:/, 'more:');
    const target = buttons.find(button => button.dataset.sidebarKey === focusKey)
      || buttons.find(button => button.dataset.sidebarKey === alternate)
      || buttons.find(button => button.dataset.sidebarKey === focusKey.replace(/^(more|less):/, ''));
    target?.focus({ preventScroll: true });
  }
  $('#sidebar').scrollTop = scroll;
}

function agentLabel(harness) {
  return ({ codex: 'Codex', opencode: 'OpenCode', codebuddy: 'CodeBuddy', deepseek: 'DeepSeek' })[harness] || harness;
}

function formatSessionDate(value, now = Date.now()) {
  if (!value) return '';
  const minutes = Math.max(1, Math.floor((now - Date.parse(value)) / 60000));
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h`;
  return `${Math.floor(minutes / 1440)}d`;
}

/* ---------- 会话详情与诊断 ---------- */

function readModelCache() {
  try {
    const cache = JSON.parse(localStorage.getItem(MODEL_CACHE_KEY) || '{}');
    return cache && typeof cache === 'object' && !Array.isArray(cache) ? cache : {};
  } catch {
    return {};
  }
}

function validModelCache(entry, harness, projectId) {
  return entry?.harness === harness && entry.projectId === projectId
    && typeof entry.modelId === 'string' && entry.modelId.trim()
    && Number.isSafeInteger(entry.eventId);
}

function cachedSessionModel(sessionId, harness, projectId) {
  const cache = readModelCache();
  for (const key of [`session:${sessionId}`, `agent:${projectId}:${harness}`]) {
    if (validModelCache(cache[key], harness, projectId)) return cache[key].modelId;
  }
  return null;
}

function rememberSessionModel(row, context) {
  state.modelId = row.payload.model_id;
  const harness = state.harness;
  if (!harness) return;
  const cache = readModelCache();
  const entry = { modelId: state.modelId, harness, projectId: context.projectId, eventId: row.id };
  const sessionKey = `session:${context.sessionId}`;
  for (const key of [sessionKey, `agent:${context.projectId}:${harness}`]) {
    if (!validModelCache(cache[key], harness, context.projectId) || cache[key].eventId <= row.id) cache[key] = entry;
  }
  state.modelId = cache[sessionKey].modelId;
  try {
    localStorage.setItem(MODEL_CACHE_KEY, JSON.stringify(cache));
  } catch { /* 存储不可用时仍显示本次 ACP 确认的模型 */ }
}

function renderComposerAgent() {
  const label = state.harness ? agentLabel(state.harness) : '未选择 Agent';
  const element = $('#composer-agent');
  element.textContent = state.modelId ? `${label} ${state.modelId}` : label;
  if (state.employeeName && !state.draft) element.textContent = `${state.employeeName} · ${element.textContent}`;
  element.title = state.modelId ? element.textContent : `${label} · 模型待连接确认`;
  $('#agent-choice').hidden = !state.draft;
  element.hidden = Boolean(state.draft);
  $('#project-select').hidden = !state.draft;
  $('#composer-project').hidden = Boolean(state.draft);
  $('#harness').disabled = Boolean(state.draft?.creating);
  $('#project-select').disabled = Boolean(state.draft?.creating);
  if (typeof EmployeesUI !== 'undefined') EmployeesUI.renderChoice();
}

function renderSessionDetail(record) {
  $('#session-title').textContent = record.title || '新会话';
  state.harness = record.harness;
  state.employeeName = record.employee_name || null;
  state.modelId = cachedSessionModel(record.id, record.harness, record.project_id || state.context?.projectId);
  renderComposerAgent();
  $('#session-cwd').textContent = record.cwd || '';
  $('#session-cwd').title = record.cwd || '';
  renderProjectGit();
  $('#session-id').textContent = record.id;
  $('#session-harness').textContent = record.harness;
  if (record.employee_name) {
    $('#session-harness').textContent = `${record.employee_name} · ${agentLabel(record.harness)}`;
  }
  if (record.employee_warnings?.length) {
    $('#session-config-warning').hidden = false;
    $('#session-config-warning').textContent = record.employee_warnings.map(item => `${item.name}：${item.error}`).join('；');
  }
  state.nativeId = record.native_id || null;
  $('#native-id').textContent = record.native_id || '首轮连接后生成';
}

function resetTaskChanges() {
  $('#task-changes').hidden = true;
  $('#task-diff-content').hidden = true;
  for (const id of ['task-diff-meta', 'task-diff-message', 'task-git-status', 'task-diff-patch',
    'task-preview-message', 'task-file-preview']) $(`#${id}`).textContent = '';
  $('#task-untracked').replaceChildren();
}

function taskDiffMeta(task, head, cwd) {
  const status = { review: '待验收', accepted: '已接受' }[task.status] || STATUS_LABELS[task.status] || task.status;
  const directory = `任务：${task.id}\n状态：${status}\n目录：${cwd || task.worktree || '-'}`;
  if (!task.worktree) return directory;
  return `${directory}\n分支：${task.branch || 'detached HEAD'}\n基线：${task.base_sha || '-'}\nHEAD：${head || '-'}`;
}

async function refreshTaskChanges(context) {
  if (!context || state.context !== context || context.generation !== state.generation || !context.task) return;
  const version = context.diffVersion = (context.diffVersion || 0) + 1;
  const current = () => state.context === context && context.generation === state.generation && context.diffVersion === version;
  $('#task-changes').hidden = false;
  $('#task-diff-content').hidden = true;
  $('#task-diff-message').textContent = '正在读取任务变更…';
  $('#task-diff-patch').textContent = '';
  $('#task-git-status').textContent = '';
  $('#task-untracked').replaceChildren();
  $('#task-file-preview').textContent = '';
  $('#task-preview-message').textContent = '';
  try {
    const detail = await api(`/api/sessions/${encodeURIComponent(context.sessionId)}?summary=true`);
    if (!current()) return;
    context.task = detail.task;
    if (!detail.task) { resetTaskChanges(); return; }
    $('#task-diff-meta').textContent = taskDiffMeta(detail.task, null, detail.session.cwd);
    $('#session-cwd').textContent = detail.session.cwd || '';
    $('#session-cwd').title = detail.session.cwd || '';
    renderProjectGit();
    if (!detail.task.worktree) {
      $('#task-diff-message').textContent = detail.task.working_dir == null
        ? '与父会话共享执行目录，不提供任务独立 Diff。'
        : '使用指定执行目录，未绑定独立 worktree，不提供任务 Diff。';
      return;
    }
    $('#task-diff-content').hidden = false;
    const diff = await api(`/api/tasks/${encodeURIComponent(detail.task.id)}/diff`);
    if (!current()) return;
    $('#task-diff-meta').textContent = taskDiffMeta(detail.task, diff.head_sha, detail.session.cwd);
    $('#task-git-status').textContent = diff.status || '工作区干净';
    $('#task-diff-patch').textContent = diff.patch || '相对基线没有已跟踪变化';
    $('#task-diff-message').textContent = '已读取当前变更';
    for (const path of diff.untracked) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = path;
      button.addEventListener('click', () => loadTaskPreview(context, detail.task.id, path, version));
      $('#task-untracked').append(button);
    }
    if (!diff.untracked.length) $('#task-untracked').textContent = '没有未跟踪文件';
  } catch (error) {
    if (current()) $('#task-diff-message').textContent = `读取失败：${error.message}`;
  }
}

async function loadTaskPreview(context, taskId, path, diffVersion) {
  const version = context.previewVersion = (context.previewVersion || 0) + 1;
  const current = () => state.context === context && context.generation === state.generation
    && context.diffVersion === diffVersion && context.previewVersion === version;
  if (!current()) return;
  $('#task-preview-message').textContent = `正在读取：${path}`;
  $('#task-file-preview').textContent = '';
  try {
    const preview = await api(`/api/tasks/${encodeURIComponent(taskId)}/preview?${new URLSearchParams({path})}`);
    if (!current()) return;
    const notes = [path];
    if (preview.kind === 'binary') notes.push('二进制文件');
    if (preview.reason) notes.push(preview.reason);
    if (preview.truncated && preview.kind === 'binary') notes.push('超过 1 MiB，已截断');
    if (preview.kind === 'text' && preview.content === '' && !preview.truncated) notes.push('空文件');
    $('#task-preview-message').textContent = notes.join('\n');
    $('#task-file-preview').textContent = preview.content ?? '';
  } catch (error) {
    if (current()) $('#task-preview-message').textContent = `预览失败：${error.message}`;
  }
}

/* 首轮连接未完成时 native_id 可能为空；仅取得非空值才算完成。
   刷新状态（resolved/inflight）挂在捕获的 context 上，旧 A 的响应不能污染 A→B→A。
   触发点限于 run.status 与 agent.tool 事件，不随文本 token 请求。 */
async function refreshSessionNativeId(context) {
  if (state.context !== context || context.generation !== state.generation) return;
  if (context.nativeIdResolved) return;
  if (context.nativeIdInflight) return;
  context.nativeIdInflight = true;
  try {
    const detail = await api(`/api/sessions/${encodeURIComponent(context.sessionId)}?summary=true`);
    if (state.context !== context || context.generation !== state.generation) return;
    if (detail.session && detail.session.native_id) {
      context.nativeIdResolved = true;
      state.nativeId = detail.session.native_id;
      $('#native-id').textContent = detail.session.native_id;
    }
  } catch {
    // 失败时保持未完成，后续 run.status / agent.tool 事件仍有合并刷新机会
  } finally {
    context.nativeIdInflight = false;
  }
}

/* ---------- 时间线：Run 分组与事件渲染 ---------- */

function ensureRun(runId) {
  const existing = state.runs.get(runId);
  if (existing) return existing;
  const run = document.createElement('article');
  run.className = 'run';
  run.dataset.runId = runId;
  const header = document.createElement('div');
  header.className = 'run-header';
  const note = document.createElement('span');
  note.className = 'run-note';
  header.hidden = true;
  header.append(note);
  const content = document.createElement('div');
  content.className = 'run-content';
  const work = document.createElement('details');
  work.className = 'work-log';
  work.open = true;
  const summary = document.createElement('summary');
  summary.className = 'work-summary';
  summary.title = '展开或收起本轮全部中间过程';
  const label = document.createElement('span');
  label.className = 'work-label';
  const status = document.createElement('span');
  status.className = 'work-status';
  summary.append(label, status);
  summary.addEventListener('click', event => {
    event.preventDefault();
    work.open = !work.open;
    work.dataset.userToggled = 'true';
  });
  const items = document.createElement('div');
  items.className = 'work-items';
  work.append(summary, items);
  const answer = document.createElement('div');
  answer.className = 'run-answer';
  answer.hidden = true;
  const alerts = document.createElement('div');
  alerts.className = 'run-alerts';
  alerts.hidden = true;
  const files = document.createElement('section');
  files.className = 'run-file-changes';
  files.hidden = true;
  content.append(work, answer, files, alerts);
  run.append(content, header);
  const hint = $('#timeline .empty-hint');
  if (hint) hint.remove();
  $('#timeline').append(run);
  state.runs.set(runId, run);
  const record = state.runRecords.get(runId);
  if (record) {
    updateRunHeader(run, record);
  }
  updateWorkLog(run);
  return run;
}

function fileChangeCounts(additions, deletions) {
  const counts = document.createElement('span');
  counts.className = 'file-change-counts';
  for (const [name, prefix, count] of [['additions', '+', additions], ['deletions', '−', deletions]]) {
    const value = document.createElement('span');
    value.className = `file-${name}`;
    value.textContent = count == null ? '—' : `${prefix}${count}`;
    counts.append(value);
  }
  if (additions == null || deletions == null) counts.title = '工具未提供完整行数，统计不完整';
  return counts;
}

function renderRunFileChanges(run, data, context) {
  const host = run.querySelector('.run-file-changes');
  host.replaceChildren();
  host.hidden = !data.files.length;
  if (!data.files.length) return;
  const group = document.createElement('details');
  group.className = 'reply-files';
  group.open = true;
  const summary = document.createElement('summary');
  const title = document.createElement('span');
  title.textContent = '改动文件';
  const count = document.createElement('span');
  count.className = 'file-count';
  count.textContent = `${data.files.length} 个文件`;
  summary.append(title, count, fileChangeCounts(data.additions, data.deletions));
  if (data.additions == null || data.deletions == null) {
    const note = document.createElement('span');
    note.className = 'file-stats-note';
    note.textContent = '统计不完整';
    summary.append(note);
  }
  const grid = document.createElement('div');
  grid.className = 'reply-files-grid';
  for (const file of data.files) {
    const card = document.createElement('div');
    card.className = 'reply-file-card';
    card.dataset.path = file.path;
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'reply-file-open';
    open.title = file.previewable ? file.path : `${file.path}（已删除或位于会话目录外，无法预览当前内容）`;
    open.disabled = !file.previewable;
    open.setAttribute('aria-label', `预览当前文件：${file.path}`);
    const icon = document.createElement('span');
    icon.className = 'reply-file-icon';
    icon.textContent = '▤';
    icon.setAttribute('aria-hidden', 'true');
    const path = document.createElement('span');
    path.className = 'reply-file-path';
    const name = document.createElement('span');
    name.className = 'reply-file-name';
    name.textContent = file.path.split('/').at(-1);
    path.append(name);
    const directory = file.path.includes('/') ? file.path.slice(0, file.path.lastIndexOf('/')) : '';
    if (directory) {
      const dir = document.createElement('span');
      dir.className = 'reply-file-directory';
      dir.textContent = directory;
      path.append(dir);
    }
    if (file.operation === 'added' || file.operation === 'deleted') {
      const badge = document.createElement('span');
      badge.className = `file-operation file-operation-${file.operation}`;
      badge.textContent = file.operation === 'added' ? '新增' : '删除';
      path.append(badge);
    }
    open.append(icon, path, fileChangeCounts(file.additions, file.deletions));
    open.addEventListener('click', () => openFileView(run, file, 'preview', context));
    const diff = document.createElement('button');
    diff.type = 'button';
    diff.className = 'reply-file-diff';
    diff.textContent = '⇄';
    diff.title = file.has_diff ? '查看本轮差异' : '工具未提供差异内容';
    diff.setAttribute('aria-label', `查看差异：${file.path}`);
    diff.disabled = !file.has_diff;
    diff.addEventListener('click', () => openFileView(run, file, 'diff', context));
    card.append(open, diff);
    grid.append(card);
  }
  group.append(summary, grid);
  host.append(group);
}

function currentFileRun(run, context) {
  return context && state.context === context && context.generation === state.generation
    && state.runs.get(run.dataset.runId) === run;
}

async function refreshRunFileChanges(run, context) {
  if (!currentFileRun(run, context) || !TERMINAL_STATUSES.has(state.runRecords.get(run.dataset.runId)?.status)) return;
  if (run.fileChangesLoaded) return;
  if (run.fileChangesPending) return run.fileChangesPending;
  const controller = new AbortController();
  run.fileChangesAbort = controller;
  run.fileChangesPending = (async () => {
    try {
      const data = await api(`/api/runs/${encodeURIComponent(run.dataset.runId)}/file-changes`, {signal: controller.signal});
      if (controller.signal.aborted || !currentFileRun(run, context)) return;
      renderRunFileChanges(run, data, context);
      run.fileChangesLoaded = true;
      followTimeline();
    } catch (error) {
      if (controller.signal.aborted || !currentFileRun(run, context)) return;
      const host = run.querySelector('.run-file-changes');
      host.hidden = false;
      const message = document.createElement('span');
      message.textContent = `文件变更加载失败：${error.message}`;
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.textContent = '重试';
      retry.addEventListener('click', () => refreshRunFileChanges(run, context));
      host.replaceChildren(message, retry);
    } finally {
      run.fileChangesPending = null;
    }
  })();
  return run.fileChangesPending;
}

function closeFileView() {
  state.fileView?.controller.abort();
  state.fileView = null;
  const dialog = $('#file-view-dialog');
  if (dialog?.open) dialog.close();
  $('#file-view-content')?.replaceChildren();
}

async function openFileView(run, file, mode, context) {
  if (!currentFileRun(run, context)) return;
  state.fileView?.controller.abort();
  $('#file-view-content').replaceChildren();
  const dialog = $('#file-view-dialog');
  if (!dialog.fileEventsBound) {
    dialog.fileEventsBound = true;
    $('#file-view-close').addEventListener('click', closeFileView);
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeFileView(); });
    dialog.addEventListener('close', () => { if (!dialog.open) closeFileView(); });
    dialog.addEventListener('keydown', event => event.stopPropagation());
  }
  const view = {run, controller: new AbortController()};
  state.fileView = view;
  $('#file-view-title').textContent = `${mode === 'diff' ? '本轮差异' : '当前内容（只读）'} · ${file.path}`;
  const message = $('#file-view-message');
  const content = $('#file-view-content');
  message.textContent = '正在加载…';
  if (!dialog.open) dialog.showModal();
  try {
    const query = new URLSearchParams({path: file.path});
    const data = await api(`/api/runs/${encodeURIComponent(run.dataset.runId)}/file-changes/${mode}?${query}`, {signal: view.controller.signal});
    if (state.fileView !== view || !currentFileRun(run, context) || view.controller.signal.aborted) return;
    message.textContent = data.reason || (mode === 'preview' ? '文件当前内容，可能已不同于本轮结束时的版本。' : '按本轮工具操作顺序展示差异。');
    if (mode === 'diff') {
      if (data.diff == null) message.textContent = '工具未提供可展示的差异内容。';
      else for (const line of data.diff.split('\n')) {
        const row = document.createElement('span');
        row.className = line.startsWith('@@') ? 'diff-hunk' : line.startsWith('+') && !line.startsWith('+++ ') ? 'diff-added'
          : line.startsWith('-') && !line.startsWith('--- ') ? 'diff-deleted' : 'diff-context';
        row.textContent = line || ' ';
        content.append(row);
      }
    } else if (data.kind === 'text') content.textContent = data.content;
  } catch (error) {
    if (state.fileView !== view || !currentFileRun(run, context) || view.controller.signal.aborted) return;
    message.textContent = `加载失败：${error.message}`;
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.textContent = '重试';
    retry.addEventListener('click', () => openFileView(run, file, mode, context));
    content.append(retry);
  }
}

function formatWorkDuration(milliseconds) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return `${hours ? `${hours}小时 ` : ''}${hours || minutes ? `${minutes}分钟 ` : ''}${seconds % 60}秒`;
}

function updateWorkLog(run) {
  const record = state.runRecords.get(run.dataset.runId);
  const status = record?.status || run.workStatus || 'running';
  const terminal = TERMINAL_STATUSES.has(status);
  if (run.workStartedAt == null) run.workStartedAt = Date.parse(record?.created_at) || Date.now();
  if (terminal) run.workFinishedAt = Date.parse(record?.finished_at) || run.workFinishedAt || Date.now();
  const elapsed = (terminal ? run.workFinishedAt : Date.now()) - run.workStartedAt;
  run.querySelector('.work-label').textContent = `Worked for ${formatWorkDuration(elapsed)}`;
  const badge = run.querySelector('.work-status');
  badge.textContent = status === 'completed' ? '' : ({ submitting: '发送中', send_failed: '发送未确认', abandoned: '已放弃本地重试' })[status] || STATUS_LABELS[status] || status;
  badge.dataset.status = status;
  run.workActive = !terminal && !['send_failed', 'abandoned'].includes(status);
  if (run.workActive && state.workTimer === null) {
    state.workTimer = setTimeout(tickWorkLogs, 1000);
  } else if (state.workTimer !== null && ![...state.runs.values()].some(item => item.workActive)) {
    clearTimeout(state.workTimer);
    state.workTimer = null;
  }
}

function tickWorkLogs() {
  state.workTimer = null;
  for (const run of state.runs.values()) updateWorkLog(run);
}

function restoreRunAnswer(run) {
  const answer = run.querySelector('.run-answer');
  const items = run.querySelector('.work-items');
  for (const node of Array.from(answer.children)) items.append(node);
  answer.hidden = true;
}

function settleRunContent(run) {
  restoreRunAnswer(run);
  if (!TERMINAL_STATUSES.has(state.runRecords.get(run.dataset.runId)?.status)) return;
  const items = run.querySelector('.work-items');
  const answer = run.querySelector('.run-answer');
  const trailing = [];
  const nodes = Array.from(items.children);
  while (nodes.at(-1)?.classList.contains('agent-text') || nodes.at(-1)?.classList.contains('error-marker')) {
    const node = nodes.pop();
    if (node.classList.contains('agent-text')) trailing.unshift(node);
  }
  const visible = trailing.some(node => (state.textBlocks.get(node)?.hasText || state.textBlocks.get(node)?.text?.trim()));
  if (visible) for (const node of trailing) answer.append(node);
  answer.hidden = !visible;
  const work = run.querySelector('.work-log');
  if (!work.dataset.userToggled) work.open = !visible;
  work.dataset.empty = String(items.children.length === 0);
}

function showPendingWork(pending) {
  if (!pending.requestId) return;
  pending.startedAt ??= Date.now();
  const run = ensureRun(pending.runId || `pending:${pending.requestId}`);
  run.workStartedAt = pending.startedAt;
  run.workHasEventTime = true;
  run.workStatus = 'submitting';
  run.workRequest = pending;
  appendUserMessage({ payload: { message: pending.message } }, run);
  updateWorkLog(run);
  followTimeline();
}

function bindPendingRun(pending, runId) {
  const temporaryId = `pending:${pending.requestId}`;
  const temporary = state.runs.get(temporaryId);
  let run = state.runs.get(runId);
  if (temporary) {
    if (run) temporary.remove();
    else {
      run = temporary;
      run.dataset.runId = runId;
      state.runs.set(runId, run);
    }
    state.runs.delete(temporaryId);
  }
  pending.runId = runId;
  if (run) {
    if (temporary?.querySelector('.work-log').dataset.userToggled) {
      const work = run.querySelector('.work-log');
      work.dataset.userToggled = 'true';
      work.open = temporary.querySelector('.work-log').open;
    }
    run.workStartedAt = pending.startedAt;
    run.workHasEventTime = true;
    run.workStatus = 'queued';
    delete run.workRequest;
    updateWorkLog(run);
    settleRunContent(run);
  }
}

function updateRunHeader(runElement, record) {
  const note = runElement.querySelector('.run-header .run-note');
  note.className = 'run-note run-error';
  note.textContent = record.error ? `执行失败：${record.error}` : '';
  runElement.querySelector('.run-header').hidden = !record.error;
  if (TERMINAL_STATUSES.has(record.status)) {
    for (const summary of runElement.querySelectorAll('.thought-block > summary')) summary.textContent = '思考过程';
    if (!state.context?.replaying) flushTextUpdates();
  }
  updateWorkLog(runElement);
  settleRunContent(runElement);
  updateExecutionStatus();
}

function appendText(row, runElement) {
  const content = runElement.querySelector('.work-items');
  let block = content.lastElementChild;
  if (!block || !block.classList.contains('text-block') || block.dataset.channel !== row.payload.channel || state.textBlocks.get(block)?.closed) {
    if (block && state.textBlocks.has(block)) { state.textBlocks.get(block).closed = true; state.dirtyText.add(block); }
    if (block?.classList.contains('thought-block')) block.querySelector('summary').textContent = '思考过程';
    const thought = row.payload.channel === 'thought';
    block = document.createElement(thought ? 'details' : 'div');
    block.className = thought ? 'thought-block text-block' : 'agent-text text-block';
    block.dataset.channel = row.payload.channel;
    let body = block;
    if (thought) {
      const summary = document.createElement('summary');
      summary.textContent = TERMINAL_STATUSES.has(state.runRecords.get(row.run_id)?.status) ? '思考过程' : '思考中…';
      body = document.createElement('div');
      body.className = 'agent-text';
      block.append(summary, body);
    }
    content.append(block);
    state.textBlocks.set(block, { body, text: '' });
  }
  state.textBlocks.get(block).text += row.payload.text || '';
  state.dirtyText.add(block);
  if (!state.context?.replaying) scheduleTextFlush(state, flushTextUpdates);
}

function applyToolEvent(row, runElement) {
  const key = `${row.run_id}:${row.payload.toolCallId}`;
  let card = state.tools.get(key);
  if (!card) {
    card = document.createElement('details');
    card.className = 'tool-card';
    card.dataset.toolKey = key;
    card.dataset.runId = row.run_id;
    card.addEventListener('toggle', () => {
      if (card.open) renderToolCard(card, state.toolData.get(key));
      else { card.detailAbort?.abort(); card.detailVersion = (card.detailVersion || 0) + 1; card.replaceChildren(createToolHeader(state.toolData.get(key))); }
    });
    runElement.querySelector('.work-items').append(card);
    state.tools.set(key, card);
    state.toolData.set(key, {});
  }
  const data = state.toolData.get(key);
  for (const [field, value] of Object.entries(row.payload)) {
    if (field !== 'raw' && field !== 'sessionUpdate' && value != null) data[field] = value;
  }
  renderToolCard(card, data);
  groupToolCalls(runElement.querySelector('.work-items'));
}

function toolSummary(data) {
  const title = (typeof data.title === 'object' ? data.title?.preview || '' : data.title || '').replace(/\s+/g, ' ').trim();
  if (/^view[ _]image\b/i.test(title)) return '查看图片';
  if (data.kind === 'execute' || /^(?:@['"]|(?:node|python(?:3)?|powershell|pwsh|cmd|bash|git|uv|npm|npx)\b|[a-z]+-[a-z]+\b)/i.test(title)) return '执行命令';
  if (!title) return TOOL_KIND_LABELS[data.kind] || '工具调用';
  const characters = Array.from(title);
  return characters.length > 32 ? `${characters.slice(0, 31).join('')}…` : title;
}

function createToolHeader(data) {
  const header = document.createElement('summary');
  header.className = 'tool-card-header';
  const icon = document.createElement('span');
  icon.className = 'tool-icon';
  icon.setAttribute('aria-hidden', 'true');
  const title = document.createElement('h4');
  title.textContent = toolSummary(data);
  title.title = typeof data.title === 'string' ? data.title : data.title?.preview || title.textContent;
  const badge = document.createElement('span');
  badge.className = data.status === 'completed' ? 'tool-status sr-only' : 'tool-status';
  if (data.status) {
    badge.dataset.status = data.status;
    badge.textContent = ({ pending: '待执行', in_progress: '执行中', completed: '已完成', failed: '失败' })[data.status] || data.status;
  } else {
    badge.textContent = '未知状态';
  }
  header.append(icon, title, badge);
  return header;
}

function renderToolCard(card, data) {
  const remote = JSON.stringify(data).includes('"$detail"');
  if (card.open && card.querySelector('pre') && card.detailStatus === data.status && (!remote || card.detailRemote)) {
    card.querySelector('summary')?.remove(); card.prepend(createToolHeader(data));
    if (!card.detailRemote) card.querySelector('pre').textContent = JSON.stringify(data, null, 2).slice(0, 16000);
    return;
  }
  card.detailStatus = data.status;
  card.detailRemote = remote;
  card.detailAbort?.abort();
  card.detailAbort = typeof AbortController === 'undefined' ? null : new AbortController();
  const header = createToolHeader(data);
  card.replaceChildren(header);
  if (!card.open) return;
  const pre = document.createElement('pre');
  pre.textContent = JSON.stringify(data, null, 2).slice(0, 16000);
  card.append(pre);
  const context = state.context;
  const version = card.detailVersion = (card.detailVersion || 0) + 1;
  const next = document.createElement('button');
  next.textContent = '读取完整详情';
  card.append(next);
  let cursor = null;
  let high = null;
  const load = async () => {
    next.disabled = true;
    const query = new URLSearchParams();
    if (high !== null) query.set('high_watermark', String(high));
    if (cursor) query.set('cursor', cursor);
    try {
      const page = await api(`/api/runs/${encodeURIComponent(card.dataset.runId)}/tools/${encodeURIComponent(data.toolCallId)}/details?${query}`, {signal: card.detailAbort?.signal});
      if (state.context !== context || card.detailVersion !== version || !card.open || !card.isConnected) return;
      high = page.high_watermark;
      cursor = page.next;
      // 每页替换显示窗口，避免展开后累计保留整个工具输出。
      pre.textContent = page.records.map(record => {
        const parts = record.parts.map(part => part.text).join('');
        return parts || JSON.stringify(record.payload, null, 2);
      }).join('\n');
      next.textContent = cursor ? '下一页详情' : '已读完';
      next.disabled = !cursor;
    } catch (error) {
      if (state.context !== context || card.detailVersion !== version) return;
      pre.textContent = `详情加载失败：${error.message}`;
      next.disabled = false;
    }
  };
  next.addEventListener('click', load);
  if (card.detailRemote) load();
}

function toolGroupLabel(card) {
  const data = state.toolData.get(card.dataset.toolKey);
  const summary = toolSummary(data);
  if (summary === '查看图片' || summary === '执行命令') return summary;
  return TOOL_KIND_LABELS[data.kind] || toolSummary(data);
}

function placeToolNodes(parent, nodes) {
  nodes.forEach((node, index) => {
    if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null);
  });
  while (parent.children.length > nodes.length) parent.lastElementChild.remove();
}

function updateToolGroup(group, cards, label) {
  if (!group) {
    group = document.createElement('details');
    group.className = 'tool-group';
    const items = document.createElement('div');
    items.className = 'tool-group-items';
    items.tabIndex = 0;
    items.setAttribute('role', 'region');
    items.setAttribute('aria-label', '各次工具调用');
    const header = createToolHeader({ title: label, status: 'completed' });
    const count = document.createElement('span');
    count.className = 'tool-group-count';
    header.insertBefore(count, header.querySelector('.tool-status'));
    group.append(header, items);
  }
  group.querySelector('h4').textContent = toolSummary({ title: label });
  group.querySelector('h4').title = label;
  group.querySelector('.tool-group-count').textContent = `· ${cards.length} 次`;
  const records = cards.map(card => state.toolData.get(card.dataset.toolKey));
  const failed = records.filter(data => data.status === 'failed').length;
  const running = records.some(data => data.status === 'in_progress');
  const pending = records.some(data => data.status === 'pending');
  const completed = records.every(data => data.status === 'completed');
  const status = group.querySelector('.tool-status');
  status.textContent = [running ? '执行中' : pending ? '待执行' : '', failed ? `${failed} 次失败` : ''].filter(Boolean).join(' · ') || (completed ? '已完成' : '未知状态');
  status.className = completed ? 'tool-status sr-only' : 'tool-status';
  status.dataset.status = failed ? 'failed' : running ? 'in_progress' : pending ? 'pending' : completed ? 'completed' : 'unknown';
  placeToolNodes(group.querySelector('.tool-group-items'), cards);
  return group;
}

function groupToolCalls(content) {
  const groups = new Map();
  const nodes = Array.from(content.children).flatMap(node => {
    if (!node.classList.contains('tool-group')) return [node];
    const cards = Array.from(node.querySelector('.tool-group-items').children);
    groups.set(cards[0], node);
    return cards;
  });
  const result = [];
  for (let index = 0; index < nodes.length;) {
    const first = nodes[index++];
    if (!first.dataset.toolKey) {
      result.push(first);
      continue;
    }
    const label = toolGroupLabel(first);
    const cards = [first];
    while (index < nodes.length && nodes[index].dataset.toolKey && toolGroupLabel(nodes[index]) === label) cards.push(nodes[index++]);
    result.push(cards.length === 1 ? first : updateToolGroup(groups.get(first), cards, label));
  }
  placeToolNodes(content, result);
}

function appendDetails(row, runElement, summaryText, target = '.work-items') {
  const details = document.createElement('details');
  details.className = 'event-details';
  const summary = document.createElement('summary');
  summary.textContent = summaryText;
  const pre = document.createElement('pre');
  const display = { ...row.payload };
  delete display.raw;
  pre.textContent = JSON.stringify(display, null, 2);
  details.append(summary, pre);
  runElement.querySelector(target).append(details);
}

function renderPlan(row, runElement) {
  let card = runElement.querySelector('.plan-card');
  const expanded = card?.querySelector('details')?.open || false;
  if (!card) {
    card = document.createElement('section');
    card.className = 'plan-card';
    runElement.querySelector('.work-items').append(card);
  }
  const title = document.createElement('h4');
  const removed = row.payload.sessionUpdate === 'plan_removed';
  title.textContent = removed ? '计划已移除' : '执行计划';
  const list = document.createElement('ul');
  list.className = 'plan-list';
  for (const entry of removed ? [] : row.payload.entries || []) {
    const item = document.createElement('li');
    item.dataset.status = entry.status;
    const mark = ({ completed: '✓', in_progress: '◉', pending: '○' })[entry.status] || '○';
    const label = ({ completed: '已完成', in_progress: '进行中', pending: '待处理' })[entry.status] || entry.status;
    item.textContent = `${mark} ${entry.content}（${label}）`;
    list.append(item);
  }
  const details = document.createElement('details');
  details.className = 'event-details';
  details.open = expanded;
  const summary = document.createElement('summary');
  summary.textContent = '计划详情';
  const pre = document.createElement('pre');
  const { raw, ...payload } = row.payload;
  pre.textContent = JSON.stringify(payload, null, 2);
  details.append(summary, pre);
  card.replaceChildren(title, list, details);
}

function appendError(row, runElement) {
  const marker = document.createElement('div');
  marker.className = 'error-marker';
  marker.hidden = true;
  runElement.querySelector('.work-items').append(marker);
  const card = document.createElement('section');
  card.className = 'event-error';
  const message = document.createElement('p');
  const error = row.payload.error;
  message.textContent = `执行出错：${typeof error === 'string' ? error : error?.message || 'Agent 返回了错误，展开查看详情。'}`;
  card.append(message);
  const alerts = runElement.querySelector('.run-alerts');
  alerts.hidden = false;
  alerts.append(card);
  appendDetails(row, runElement, '错误详情', '.run-alerts');
}

function applyPermission(record, context) {
  if (state.context !== context || context.generation !== state.generation) return;
  const previous = state.permissions.get(record.id);
  if (previous && previous.status !== 'pending' && record.status === 'pending') return;
  const merged = { ...previous, ...record };
  state.permissions.set(record.id, merged);
  context.permissionRevision = (context.permissionRevision || 0) + 1;
  let card = state.permissionCards.get(record.id);
  if (!card) {
    card = document.createElement('article');
    card.className = 'tool-card permission-card';
    card.dataset.permissionId = record.id;
    card.workSlot = document.createElement('div');
    card.workSlot.className = 'permission-slot';
    ensureRun(merged.run_id).querySelector('.work-items').append(card.workSlot);
    state.permissionCards.set(record.id, card);
  }
  renderPermissionCard(card, merged, context);
  const run = ensureRun(merged.run_id);
  const alerts = run.querySelector('.run-alerts');
  (merged.status === 'pending' ? alerts : card.workSlot).append(card);
  alerts.hidden = alerts.children.length === 0;
  updateExecutionStatus();
  followTimeline();
}

function renderPermissionCard(card, record, context) {
  const expanded = card.querySelector('details')?.open || false;
  const payload = record.payload || {};
  const tool = payload.tool_call || {};
  const header = document.createElement('div');
  header.className = 'tool-card-header';
  const title = document.createElement('h4');
  title.textContent = `权限：${tool.title || tool.toolCallId || record.id}`;
  const badge = document.createElement('span');
  badge.className = 'badge';
  badge.textContent = ({ pending: '等待选择', answered: '已回答', expired: '已过期', resolved: '已结束' })[record.status];
  header.append(title, badge);
  const details = document.createElement('details');
  details.open = expanded;
  const summary = document.createElement('summary');
  summary.textContent = '工具详情';
  const pre = document.createElement('pre');
  pre.textContent = JSON.stringify(tool, null, 2);
  details.append(summary, pre);
  const actions = document.createElement('div');
  actions.className = 'permission-actions';
  for (const option of payload.options || []) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.optionId = option.optionId;
    button.textContent = `${option.name}（${option.kind}）`;
    button.disabled = record.status !== 'pending' || Boolean(record.submitting) || Boolean(record.uncertain);
    button.addEventListener('click', () => answerPermission(record.id, option.optionId, context));
    actions.append(button);
  }
  const message = document.createElement('p');
  message.className = 'form-error';
  message.textContent = record.error || '';
  card.replaceChildren(header, details, actions, message);
}

function restorePermissions(interactions, context) {
  for (const interaction of interactions) {
    if (interaction.kind !== 'permission') continue;
    applyPermission({ ...interaction, uncertain: false, payload: typeof interaction.payload === 'string'
      ? JSON.parse(interaction.payload) : interaction.payload }, context);
  }
}

async function refreshPermissions(context) {
  let revision;
  let detail;
  do {
    revision = context.permissionRevision || 0;
    detail = await api(`/api/sessions/${encodeURIComponent(context.sessionId)}?summary=true`);
    if (state.context !== context || context.generation !== state.generation) return;
  } while (revision !== (context.permissionRevision || 0));
  const pending = new Set(detail.interactions.filter(item => item.kind === 'permission').map(item => item.id));
  for (const record of state.permissions.values()) {
    if (record.status === 'pending' && !pending.has(record.id)) {
      applyPermission({ ...record, status: 'resolved', submitting: false }, context);
    }
  }
  restorePermissions(detail.interactions, context);
}

async function answerPermission(permissionId, optionId, context) {
  if (state.context !== context || context.generation !== state.generation) return;
  const record = state.permissions.get(permissionId);
  if (!record || record.status !== 'pending' || record.submitting || record.uncertain) return;
  applyPermission({ ...record, submitting: true, error: '' }, context);
  try {
    const answered = await api(`/api/interactions/${encodeURIComponent(permissionId)}/answer`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ option_id: optionId }),
    });
    applyPermission({ ...answered, payload: JSON.parse(answered.payload), submitting: false }, context);
  } catch (error) {
    if (state.context !== context || context.generation !== state.generation) return;
    try {
      await refreshPermissions(context);
      if (state.context !== context || context.generation !== state.generation) return;
      applyPermission({ ...state.permissions.get(permissionId), submitting: false, error: error.message }, context);
    } catch (refreshError) {
      // 尚未核对当前状态，保持按钮禁用，用户可重试加载或刷新。
      applyPermission({ ...state.permissions.get(permissionId), submitting: false, uncertain: true,
        error: `${error.message}；${refreshError.message}` }, context);
    }
  }
}

function appendUserMessage(row, runElement) {
  const content = runElement.querySelector('.run-content');
  if (content.querySelector('.user-message')) return; // 在所属 Run 只显示一次
  const block = document.createElement('div');
  block.className = 'user-message';
  block.textContent = row.payload.message || '';
  content.prepend(block);
}

function applyRunStatusEvent(row, runElement, context) {
  const runId = row.run_id;
  const record = state.runRecords.get(runId) || { id: runId, session_id: row.session_id, status: 'queued' };
  const nextStatus = row.payload.status || record.status;
  // 已知终态不能被迟到的 queued/running 历史事件回退（权威详情先到、历史重放后到）
  const regression = TERMINAL_STATUSES.has(record.status) && !TERMINAL_STATUSES.has(nextStatus);
  if (!regression) record.status = nextStatus;
  if (row.created_at) {
    if (nextStatus === 'queued') record.created_at ||= row.created_at;
    if (TERMINAL_STATUSES.has(nextStatus)) record.finished_at ||= row.created_at;
  }
  for (const field of ['error', 'stop_reason', 'result']) {
    if (row.payload[field] != null) record[field] = row.payload[field];
  }
  state.runRecords.set(runId, record);
  updateRunHeader(runElement, record);
  renderRunItem(record);
  if (row.payload.status === 'running' || TERMINAL_STATUSES.has(row.payload.status)) {
    refreshSessionNativeId(context);
  }
  if (TERMINAL_STATUSES.has(row.payload.status)) {
    if (!context.replaying) refreshRun(runId, true);
    if (!context.replaying) refreshRunFileChanges(runElement, context);
    refreshTaskChanges(context);
  }
}

function renderEvent(row, context) {
  if (row.kind === 'memory.context') {
    const panel = $('#session-memory-context');
    if (panel) {
      panel.hidden = false;
      $('#session-memory-label').textContent = row.payload.status === 'loaded' ? '本轮读取的长期记忆（参考资料）' : '本轮长期记忆不可用，继续执行';
      $('#session-memory-text').textContent = row.payload.context || '';
    }
    return;
  }
  if (row.kind === 'session.deleted') {
    applyDeletedSessions(row.payload.session_ids);
    return;
  }
  if (row.kind === 'session.config') {
    if (row.id <= (context.configEventId || 0)) return;
    context.configEventId = row.id;
    context.configuration = row.payload;
    const warning = $('#session-config-warning');
    if (warning) {
      warning.textContent = (row.payload.warnings || []).map(item => `${item.id}：${item.reason}`).join('；');
      warning.hidden = !warning.textContent;
    }
    return;
  }
  if (row.kind === 'raw') { sealText(row.run_id); return; }
  if (row.kind === 'task.worktree_ready') {
    context.task = { ...context.task, id: row.task_id || row.payload.task_id };
    refreshTaskChanges(context);
    scheduleSidebarRefresh();
    return;
  }
  if (row.kind === 'session.model') {
    rememberSessionModel(row, context);
    renderComposerAgent();
    return;
  }
  if (['session.title', 'task.created', 'task.status'].includes(row.kind)) {
    if (row.kind === 'session.title' && row.payload.title?.trim()) {
      const title = row.payload.title.trim().replace(/\s+/g, ' ');
      if (row.id > (context.titleEventId || 0)) {
        context.titleEventId = row.id;
        $('#session-title').textContent = title;
        const session = state.sessions.find(item => item.id === context.sessionId);
        if (session) session.title = title;
      }
    }
    scheduleSidebarRefresh();
    return;
  }
  if (!['user.message', 'agent.text', 'agent.tool', 'run.status', 'permission.requested',
    'permission.answered', 'permission.expired', 'agent.plan', 'error'].includes(row.kind) || !row.run_id) return;
  const knownRun = state.runRecords.get(row.run_id);
  if (knownRun?.session_id && knownRun.session_id !== context.sessionId) return;
  const runId = row.run_id;
  const runElement = ensureRun(runId);
  if (row.created_at && !runElement.workHasEventTime) {
    runElement.workStartedAt = Date.parse(state.runRecords.get(runId)?.created_at || row.created_at);
    runElement.workHasEventTime = true;
  }
  restoreRunAnswer(runElement);
  if (row.kind !== 'agent.text') sealText(runId);
  if (row.kind !== 'agent.text' && row.kind !== 'run.status') {
    const last = runElement.querySelector('.work-items').lastElementChild;
    if (last?.classList.contains('thought-block')) last.querySelector('summary').textContent = '思考过程';
  }
  switch (row.kind) {
    case 'user.message':
      appendUserMessage(row, runElement);
      scheduleSidebarRefresh();
      if (currentPending() || [...state.runs.values()].some(run => run.workRequest)) refreshRun(runId);
      break;
    case 'agent.text':
      appendText(row, runElement);
      break;
    case 'agent.tool':
      applyToolEvent(row, runElement);
      refreshSessionNativeId(context); // 工具活动说明连接已建立，是合理的合并刷新时机
      break;
    case 'run.status':
      applyRunStatusEvent(row, runElement, context);
      if (TERMINAL_STATUSES.has(row.payload.status)) scheduleSidebarRefresh();
      break;
    case 'permission.requested':
    case 'permission.answered':
    case 'permission.expired':
      if (row.payload.permission_id) {
        const { permission_id, status, ...payload } = row.payload;
        const previous = state.permissions.get(permission_id);
        applyPermission({ id: permission_id, run_id: row.run_id, status,
          payload: { ...previous?.payload, ...payload }, submitting: false }, context);
      }
      break;
    case 'agent.plan':
      renderPlan(row, runElement);
      break;
    case 'error':
      appendError(row, runElement);
      break;
  }
  updateWorkLog(runElement);
  settleRunContent(runElement);
  followTimeline();
}

/* 历史与实时统一入口：验证上下文身份与归属、按数据库 id 去重、DOM 成功后才推进游标。 */
function applyEvent(row, context) {
  // 本函数自身校验 context 就是当前选择且代次吻合，不依赖调用者的检查
  if (state.context !== context || context.generation !== state.generation) return true;
  if (row.project_id !== context.projectId) return true; // 服务端已过滤，防御性忽略
  if (row.session_id != null && row.session_id !== context.sessionId) return true;
  if (row.id <= context.lastEventId) return true; // 已应用过
  try {
    renderEvent(row, context);
  } catch (error) {
    handleRenderError(context, error);
    return false;
  }
  context.lastEventId = row.id;
  return true;
}

function handleRenderError(context, error) {
  if (state.context !== context) return;
  context.liveFailed = true;
  closeStream();
  setStreamStatus('渲染事件失败，可重试加载', 'error');
  $('#stream-retry').hidden = false;
  console.error('applyEvent 渲染失败：', error);
}

/* ---------- 历史分页与 SSE ---------- */

function sealText(runId) {
  if (!runId) return;
  const run = state.runs.get(runId);
  if (!run) return;
  for (const [block, data] of state.textBlocks) {
    if (run.contains(block) && !data.closed) {
      data.closed = true;
      state.dirtyText.add(block);
    }
  }
  if (!state.context?.replaying) flushTextUpdates();
}

async function hydrateEvent(row) {
  if (row.kind === 'agent.tool' || !JSON.stringify(row.payload).includes('"$detail"')) return row;
  const fields = new Map();
  let after = -1;
  do {
    const page = await api(`/api/events/${row.id}/details?after_part=${after}`);
    for (const part of page.parts) fields.set(part.field, (fields.get(part.field) || '') + part.text);
    after = page.next;
  } while (after !== null);
  const restore = value => {
    if (value && typeof value === 'object' && value.$detail) {
      const text = fields.get(value.$detail) || '';
      return value.format === 'json' ? JSON.parse(text) : text;
    }
    if (value && typeof value === 'object' && !Array.isArray(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, restore(item)]));
    return value;
  };
  return {...row, payload: restore(row.payload)};
}

function historyContext(context) {
  context.loadedRuns ||= new Set();
  context.runCursors ||= new Map();
}

async function readRunHistory(context, records, high, includeContext = false) {
  const task = (context.historyChain || Promise.resolve()).then(() => readRunHistoryNow(context, records, high, includeContext));
  context.historyChain = task.catch(() => {});
  return task;
}

async function readRunHistoryNow(context, records, high, includeContext) {
  if (state.context !== context) return;
  historyContext(context);
  for (const record of records) {
    const existing = state.runRecords.get(record.id);
    if (!existing || !TERMINAL_STATUSES.has(existing.status)) state.runRecords.set(record.id, record);
  }
  let cursor = 0;
  context.replaying = true;
  try {
    while (true) {
      const query = new URLSearchParams({project_id: context.projectId, session_id: context.sessionId,
        after_event_id: String(cursor), through_event_id: String(high), limit: '100', include_context: String(includeContext)});
      for (const record of records) query.append('run_id', record.id);
      // 空 Run 页只查询不属于会话 Run 的元数据。
      if (!records.length) query.append('run_id', '');
      const rows = await api(`/api/events?${query}`);
      if (state.context !== context) return;
      if (!rows.length) break;
      for (let row of rows) {
        if (row.id <= (context.runCursors.get(row.run_id || 'context') || 0)) continue;
        row = await hydrateEvent(row);
        if (state.context !== context) return;
        renderEvent(row, context);
        context.runCursors.set(row.run_id || 'context', row.id);
      }
      const next = rows.at(-1).id;
      if (next <= cursor) throw new Error('历史游标未前进');
      cursor = next;
    }
    for (const record of records) {
      context.loadedRuns.add(record.id);
      const run = ensureRun(record.id);
      const slot = state.runSlots.get(record.id);
      if (slot) {
        if (slot.placeholder.isConnected) $('#timeline').insertBefore(run, slot.placeholder);
        state.historyObserver?.unobserve(slot.placeholder);
        slot.placeholder.remove();
        state.runSlots.delete(record.id);
      }
      updateRunHeader(run, state.runRecords.get(record.id));
      if (TERMINAL_STATUSES.has(state.runRecords.get(record.id)?.status)) sealText(record.id);
      refreshRunFileChanges(run, context);
    }
  } finally {
    context.replaying = false;
    flushTextUpdates();
  }
  renderRunList();
}

function evictRun(context, runId) {
  const run = state.runs.get(runId);
  if (!run) return;
  run.fileChangesAbort?.abort();
  if (state.fileView?.run === run) closeFileView();
  const placeholder = document.createElement('button');
  placeholder.className = 'history-placeholder';
  placeholder.textContent = '重新加载这一轮历史';
  placeholder.style.minHeight = `${run.getBoundingClientRect?.().height || 160}px`;
  $('#timeline').insertBefore(placeholder, run);
  state.runSlots.set(runId, {placeholder});
  for (const [block] of state.textBlocks) if (run.contains(block)) { state.textBlocks.delete(block); state.dirtyText.delete(block); }
  for (const [key, card] of state.tools) if (run.contains(card)) { card.detailAbort?.abort(); card.detailVersion = (card.detailVersion || 0) + 1; state.tools.delete(key); state.toolData.delete(key); }
  for (const [key, card] of state.permissionCards) if (run.contains(card)) { state.permissionCards.delete(key); state.permissions.delete(key); }
  state.runItems.get(runId)?.remove();
  state.runItems.delete(runId);
  state.runFetchVersions.delete(runId);
  state.runRecords.delete(runId);
  run.remove();
  state.runs.delete(runId);
  context.loadedRuns.delete(runId);
  context.runCursors.delete(runId);
  const restore = async () => {
    if (placeholder.disabled || state.context !== context) return;
    placeholder.disabled = true;
    try {
      const summary = await api(`/api/runs/${encodeURIComponent(runId)}?summary=true`);
      await readRunHistory(context, [summary], context.lastEventId);
      if (state.context !== context) return;
      if (placeholder.isConnected) $('#timeline').insertBefore(state.runs.get(runId), placeholder);
      state.historyObserver?.unobserve(placeholder);
      placeholder.remove();
      state.runSlots.delete(runId);
      trimHistory(context, new Set([runId]));
    } catch (error) { placeholder.textContent = `加载失败，点击重试：${error.message}`; placeholder.disabled = false; }
  };
  placeholder.addEventListener('click', restore);
  if (typeof IntersectionObserver !== 'undefined') {
    state.historyObserver ||= new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting) entry.target.click();
    }, {root: $('#timeline')});
    state.historyObserver.observe(placeholder);
  }
}

function trimHistory(context, keep = new Set()) {
  historyContext(context);
  const candidates = [...state.runs.keys()].filter(id => !id.startsWith('pending:') && !['running', 'waiting_permission', 'cancel_requested'].includes(state.runRecords.get(id)?.status));
  const viewport = $('#timeline').getBoundingClientRect?.();
  if (viewport) candidates.sort((left, right) => {
    const distance = id => {
      const bounds = state.runs.get(id).getBoundingClientRect();
      return Math.abs((bounds.top + bounds.bottom - viewport.top - viewport.bottom) / 2);
    };
    return distance(right) - distance(left);
  });
  while (candidates.length > 60) {
    const index = candidates.findIndex(id => !keep.has(id));
    if (index < 0) break;
    evictRun(context, candidates.splice(index, 1)[0]);
  }
}

async function loadOlderHistory(context, button) {
  if (button.disabled || state.context !== context) return;
  button.disabled = true;
  const timeline = $('#timeline');
  const height = timeline.scrollHeight;
  const top = timeline.scrollTop;
  const anchor = Array.from(timeline.children).find(node => node.classList.contains('run') || node.classList.contains('history-placeholder'));
  try {
    const page = await api(`/api/sessions/${encodeURIComponent(context.sessionId)}/history?before=${context.historyBefore}`);
    if (state.context !== context) return;
    await readRunHistory(context, page.runs, page.high_watermark);
    if (state.context !== context) return;
    for (const record of page.runs) {
      const run = state.runs.get(record.id);
      if (run) timeline.insertBefore(run, anchor || null);
    }
    context.historyBefore = page.before;
    button.hidden = page.before === null;
    trimHistory(context, new Set(page.runs.map(run => run.id)));
    timeline.scrollTop = top + timeline.scrollHeight - height;
  } catch (error) { button.textContent = `加载失败，点击重试：${error.message}`; }
  finally { button.disabled = false; }
}

async function loadHistory(context) {
  setStreamStatus('加载历史…', 'loading');
  $('#stream-retry').hidden = true;
  try {
    const page = await api(`/api/sessions/${encodeURIComponent(context.sessionId)}/history`);
    if (state.context !== context) return;
    const records = [...new Map([...page.runs, ...page.active_runs].map(run => [run.id, run])).values()];
    await readRunHistory(context, records, page.high_watermark, true);
    if (state.context !== context) return;
    for (const row of page.metadata) renderEvent(row, context);
    context.lastEventId = page.high_watermark;
    context.historyBefore = page.before;
    if (page.before !== null && !$('#timeline .history-more')) {
      const button = document.createElement('button');
      button.className = 'history-more';
      button.textContent = '加载更早的会话';
      button.addEventListener('click', () => loadOlderHistory(context, button));
      $('#timeline').prepend(button);
    }
    trimHistory(context);
    await refreshPermissions(context);
    if (state.context !== context) return;
    context.ready = true;
    context.liveFailed = false;
    updateMessageForm();
    subscribeEvents(context);
  } catch (error) { handleLoadError(context, error); }
}

async function receiveLiveEvent(row, context) {
  if (state.context !== context || context.liveFailed || row.id <= context.lastEventId) return;
  historyContext(context);
  if (row.run_id && !context.loadedRuns.has(row.run_id) && !['task.created', 'task.status', 'task.worktree_ready'].includes(row.kind)) {
    const run = await api(`/api/runs/${encodeURIComponent(row.run_id)}?summary=true`);
    if (state.context !== context) return;
    if (run.session_id === context.sessionId) await readRunHistory(context, [run], row.id - 1);
  }
  row = await hydrateEvent(row);
  if (state.context !== context) return;
  if (applyEvent(row, context) && state.context === context) {
    if (row.run_id) context.runCursors.set(row.run_id, row.id);
    trimHistory(context, new Set([row.run_id]));
  }
}

function handleLoadError(context, error) {
  if (state.context !== context || context.generation !== state.generation) return;
  context.liveFailed = true;
  closeStream();
  setStreamStatus('加载历史失败，可重试', 'error');
  $('#stream-retry').hidden = false;
  console.error('历史加载失败：', error);
}

function subscribeEvents(context) {
  closeStream();
  const query = new URLSearchParams({
    project_id: context.projectId,
    session_id: context.sessionId,
    after_event_id: String(context.lastEventId),
    limit: String(HISTORY_PAGE_SIZE),
  });
  const source = new EventSource(`/api/stream?${query}`);
  state.source = source;
  source.addEventListener('open', () => {
    if (context.generation !== state.generation) return;
    setStreamStatus('实时已连接', 'connected');
  });
  source.addEventListener('lab', (message) => {
    if (context.generation !== state.generation) return;
    let row;
    try {
      row = JSON.parse(message.data);
    } catch (error) {
      handleRenderError(context, error);
      return;
    }
    if (state.source !== source) return;
    context.livePending = (context.livePending || 0) + 1;
    if (context.livePending > 256) { context.livePending -= 1; handleStreamError(context); return; }
    context.liveChain = (context.liveChain || Promise.resolve())
      .then(() => receiveLiveEvent(row, context))
      .catch(error => handleLoadError(context, error))
      .finally(() => { context.livePending -= 1; });
  });
  source.addEventListener('error', () => {
    if (context.generation !== state.generation) return;
    handleStreamError(context);
  });
  // EventSource 构造不等于已连接；open 事件到达前显示正在连接
  setStreamStatus('正在连接实时事件…', 'loading');
}

function handleStreamError(context) {
  if (state.source) {
    state.source.close();
    state.source = null;
  }
  setStreamStatus('连接中断，正在重连…', 'error');
  if (state.reconnectTimer !== null) return;
  state.reconnectTimer = setTimeout(() => {
    state.reconnectTimer = null;
    if (context.generation !== state.generation || state.context !== context) return;
    subscribeEvents(context); // 从最后成功应用的游标重建
  }, RECONNECT_DELAY_MS);
}

function closeStream() {
  if (state.reconnectTimer !== null) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
  if (state.source) {
    state.source.close();
    state.source = null;
  }
}

/* ---------- 会话切换 ---------- */

function resetConversation() {
  closeStream();
  state.generation += 1;
  state.context = null;
  state.draft = null;
  state.nativeId = null;
  state.harness = null;
  state.modelId = null;
  state.employeeName = null;
  resetTaskChanges();
  clearConversationDom();
  $('#session-title').textContent = '未选择会话';
  $('#session-cwd').textContent = '';
  renderProjectGit();
  renderComposerAgent();
  $('#session-id').textContent = '-';
  $('#session-harness').textContent = '-';
  $('#native-id').textContent = '-';
  setStreamStatus('等待选择会话', '');
  $('#stream-retry').hidden = true;
  $('#message-input').value = '';
  $('#message-error').textContent = '';
  updateMessageForm();
}

function clearConversationDom() {
  const memoryPanel = $('#session-memory-context');
  if (memoryPanel) { memoryPanel.hidden = true; $('#session-memory-text').textContent = ''; }
  closeFileView();
  for (const run of state.runs.values()) run.fileChangesAbort?.abort();
  const configWarning = $('#session-config-warning');
  if (configWarning) { configWarning.hidden = true; configWarning.textContent = ''; }
  if (state.workTimer !== null) clearTimeout(state.workTimer);
  state.workTimer = null;
  if (state.textFrame !== null) cancelAnimationFrame(state.textFrame);
  if (state.scrollFrame !== null) cancelAnimationFrame(state.scrollFrame);
  state.textFrame = state.scrollFrame = null;
  if (state.textTimer !== null) clearTimeout(state.textTimer);
  state.textTimer = null;
  state.historyObserver?.disconnect();
  state.historyObserver = null;
  state.runSlots.clear();
  state.textBlocks.clear();
  state.dirtyText.clear();
  state.cancelingRuns.clear();
  state.followingBottom = true;
  $('#scroll-bottom').hidden = true;
  $('#timeline').replaceChildren(createTimelineHint());
  state.runs.clear();
  for (const card of state.tools.values()) card.detailAbort?.abort();
  state.tools.clear();
  state.toolData.clear();
  state.runRecords.clear();
  state.runItems.clear();
  state.runFetchVersions.clear();
  state.permissions.clear();
  state.permissionCards.clear();
  renderRunList();
}

function createTimelineHint() {
  const hint = document.createElement('div');
  hint.className = 'welcome empty-hint';
  const mark = document.createElement('span');
  mark.className = 'welcome-mark';
  mark.textContent = '✳';
  mark.setAttribute('aria-hidden', 'true');
  const title = document.createElement('h2');
  title.textContent = state.context ? '今天一起完成什么？' : '从一个想法开始';
  const text = document.createElement('p');
  text.textContent = state.projectId ? '新建或选择会话，发送消息开始工作。' : '选择项目并新建会话，与 Agent 一起推进工作。';
  hint.append(mark, title, text);
  return hint;
}

async function selectSession(sessionId) {
  if (typeof TeamWorkspace !== 'undefined') {
    const session = state.sessions.find(item => item.id === sessionId);
    if (state.projects.find(item => item.id === state.projectId)?.team && session?.employee_id) {
      await TeamWorkspace.openSession(session);
      return;
    }
    if (!state.projects.find(item => item.id === state.projectId)?.team) TeamWorkspace.openProject(null);
  }
  const projectId = state.projectId;
  if (!projectId) return;
  // 切换开头：旧流关闭、代次递增、禁用发送、清空显示
  resetConversation();
  const context = {
    projectId,
    sessionId,
    generation: state.generation,
    lastEventId: 0, // 每次清空 DOM 重建，历史游标从 0 开始
  };
  state.context = context;
  renderComposerAgent();
  saveSelection();
  renderSessionList();
  updateMessageForm();
  setStreamStatus('加载会话详情…', 'loading');
  let detail;
  try {
    detail = await api(`/api/sessions/${encodeURIComponent(sessionId)}?summary=true`);
  } catch (error) {
    if (context.generation !== state.generation) return;
    setStreamStatus('会话详情加载失败', 'error');
    $('#message-error').textContent = `会话详情加载失败：${error.message}`;
    $('#stream-retry').hidden = false; // 保持禁用发送，提供重试加载路径
    updateMessageForm();
    return;
  }
  if (context.generation !== state.generation) return;
  if (detail.session.project_id !== projectId) {
    setStreamStatus('会话不属于当前项目', 'error');
    $('#stream-retry').hidden = false;
    return;
  }
  renderSessionDetail(detail.session);
  context.task = detail.task || null;
  refreshTaskChanges(context);
  if (detail.session.native_id) context.nativeIdResolved = true; // 详情已含原生 ID，无需再刷新
  for (const run of detail.runs || []) state.runRecords.set(run.id, run);
  restorePermissions(detail.interactions || [], context);
  renderRunList();
  const pending = currentPending();
  if (pending) showPendingWork(pending);
  await loadHistory(context);
}

/* ---------- 右栏 Run 列表 ---------- */

function renderRunList() {
  const list = $('#run-list');
  list.replaceChildren();
  if (state.runRecords.size === 0) {
    const hint = document.createElement('p');
    hint.className = 'empty-hint';
    hint.textContent = '尚无执行记录。';
    list.append(hint);
    updateExecutionStatus();
    return;
  }
  for (const record of state.runRecords.values()) renderRunItem(record);
}

function renderRunItem(record) {
  const list = $('#run-list');
  const hint = list.querySelector('.empty-hint');
  if (hint) hint.remove();
  let item = state.runItems.get(record.id);
  if (!item) {
    item = document.createElement('div');
    item.className = 'run-item';
    item.dataset.runId = record.id;
    state.runItems.set(record.id, item);
  }
  if (!item.isConnected) list.append(item); // 列表被重渲染清空后，重挂已有节点，避免重复 Run
  const idSpan = document.createElement('div');
  idSpan.className = 'run-id';
  idSpan.textContent = record.id;
  const badge = document.createElement('span');
  badge.className = 'badge';
  if (record.status) {
    badge.dataset.status = record.status;
    badge.textContent = `${record.status}（${STATUS_LABELS[record.status] || record.status}）`;
  } else {
    badge.textContent = '未知状态';
  }
  const detail = document.createElement('div');
  detail.className = 'run-item-detail';
  const fragments = [];
  if (record.error) fragments.push(`错误：${record.error}`);
  if (record.stop_reason) fragments.push(`停止原因：${record.stop_reason}`);
  if (record.result || record.result_preview) {
    const text = String(record.result || record.result_preview);
    fragments.push(`结果：${text.length > 160 ? `${text.slice(0, 160)}…` : text}`);
  }
  detail.textContent = fragments.join('\n');

  const actions = document.createElement('div');
  actions.className = 'run-item-actions';
  const refreshButton = document.createElement('button');
  refreshButton.type = 'button';
  refreshButton.textContent = '查询';
  refreshButton.addEventListener('click', () => refreshRun(record.id));
  const cancelButton = document.createElement('button');
  cancelButton.type = 'button';
  cancelButton.dataset.action = 'cancel';
  if (CANCELABLE_STATUSES.has(record.status)) {
    cancelButton.textContent = '取消';
  } else if (record.status === 'cancel_requested') {
    cancelButton.textContent = '取消中';
    cancelButton.disabled = true;
  } else if (TERMINAL_STATUSES.has(record.status)) {
    cancelButton.textContent = '已结束';
    cancelButton.disabled = true;
  } else {
    cancelButton.textContent = '取消';
    cancelButton.disabled = true;
  }
  cancelButton.addEventListener('click', () => cancelRun(record.id)); // 闭包绑定 run_id
  if (state.cancelingRuns.has(record.id)) cancelButton.disabled = true;
  actions.append(refreshButton, cancelButton);
  item.replaceChildren(idSpan, badge, detail, actions);
  updateExecutionStatus();
}

async function refreshRun(runId, summary = false) {
  const context = state.context; // 捕获调用时的上下文身份
  const version = (state.runFetchVersions.get(runId) || 0) + 1;
  state.runFetchVersions.set(runId, version);
  let run;
  try {
    run = await api(`/api/runs/${encodeURIComponent(runId)}${summary ? '?summary=true' : ''}`);
  } catch {
    return; // 查询失败保留当前展示，可再点查询
  }
  // 版本号在 A→B→A 切换后可能重号，须同时核对捕获的 context 身份
  if (state.runFetchVersions.get(runId) !== version) return; // 旧请求不覆盖新请求
  if (!context || state.context !== context || context.generation !== state.generation) return; // 切换后丢弃
  if (run.session_id !== context.sessionId) return;
  const existing = state.runRecords.get(runId);
  if (existing && TERMINAL_STATUSES.has(existing.status) && !TERMINAL_STATUSES.has(run.status)) {
    return; // 迟到快照不能把已知终态回退到 queued/running
  }
  state.runRecords.set(runId, run);
  const pending = [...state.runs.values()].find(item => item.workRequest?.requestId === run.request_id)?.workRequest || currentPending();
  if (pending && run.request_id === pending.requestId) bindPendingRun(pending, runId);
  renderRunItem(run);
  const runElement = state.runs.get(runId);
  if (runElement) {
    updateRunHeader(runElement, run);
    if (!context.replaying) refreshRunFileChanges(runElement, context);
  }
}

async function cancelRun(runId) {
  const context = state.context; // 捕获调用时的上下文身份
  const item = state.runItems.get(runId);
  if (!item || state.cancelingRuns.has(runId)) return;
  state.cancelingRuns.add(runId);
  updateExecutionStatus();
  const cancelButton = item.querySelector('button[data-action="cancel"]');
  const detail = item.querySelector('.run-item-detail');
  if (cancelButton) cancelButton.disabled = true;
  if (detail) detail.textContent = '正在请求取消…';
  try {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' });
    if (!context || state.context !== context || context.generation !== state.generation) return; // 切换后丢弃
    const existing = state.runRecords.get(runId);
    if (existing && TERMINAL_STATUSES.has(existing.status) && !TERMINAL_STATUSES.has(run.status)) {
      return; // 已确认的终态不被迟到响应回退
    }
    state.runRecords.set(runId, run);
    renderRunItem(run);
    const runElement = state.runs.get(runId);
    if (runElement) updateRunHeader(runElement, run);
  } catch (error) {
    if (!context || state.context !== context || context.generation !== state.generation) return;
    if (detail) detail.textContent = `取消失败：${error.message}`;
    if (cancelButton) cancelButton.disabled = false;
    $('#message-error').textContent = `取消失败：${error.message}`;
  } finally {
    if (state.context === context) {
      state.cancelingRuns.delete(runId);
      updateExecutionStatus();
    }
  }
}

/* ---------- 消息提交：request_id 幂等与本地重试 ---------- */

function currentPending() {
  return state.context ? state.pendingBySession.get(state.context.sessionId) : null;
}

function isSending(sessionId) {
  return state.sendingBySession.get(sessionId) || false;
}

function updateSendButton() {
  const context = state.context;
  const run = context && !state.draft ? currentExecution() : null;
  const pausing = run && (CANCELABLE_STATUSES.has(run.status) || run.status === 'cancel_requested');
  const canceling = pausing && (run.status === 'cancel_requested' || state.cancelingRuns.has(run.id));
  const send = $('#send-button');
  send.type = pausing ? 'button' : 'submit';
  send.title = pausing ? (canceling ? '暂停中' : '暂停生成') : '发送消息';
  send.setAttribute('aria-label', send.title);
  $('#send-icon').toggleAttribute('hidden', Boolean(pausing));
  $('#pause-icon').toggleAttribute('hidden', !pausing);
  const ready = Boolean(context?.ready || (state.draft?.projectId && !state.draft.creating));
  send.disabled = pausing ? Boolean(canceling) : !ready || Boolean(currentPending()) || Boolean(context && isSending(context.sessionId));
}

function updateMessageForm() {
  const context = state.context;
  const ready = Boolean(context?.ready || (state.draft?.projectId && !state.draft.creating));
  const pending = currentPending();
  const sending = context ? isSending(context.sessionId) : false;
  const input = $('#message-input');
  const retry = $('#retry-button');
  const discard = $('#discard-button');
  if (pending) {
    input.value = pending.message; // 锁定正文
    input.readOnly = true;
    input.disabled = false;
    retry.hidden = false;
    retry.disabled = sending;
    discard.hidden = false;
    discard.disabled = sending;
  } else {
    input.readOnly = false;
    input.disabled = !ready || sending;
    retry.hidden = true;
    discard.hidden = true;
  }
  $('#harness').disabled = Boolean(state.draft?.creating);
  $('#project-select').disabled = Boolean(state.draft?.creating);
  updateSendButton();
  if (typeof EmployeesUI !== 'undefined') EmployeesUI.renderChoice();
}

async function submitPending(pending) {
  state.sendingBySession.set(pending.sessionId, true);
  if (state.context?.sessionId === pending.sessionId) showPendingWork(pending);
  updateMessageForm();
  try {
    const submitted = await api(`/api/sessions/${encodeURIComponent(pending.sessionId)}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: pending.message, request_id: pending.requestId }),
    });
    scheduleSidebarRefresh();
    if (state.context?.sessionId === pending.sessionId) bindPendingRun(pending, submitted.run_id);
    if (state.context?.sessionId === pending.sessionId && !state.runRecords.has(submitted.run_id)) {
      const run = { id: submitted.run_id, session_id: pending.sessionId, request_id: pending.requestId, status: submitted.status };
      state.runRecords.set(run.id, run);
      renderRunItem(run);
    }
    state.pendingBySession.delete(pending.sessionId);
    state.sendingBySession.delete(pending.sessionId);
    // 仅当前上下文仍匹配才清空输入并更新右栏
    if (state.context && state.context.sessionId === pending.sessionId) {
      if ($('#message-input').value === pending.message) $('#message-input').value = '';
      $('#message-error').textContent = '';
      updateMessageForm();
      await refreshRun(submitted.run_id);
    } else {
      updateMessageForm();
    }
  } catch (error) {
    state.sendingBySession.delete(pending.sessionId);
    // 网络/HTTP 失败保留 pending 与正文，显示“重试原消息”
    if (state.context && state.context.sessionId === pending.sessionId) {
      const run = state.runs.get(pending.runId || `pending:${pending.requestId}`);
      if (run) {
        run.workStatus = 'send_failed';
        updateWorkLog(run);
      }
      $('#message-error').textContent = `发送失败：${error.message}。可点击“重试原消息”用同一 request_id 重发；服务端可能已接收。`;
      updateMessageForm();
    }
  }
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.draft) {
    await sendDraftMessage();
    return;
  }
  const context = state.context;
  if (!context || !context.ready || currentPending() || isSending(context.sessionId)) return;
  const text = $('#message-input').value;
  if (!text.trim()) {
    $('#message-error').textContent = '消息不能为空白。';
    return;
  }
  const pending = {
    projectId: context.projectId,
    sessionId: context.sessionId,
    message: text, // 保存原文，trim 仅用于校验
    requestId: crypto.randomUUID(),
  };
  state.pendingBySession.set(pending.sessionId, pending);
  updateMessageForm();
  await submitPending(pending);
}

async function sendDraftMessage() {
  const draft = state.draft;
  if (draft.creating || !draft.projectId) return;
  const text = $('#message-input').value;
  if (!text.trim()) {
    $('#message-error').textContent = '消息不能为空白。';
    return;
  }
  draft.creating = true;
  $('#message-error').textContent = '';
  updateMessageForm();
  let created;
  try {
    created = await api('/api/sessions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: draft.projectId, ...(draft.employeeId ? {employee_id: draft.employeeId} : {harness: draft.harness}) }),
    });
  } catch (error) {
    draft.creating = false;
    if (state.draft === draft) {
      $('#message-error').textContent = `创建会话失败：${error.message}`;
      updateMessageForm();
    }
    return;
  }
  const pending = { projectId: draft.projectId, sessionId: created.id, message: text, requestId: crypto.randomUUID() };
  state.pendingBySession.set(created.id, pending);
  if (!state.sessions.some(session => session.id === created.id)) state.sessions.push({ ...created, title: '新会话' });
  if (state.draft === draft) {
    state.draft = null;
    state.context = { projectId: draft.projectId, sessionId: created.id, generation: state.generation, lastEventId: 0, ready: true };
    renderSessionDetail(created);
    renderSessionList();
    saveSelection();
    subscribeEvents(state.context);
  }
  scheduleSidebarRefresh();
  await submitPending(pending);
}

async function retryMessage() {
  const pending = currentPending();
  if (!pending || isSending(pending.sessionId)) return;
  await submitPending(pending); // 同 session 同正文同 request_id 重发
}

function discardPending() {
  const context = state.context;
  if (!context) return;
  const pending = state.pendingBySession.get(context.sessionId);
  if (!pending || isSending(context.sessionId)) return;
  const run = state.runs.get(`pending:${pending.requestId}`);
  if (run) {
    run.workStatus = 'abandoned';
    updateWorkLog(run);
  }
  state.pendingBySession.delete(context.sessionId);
  $('#message-error').textContent = '已放弃本地重试，恢复编辑；服务端可能已接收该消息，执行结果仍会出现在时间线。';
  updateMessageForm();
}

/* ---------- 表单与初始化 ---------- */

let projectDirectoryRequest = 0;

function closeProjectDirectoryBrowser() {
  projectDirectoryRequest++;
  $('#project-directory-browser').hidden = true;
}

async function browseProjectDirectory(path) {
  if (state.projectCreating) return;
  const request = ++projectDirectoryRequest;
  $('#project-directory-use').disabled = true;
  $('#project-error').textContent = '';
  try {
    const data = await api(`/api/project-directories${path ? `?${new URLSearchParams({path})}` : ''}`);
    if (request !== projectDirectoryRequest || !$('#project-dialog').open) return;
    $('#project-directory-current').textContent = data.path;
    const parent = $('#project-directory-parent');
    parent.disabled = !data.parent;
    parent.onclick = () => browseProjectDirectory(data.parent);
    for (const [selector, entries] of [
      ['#project-directory-roots', data.roots.map(root => ({name: root, path: root}))],
      ['#project-directory-entries', data.directories],
    ]) {
      const container = $(selector);
      container.replaceChildren();
      for (const entry of entries) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = entry.name;
        button.addEventListener('click', () => browseProjectDirectory(entry.path));
        container.append(button);
      }
    }
    if (!data.directories.length) $('#project-directory-entries').textContent = '此目录没有子文件夹。';
    $('#project-directory-use').disabled = false;
  } catch (error) {
    if (request === projectDirectoryRequest && $('#project-dialog').open) $('#project-error').textContent = error.message;
  }
}

async function openProjectDirectoryBrowser(name) {
  if (state.projectCreating) return;
  $('#project-directory-browser').hidden = false;
  $('#project-directory-notice').textContent = name
    ? `浏览器未提供“${name}”的完整路径，请选择该文件夹后确认。`
    : '选择本机上的项目目录，确认后填入项目路径。';
  await browseProjectDirectory(normalizeProjectPath($('#project-path').value));
}

function openProjectDialog() {
  $('#project-error').textContent = '';
  $('#project-dialog').showModal();
  $('#project-path').focus();
  if (typeof TeamsUI !== 'undefined') TeamsUI.projectOptions().catch(error => {$('#project-error').textContent = error.message;});
}

function normalizeProjectPath(value) {
  let path = value.trim().replace(/^(["'])(.*)\1$/, '$2');
  if (/^file:\/\//i.test(path)) {
    const url = new URL(path);
    path = decodeURIComponent(url.pathname);
    path = url.hostname ? `//${url.hostname}${path}` : path.replace(/^\/([a-z]:\/)/i, '$1');
  }
  return path;
}

async function dropProjectDirectory(event) {
  event.preventDefault();
  event.stopPropagation();
  $('#project-form').classList.remove('project-drag-over');
  if (state.projectCreating) return;
  const transfer = event.dataTransfer;
  const files = Array.from(transfer.files || []);
  const items = Array.from(transfer.items || []).filter(item => item.kind === 'file');
  const errorElement = $('#project-error');
  if (Math.max(files.length, items.length) > 1) {
    errorElement.textContent = '请一次拖入一个项目文件夹。';
    return;
  }
  const entry = items[0]?.webkitGetAsEntry?.();
  if (entry?.isFile) {
    errorElement.textContent = '请拖入项目文件夹，而不是文件。';
    return;
  }
  try {
    const uri = transfer.getData('text/uri-list').split(/\r?\n/).find(line => line && !line.startsWith('#'));
    const path = normalizeProjectPath(files[0]?.path || uri || transfer.getData('text/plain'));
    if (!/^(?:[a-z]:[\\/]|[\\/]{2}[^\\/]+[\\/][^\\/]+)/i.test(path)) {
      if (entry?.isDirectory) {
        await openProjectDirectoryBrowser(entry.name);
      } else {
        errorElement.textContent = '请拖入项目文件夹或完整的目录路径。';
      }
      return;
    }
    closeProjectDirectoryBrowser();
    $('#project-path').value = path;
    errorElement.textContent = '';
    $('#project-path').focus();
  } catch (error) {
    errorElement.textContent = `无法读取目录路径：${error.message}`;
  }
}

async function createProject(event) {
  event.preventDefault();
  if (state.projectCreating) return;
  const generation = state.generation;
  const errorElement = $('#project-error');
  errorElement.textContent = '';
  let body;
  try {
    body = {
      path: normalizeProjectPath($('#project-path').value),
    };
    if (typeof TeamsUI !== 'undefined' && $('#project-team').value) {
      body.team_id = $('#project-team').value;
      body.context_mode = $('#project-context-mode').value;
    }
    if (!body.path) throw new Error('项目路径不能为空');
  } catch (error) {
    errorElement.textContent = error.message;
    return;
  }
  state.projectCreating = true;
  closeProjectDirectoryBrowser();
  $('#project-directory-open').disabled = true;
  $('#project-create-submit').disabled = true;
  $('#project-path').disabled = true;
  try {
    const created = await api('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (generation !== state.generation) return;
    await loadProjects(); // 内部按代次渲染，保留用户当前选择
    if (generation !== state.generation) return; // 用户已切换项目，迟到响应不抢走新选择
    selectProject(created.id);
    $('#project-path').value = '';
    $('#project-dialog').close();
  } catch (error) {
    if (generation !== state.generation) return;
    errorElement.textContent = error.message; // 422/409 原样显示服务端 detail
  } finally {
    state.projectCreating = false;
    $('#project-directory-open').disabled = false;
    $('#project-create-submit').disabled = false;
    $('#project-path').disabled = false;
  }
}

async function createSession(event, projectId = state.projectId) {
  event.preventDefault();
  if (typeof TeamWorkspace !== 'undefined' && state.projects.find(item => item.id === projectId)?.team) {
    if (state.projectId !== projectId) selectProject(projectId);
    await TeamWorkspace.newLeader();
    return;
  }
  state.collapsed.delete('section:projects');
  state.collapsed.delete(`project:${projectId}`);
  saveSidebarPreferences();
  selectProject(projectId);
  if ($('#sidebar').dataset.drawer === 'true' && !$('#sidebar').hidden) {
    $('#sidebar').querySelector('[data-panel-toggle="left"]').click();
  }
  $('#message-input').focus();
}

function saveSelection() {
  try {
    localStorage.setItem(SELECTION_KEY, JSON.stringify({ projectId: state.projectId, sessionId: state.context ? state.context.sessionId : null }));
  } catch { /* localStorage 不可用时忽略 */ }
}

async function restoreSelection() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(SELECTION_KEY) || 'null');
  } catch {
    saved = null;
  }
  if (!saved || !saved.projectId) return;
  if (!state.projects.some((project) => project.id === saved.projectId)) return; // 记录已不存在
  selectProject(saved.projectId);
  if (!saved.sessionId) return;
  if (state.sessions.some((session) => session.id === saved.sessionId && session.project_id === saved.projectId)) {
    await selectSession(saved.sessionId);
  }
}

function bindEvents() {
  if (typeof ProjectGitUI !== 'undefined') ProjectGitUI.bind();
  bindSessionActions();
  $('#task-diff-refresh').addEventListener('click', () => refreshTaskChanges(state.context));
  const projectDialog = $('#project-dialog');
  const projectForm = $('#project-form');
  $('#project-directory-open').addEventListener('click', () => openProjectDirectoryBrowser(''));
  $('#project-directory-home').addEventListener('click', () => browseProjectDirectory(''));
  $('#project-directory-cancel').addEventListener('click', closeProjectDirectoryBrowser);
  $('#project-directory-use').addEventListener('click', () => {
    if (state.projectCreating || $('#project-directory-use').disabled) return;
    $('#project-path').value = $('#project-directory-current').textContent;
    $('#project-error').textContent = '';
    closeProjectDirectoryBrowser();
    $('#project-path').focus();
  });
  projectForm.addEventListener('dragover', event => {
    event.preventDefault();
    if (state.projectCreating) return;
    event.dataTransfer.dropEffect = 'copy';
    projectForm.classList.add('project-drag-over');
  });
  projectForm.addEventListener('dragleave', event => {
    if (!projectForm.contains(event.relatedTarget)) projectForm.classList.remove('project-drag-over');
  });
  projectForm.addEventListener('drop', dropProjectDirectory);
  for (const button of projectDialog.querySelectorAll('[data-project-dialog-close]')) {
    button.addEventListener('click', () => projectDialog.close());
  }
  projectDialog.addEventListener('click', event => {
    if (event.target === projectDialog) projectDialog.close();
  });
  projectDialog.addEventListener('keydown', event => event.stopPropagation());
  projectDialog.addEventListener('close', () => {
    closeProjectDirectoryBrowser();
    projectForm.classList.remove('project-drag-over');
    $('#projects-heading').querySelector('button[data-sidebar-key="add-project"]')?.focus({ preventScroll: true });
  });
  $('#project-select').addEventListener('change', event => {
    if (!state.draft || state.draft.creating) return;
    state.projectId = event.target.value || null;
    state.draft.projectId = state.projectId;
    renderProjectContext();
    renderSessionList();
    saveSelection();
    updateMessageForm();
  });
  $('#harness').addEventListener('change', event => {
    if (state.draft && !state.draft.creating) state.draft.harness = event.target.value;
  });
  $('#project-form').addEventListener('submit', createProject);
  $('#session-form').addEventListener('submit', createSession);
  $('#message-form').addEventListener('submit', sendMessage);
  $('#send-button').addEventListener('click', () => {
    const run = currentExecution();
    if ($('#send-button').type === 'button' && !$('#send-button').disabled && run && CANCELABLE_STATUSES.has(run.status)) {
      return cancelRun(run.id);
    }
  });
  $('#retry-button').addEventListener('click', retryMessage);
  $('#discard-button').addEventListener('click', discardPending);
  $('#stop-button').addEventListener('click', () => {
    const run = currentExecution();
    if (run && CANCELABLE_STATUSES.has(run.status)) cancelRun(run.id);
  });
  $('#timeline').addEventListener('scroll', () => {
    const timeline = $('#timeline');
    state.followingBottom = timeline.scrollHeight - timeline.clientHeight - timeline.scrollTop < 80;
    $('#scroll-bottom').hidden = state.followingBottom;
  }, { passive: true });
  $('#scroll-bottom').addEventListener('click', () => {
    state.followingBottom = true;
    followTimeline();
  });
  new ResizeObserver(followTimeline).observe($('#timeline'));
  $('#message-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      $('#message-form').requestSubmit();
    }
  });
  $('#stream-retry').addEventListener('click', () => {
    const context = state.context;
    if (!context) return;
    if (!context.ready && context.lastEventId === 0) {
      selectSession(context.sessionId); // 详情或首批历史尚未成功，从头重建
      return;
    }
    loadHistory(context); // DOM 保留时从最后成功游标继续
  });
  window.addEventListener('beforeunload', closeStream);
  const refreshSidebar = () => { if (!document.hidden) scheduleSidebarRefresh(); };
  window.addEventListener('focus', refreshSidebar);
  document.addEventListener('visibilitychange', refreshSidebar);
}

async function init() {
  WorkbenchLayout.init();
  loadSidebarPreferences();
  bindEvents();
  updateMessageForm();
  renderSessionList();
  try {
    await loadProjects();
    await loadSessions();
    await restoreSelection();
  } catch (error) {
    setStreamStatus('项目列表加载失败', 'error');
    console.error('初始化失败：', error);
  }
  startSidebarPolling();
  if (typeof HarnessSettingsUI !== 'undefined') HarnessSettingsUI.init(() => state.projectId);
}

init();
