const assert = require('node:assert/strict');
const { test } = require('node:test');
const { environment } = require('./frontend_dom.cjs');

function sidebar(width = 1440) {
  const env = environment(width);
  env.run(`
    state.projects = [{id: 'p', name: 'repo', path: 'D:/one/repo'}, {id: 'q', name: 'repo', path: 'D:/two/repo'}];
    state.projectId = 'p';
    state.sessions = [
      {id: 'a', project_id: 'p', parent_id: null, title: '主会话 A', harness: 'codex', created_at: '2026-10-10T00:00:00Z', updated_at: '2026-10-10T00:00:00Z', activity_event_id: 1},
      {id: 'b', project_id: 'p', parent_id: null, title: '主会话 B', harness: 'opencode', created_at: '2026-10-11T00:00:00Z', updated_at: '2026-10-11T00:00:00Z', activity_event_id: 2},
      {id: 'c', project_id: 'p', parent_id: 'a', title: '子会话 C', harness: 'codebuddy', created_at: '2026-10-12T00:00:00Z', updated_at: '2026-10-12T00:00:00Z', activity_event_id: 3},
      {id: 'd', project_id: 'p', parent_id: 'c', title: '孙会话 D', harness: 'codex', created_at: '2026-10-13T00:00:00Z', updated_at: '2026-10-13T00:00:00Z', activity_event_id: 4},
      {id: 'e', project_id: 'q', parent_id: null, title: '其他项目 E', harness: 'codex', created_at: '2026-10-12T00:00:00Z', updated_at: '2026-10-12T00:00:00Z', activity_event_id: 5},
    ];
    renderSessionList();
  `);
  return env;
}

test('项目树递归连接父子与孙会话，最近区跨项目只列主会话，后代活动提升祖先排序', () => {
  const env = sidebar();
  assert.deepEqual(env.get('#session-list').children.map(node => node.dataset.sessionId), ['a', 'e', 'b']);
  assert.equal(env.get('#children-a').querySelectorAll('.session-button').length, 2);
  assert.equal(env.get('#children-c').querySelector('.session-button').dataset.sessionId, 'd');
  const project = env.get('#project-sessions-p');
  assert.deepEqual(project.children.map(node => node.querySelector('.session-button').dataset.sessionId), ['a', 'b']);
  assert.equal(env.get('#children-a').hidden, false);
  const folders = env.document.querySelectorAll('.project-button');
  assert.deepEqual(folders.map(node => node.title), ['D:/one/repo', 'D:/two/repo']);
});

test('折叠按钮只切换分支，刷新与新子会话保留手动折叠，浏览器偏好可恢复', () => {
  const env = sidebar();
  env.run(`state.source = {close() { throw new Error('折叠不应断流'); }};`);
  const toggle = env.get('button[data-sidebar-key="children:a"]');
  toggle.focus();
  toggle.emit('click');
  assert.equal(env.get('#children-a').hidden, true);
  assert.equal(env.document.activeElement.dataset.sidebarKey, 'children:a');
  assert.equal(env.get('button[data-sidebar-key="children:a"]').getAttribute('aria-expanded'), 'false');
  env.run(`state.sessions.push({...state.sessions[2], id: 'new-child'}); renderSessionList();`);
  assert.equal(env.get('#children-a').hidden, true);
  assert.equal(env.get('#children-a').querySelectorAll('.session-button').length, 3);
  const restored = sidebar();
  restored.storage.set('agentsteam.sidebar.v1', env.storage.get('agentsteam.sidebar.v1'));
  restored.run('loadSidebarPreferences(); renderSessionList();');
  assert.equal(restored.get('#children-a').hidden, true);
});

test('最近项打开所属项目，项目展开且会话被选中，父树中子会话可单独打开', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    state.collapsed.add('project:q'); renderSessionList();
    let selected; selectSession = async id => { selected = id; };
    await $('#session-list').querySelector('button[data-session-id="e"]').listeners.click[0]();
    if (state.projectId !== 'q' || selected !== 'e' || state.collapsed.has('project:q')) throw new Error('跨项目打开失败');
    await openSidebarSession(state.sessions.find(item => item.id === 'd'));
    if (state.projectId !== 'p' || selected !== 'd') throw new Error('子会话无法打开');
  })()`);
});

test('相同活动时间按事件 ID 倒序，同级子会话保持在父节点下', () => {
  const env = sidebar();
  env.run(`
    for (const session of state.sessions) session.updated_at = '2026-10-18T00:00:00Z';
    state.sessions.push({...state.sessions[2], id: 'f', activity_event_id: 9});
    renderSessionList();
  `);
  assert.deepEqual(env.get('#session-list').children.map(node => node.dataset.sessionId), ['a', 'e', 'b']);
  const branches = env.get('#children-a').children;
  assert.deepEqual(branches.map(node => node.querySelector('.session-button').dataset.sessionId), ['f', 'c']);
});

test('内部任务事件、标题、未知事件与未关联事件不生成回合且推进历史游标', () => {
  const env = sidebar();
  env.run(`state.context = {generation: 0, projectId: 'p', sessionId: 'a', lastEventId: 0};`);
  const kinds = ['task.created', 'task.status', 'session.title', 'unknown.notification', 'raw'];
  kinds.forEach((kind, index) => env.run(`applyEvent(${JSON.stringify({
    id: index + 1, project_id: 'p', session_id: 'a', run_id: 'child-run', kind,
    payload: { title: 'Agent 新标题', status: 'running' },
  })}, state.context);`));
  env.run(`applyEvent({id: 6, project_id: 'p', session_id: 'a', kind: 'error', payload: {error: '未关联'}}, state.context);`);
  assert.equal(env.document.querySelectorAll('.run').length, 0);
  assert.equal(env.run('state.workTimer'), null);
  assert.equal(env.run('state.context.lastEventId'), 6);
  assert.equal(env.get('#session-title').textContent, 'Agent 新标题');
  assert.equal(env.run('state.sessions[0].title'), 'Agent 新标题');
  assert.equal(env.timers.size, 1); // 多个通知合并一次摘要刷新。
});

test('完成回答之后到达委派状态，保持最终回答、终态和计时不变', () => {
  const env = sidebar();
  env.run(`
    state.context = {generation: 0, projectId: 'p', sessionId: 'a', lastEventId: 0};
    refreshRun = async () => {}; refreshSessionNativeId = async () => {};
    state.runRecords.set('r', {id: 'r', session_id: 'a', status: 'completed', created_at: '2026-10-18T00:00:00Z', finished_at: '2026-10-18T00:00:12Z'});
    applyEvent({id: 1, project_id: 'p', session_id: 'a', run_id: 'r', kind: 'agent.text', payload: {channel: 'message', text: '最终回答'}}, state.context);
    applyEvent({id: 2, project_id: 'p', session_id: 'a', run_id: 'child', kind: 'task.status', payload: {status: 'review'}}, state.context);
  `);
  assert.equal(env.document.querySelectorAll('.run').length, 1);
  assert.equal(env.get('.work-log').open, false);
  assert.equal(env.get('.run-answer').querySelector('.agent-text') !== null, true);
  assert.equal(env.get('.work-label').textContent, 'Worked for 12秒');
  assert.equal(env.run('state.workTimer'), null);
});

test('摘要刷新不清空正文、输入和折叠状态，失败保留列表且后续可恢复', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    state.context = {generation: 0, projectId: 'p', sessionId: 'a', lastEventId: 0};
    $('#message-input').value = '尚未发送';
    const content = ensureRun('r');
    state.collapsed.add('children:a');
    const current = state.sessions;
    api = async () => { throw new Error('断线'); };
    try { await loadSessions(); } catch {}
    if (state.sessions !== current) throw new Error('失败清空了列表');
    api = async () => current.map(session => ({...session, title: '已更新 ' + session.title}));
    await loadSessions();
    if ($('#message-input').value !== '尚未发送' || state.runs.get('r') !== content || !$('#children-a').hidden) throw new Error('摘要刷新破坏了会话');
    if ($('#session-title').textContent !== '已更新 主会话 A') throw new Error('标题未同步');
  })()`);
});

test('摘要并发旧响应和标题事件之前的快照不能覆盖新数据，不抢项目选择', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    const original = state.sessions;
    let finishOld; api = () => new Promise(resolve => {finishOld = resolve;});
    const old = loadSessions();
    api = async () => original.map(session => ({...session, title: '新数据'}));
    await loadSessions();
    finishOld(original); await old;
    if (state.sessions[0].title !== '新数据') throw new Error('旧请求覆盖新请求');
    let finishSnapshot; api = () => new Promise(resolve => {finishSnapshot = resolve;});
    const snapshot = loadSessions();
    scheduleSidebarRefresh();
    state.sessions[0].title = '新事件'; state.projectId = 'q';
    finishSnapshot(original); await snapshot;
    if (state.sessions[0].title !== '新事件' || state.projectId !== 'q') throw new Error('迟到快照覆盖新状态');
  })()`);
});

test('新建会话进入草稿不请求后端，在输入框选项目和 Agent，首次发送才创建', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    bindEvents(); let requests = 0;
    api = async () => { requests++; throw new Error('新建草稿不应发请求'); };
    await createSession({preventDefault() {}});
    if (requests || state.context || !state.draft) throw new Error('没有进入草稿');
    if ($('#agent-choice').hidden || $('#project-select').hidden || !$('#composer-agent').hidden) throw new Error('草稿控件位置错误');
    $('#harness').emit('change', {target: {value: 'codebuddy'}});
    $('#project-select').emit('change', {target: {value: 'q'}});
    if (state.draft.harness !== 'codebuddy' || state.draft.projectId !== 'q') throw new Error('草稿选择丢失');
    $('#message-input').value = '第一条提问';
    globalThis.crypto = {randomUUID: () => 'stable-request'};
    globalThis.EventSource = class { addEventListener() {} close() {} };
    const calls = [];
    api = async (url, options) => {
      calls.push({url, body: options && JSON.parse(options.body)});
      if (url === '/api/sessions') return {id: 'new', project_id: 'q', harness: 'codebuddy', cwd: 'D:/two/repo', created_at: '2026-10-18T00:00:00Z'};
      if (url === '/api/sessions/new/messages') return {run_id: 'r', status: 'queued'};
      if (url === '/api/runs/r') return {id: 'r', session_id: 'new', status: 'queued', request_id: 'stable-request'};
      throw new Error('意外请求');
    };
    await sendMessage({preventDefault() {}});
    if (calls[0].body.project_id !== 'q' || calls[0].body.harness !== 'codebuddy') throw new Error('没有采用输入框选择');
    if (calls[1].body.message !== '第一条提问' || calls[1].body.request_id !== 'stable-request') throw new Error('首条消息错误');
    if (state.draft || !$('#agent-choice').hidden || !$('#project-select').hidden || $('#composer-agent').hidden) throw new Error('已有会话仍可选 Agent');
    if ($('#composer-agent').textContent !== 'CodeBuddy') throw new Error('已有会话 Agent 错误');
  })()`);
});

test('首次消息失败重试复用已创建的会话及请求 ID，创建中禁止重复发送', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    await createSession({preventDefault() {}});
    globalThis.crypto = {randomUUID: () => 'stable-request'};
    globalThis.EventSource = class { addEventListener() {} close() {} };
    $('#message-input').value = '首条消息';
    let finish; let creates = 0; let submits = 0; const bodies = [];
    api = async (url, options) => {
      if (url === '/api/sessions') { creates++; return new Promise(resolve => {finish = resolve;}); }
      if (url.endsWith('/messages')) { submits++; bodies.push(options.body); if (submits === 1) throw new Error('提交断开'); return {run_id: 'r'}; }
      if (url === '/api/runs/r') return {id: 'r', session_id: 'new', status: 'queued', request_id: 'stable-request'};
    };
    const sending = sendMessage({preventDefault() {}});
    await sendMessage({preventDefault() {}});
    if (!$('#harness').disabled || !$('#send-button').disabled || creates !== 1) throw new Error('创建中重复发送');
    state.sessions.push({id: 'new', project_id: 'p', title: '轮询先发现的新会话', harness: 'codex', created_at: '2026-10-18T00:00:00Z'});
    finish({id: 'new', project_id: 'p', harness: 'codex', cwd: 'D:/one/repo', created_at: '2026-10-18T00:00:00Z'});
    await sending;
    if (state.sessions.filter(session => session.id === 'new').length !== 1) throw new Error('轮询和创建响应生成重复会话');
    if (!state.pendingBySession.has('new')) throw new Error('没有保留失败消息');
    await retryMessage();
    if (creates !== 1 || submits !== 2 || bodies[0] !== bodies[1]) throw new Error('重试改变会话或请求身份');
  })()`);
});

test('创建失败保留草稿；切换后迟到的创建结果不污染新会话', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    await createSession({preventDefault() {}});
    $('#message-input').value = '保留原文';
    api = async () => { throw new Error('创建失败'); };
    await sendMessage({preventDefault() {}});
    if (!state.draft || state.draft.creating || $('#message-input').value !== '保留原文') throw new Error('失败丢失草稿');
    let finish; api = () => new Promise(resolve => {finish = resolve;});
    globalThis.crypto = {randomUUID: () => 'stable-request'};
    let background;
    submitPending = async pending => { background = pending; };
    const sending = sendMessage({preventDefault() {}});
    selectProject('q'); $('#message-input').value = '另一个项目的草稿';
    finish({id: 'old-created', project_id: 'p', harness: 'codex', created_at: '2026-10-18T00:00:00Z'});
    await sending;
    if (state.context || state.draft.projectId !== 'q' || $('#message-input').value !== '另一个项目的草稿') throw new Error('旧创建污染新选择');
    if (background.sessionId !== 'old-created' || background.message !== '保留原文') throw new Error('已提交输入未继续发送');
  })()`);
});

test('可见页面每五秒刷新，隐藏时暂停请求，恢复焦点合并刷新', async () => {
  const env = sidebar();
  env.run(`globalThis.pollCalls = 0; loadSessions = async () => {pollCalls++;}; bindEvents(); startSidebarPolling();`);
  const first = env.run('state.sidebarPoll');
  await env.timers.get(first)();
  assert.equal(env.run('pollCalls'), 1);
  assert.notEqual(env.run('state.sidebarPoll'), first);
  env.document.hidden = true;
  await env.timers.get(env.run('state.sidebarPoll'))();
  assert.equal(env.run('pollCalls'), 1);
  env.document.hidden = false;
  env.document.emit('visibilitychange', {});
  env.window.listeners.focus[0]();
  const refresh = env.run('state.sidebarTimer');
  await env.timers.get(refresh)();
  assert.equal(env.run('pollCalls'), 2);
  assert.equal(env.run('state.sidebarTimer'), null);
});

function pagedSidebar(count, width = 1440) {
  const env = sidebar(width);
  env.run(`
    state.sessions = Array.from({length: ${count}}, (_, index) => ({
      id: 'root-' + index, project_id: 'p', parent_id: null, title: '用于验证省略显示的很长会话标题 ' + index,
      harness: 'codex', created_at: '2026-10-18T00:00:00Z', updated_at: '2026-10-18T00:00:00Z',
      activity_event_id: 100 - index,
    }));
    renderSessionList();
  `);
  return env;
}

function directSessionIds(container) {
  return container.children.filter(child => child.classList.contains('session-branch'))
    .map(child => child.querySelector('.session-button').dataset.sessionId);
}

for (const count of [0, 5, 6, 11]) {
  test(`${count} 个主会话默认最多显示五个，剩余数量准确且最近区不分页`, () => {
    const env = pagedSidebar(count);
    assert.equal(directSessionIds(env.get('#project-sessions-p')).length, Math.min(count, 5));
    const more = env.get('button[data-sidebar-key="more:project:p"]');
    assert.equal(Boolean(more), count > 5);
    if (more) assert.equal(more.textContent, `Show more (${count - 5})`);
    assert.equal(env.get('#session-list').querySelectorAll('.session-button').length, count);
    assert.equal(Boolean(env.get('#project-sessions-p').querySelector('.empty-hint')), count === 0);
  });
}

test('Show more 每次增加五个、末页焦点移到 Show less，收起恢复五个与控制按钮焦点', () => {
  const env = pagedSidebar(11);
  env.get('#sidebar').scrollTop = 48;
  let more = env.get('button[data-sidebar-key="more:project:p"]');
  more.focus(); more.emit('click');
  assert.equal(directSessionIds(env.get('#project-sessions-p')).length, 10);
  assert.equal(env.document.activeElement.dataset.sidebarKey, 'more:project:p');
  more = env.get('button[data-sidebar-key="more:project:p"]');
  assert.equal(more.textContent, 'Show more (1)');
  more.emit('click');
  assert.equal(directSessionIds(env.get('#project-sessions-p')).length, 11);
  assert.equal(env.document.activeElement.dataset.sidebarKey, 'less:project:p');
  env.get('button[data-sidebar-key="less:project:p"]').emit('click');
  assert.equal(directSessionIds(env.get('#project-sessions-p')).length, 5);
  assert.equal(env.document.activeElement.dataset.sidebarKey, 'more:project:p');
  assert.equal(env.get('#sidebar').scrollTop, 48);
});

test('项目、子会话和孙会话独立分页；刷新及折叠保留数量，重新加载恢复五个', async () => {
  const env = pagedSidebar(6);
  env.run(`
    for (const parent of ['root-0', 'child-0']) {
      for (let index = 0; index < 6; index++) state.sessions.push({
        ...state.sessions[0], id: (parent === 'root-0' ? 'child-' : 'grandchild-') + index,
        parent_id: parent, activity_event_id: 90 - index,
      });
    }
    renderSessionList();
  `);
  assert.equal(directSessionIds(env.get('#children-root-0')).length, 5);
  assert.equal(directSessionIds(env.get('#children-child-0')).length, 5);
  env.get('button[data-sidebar-key="more:children:root-0"]').emit('click');
  assert.equal(directSessionIds(env.get('#children-root-0')).length, 6);
  assert.equal(directSessionIds(env.get('#children-child-0')).length, 5);
  assert.equal(directSessionIds(env.get('#project-sessions-p')).length, 5);
  env.get('button[data-sidebar-key="more:children:child-0"]').emit('click');
  env.get('button[data-sidebar-key="children:child-0"]').emit('click');
  env.get('button[data-sidebar-key="project:p"]').emit('click');
  await env.run('(async () => { api = async () => state.sessions; await loadSessions(); })()');
  assert.equal(env.get('#project-sessions-p').hidden, true);
  assert.equal(env.get('#children-child-0').hidden, true);
  env.get('button[data-sidebar-key="project:p"]').emit('click');
  assert.equal(directSessionIds(env.get('#children-root-0')).length, 6);
  assert.equal(directSessionIds(env.get('#children-child-0')).length, 6);
  env.get('button[data-sidebar-key="less:children:root-0"]').emit('click');
  assert.equal(env.get('#children-child-0').hidden, true);
  const restored = pagedSidebar(6);
  restored.storage.set('agentsteam.sidebar.v1', env.storage.get('agentsteam.sidebar.v1'));
  restored.run('loadSidebarPreferences(); renderSessionList();');
  assert.equal(directSessionIds(restored.get('#project-sessions-p')).length, 5);
});

test('隐藏范围的选中后代与祖先作为额外节点保持排序，剩余数量排除已展示节点', async () => {
  const env = pagedSidebar(11);
  env.run(`
    for (let index = 0; index < 7; index++) state.sessions.push({
      ...state.sessions[0], id: 'child-' + index, parent_id: 'root-10', activity_event_id: 20 - index,
    });
    state.sessions.push({...state.sessions[0], id: 'selected', parent_id: 'child-6', activity_event_id: 1});
    state.context = {sessionId: 'selected', projectId: 'p'};
    renderSessionList();
  `);
  assert.deepEqual(directSessionIds(env.get('#project-sessions-p')), ['root-0', 'root-1', 'root-2', 'root-3', 'root-4', 'root-10']);
  assert.equal(env.get('button[data-sidebar-key="more:project:p"]').textContent, 'Show more (5)');
  assert.deepEqual(directSessionIds(env.get('#children-root-10')), ['child-0', 'child-1', 'child-2', 'child-3', 'child-4', 'child-6']);
  assert.equal(env.get('button[data-sidebar-key="more:children:root-10"]').textContent, 'Show more (1)');
  env.get('button[data-sidebar-key="more:project:p"]').emit('click');
  env.get('button[data-sidebar-key="less:project:p"]').emit('click');
  assert.equal(env.get('#children-child-6').querySelector('.selected').dataset.sessionId, 'selected');
  await env.run(`(async () => {
    state.collapsed = new Set(['section:projects', 'project:p', 'children:root-10', 'children:child-6']);
    selectSession = async id => { state.context = {sessionId: id, projectId: state.projectId}; renderSessionList(); };
    await openSidebarSession(state.sessions.find(session => session.id === 'selected'));
  })()`);
  assert.equal(env.get('#projects-content').hidden, false);
  assert.equal(env.get('#project-sessions-p').hidden, false);
  assert.equal(env.get('#children-root-10').hidden, false);
  assert.equal(env.get('#children-child-6').hidden, false);
  env.get('button[data-sidebar-key="children:child-6"]').emit('click');
  env.run('renderSessionList();');
  assert.equal(env.get('#children-child-6').hidden, true); // 自动刷新不取消手动折叠。
});

test('项目和最近分区可独立折叠，恢复偏好和焦点且不切换聊天', () => {
  const env = sidebar();
  env.run(`state.source = {close() {throw new Error('分区折叠不应断流');}};`);
  const toggle = env.get('button[data-sidebar-key="section:projects"]');
  assert.equal(toggle.type, 'button');
  assert.equal(toggle.getAttribute('aria-controls'), 'projects-content');
  toggle.focus(); toggle.emit('click');
  assert.equal(env.get('#projects-content').hidden, true);
  assert.equal(env.get('#session-list').hidden, false);
  assert.equal(env.document.activeElement.dataset.sidebarKey, 'section:projects');
  env.get('button[data-sidebar-key="section:recent"]').emit('click');
  const restored = sidebar();
  restored.storage.set('agentsteam.sidebar.v1', env.storage.get('agentsteam.sidebar.v1'));
  restored.run('loadSidebarPreferences(); renderSessionList();');
  assert.equal(restored.get('#projects-content').hidden, true);
  assert.equal(restored.get('#session-list').hidden, true);
  assert.equal(restored.get('button[data-sidebar-key="section:recent"]').getAttribute('aria-expanded'), 'false');
});

test('相对时间紧凑且保留完整时间提示，叶节点不再显示字符圆点', () => {
  const env = pagedSidebar(6);
  const now = Date.parse('2026-10-18T12:00:00Z');
  for (const [minutes, expected] of [[0, '1m'], [5, '5m'], [60, '1h'], [2880, '2d']]) {
    assert.equal(env.run(`formatSessionDate('${new Date(now - minutes * 60000).toISOString()}', ${now})`), expected);
  }
  const time = env.get('.session-button-meta');
  assert.match(time.title, /2026/);
  assert.match(time.title, /10/);
  for (const marker of env.document.querySelectorAll('.tree-leaf')) assert.equal(marker.textContent, '');
  assert.ok(env.get('.tree-toggle').querySelector('.sidebar-icon-chevron'));
});

test('窄屏测试数据保持完整标题提示、分页和多级会话可打开', async () => {
  const env = pagedSidebar(6, 390);
  const button = env.get('button[data-session-id="root-0"]');
  assert.ok(button.title.includes(button.querySelector('.session-button-title').textContent));
  env.get('button[data-sidebar-key="more:project:p"]').emit('click');
  await env.run(`(async () => {
    let selected; selectSession = async id => {selected = id;};
    await $('#project-list').querySelector('button[data-session-id="root-5"]').listeners.click[0]();
    if (selected !== 'root-5') throw new Error('窄屏项目会话无法打开');
  })()`);
});

test('分区新建按钮打开独立项目弹窗，刷新不破坏填写内容、焦点或当前会话', () => {
  const env = sidebar();
  env.run(`bindEvents(); state.context = {sessionId:'a',projectId:'p'}; state.source = {close() {throw new Error('打开弹窗不应断流');}};`);
  env.get('#message-input').value = '当前会话草稿';
  const opener = env.get('button[data-sidebar-key="add-project"]');
  assert.equal(opener.type, 'button');
  assert.equal(opener.getAttribute('aria-haspopup'), 'dialog');
  opener.focus(); opener.emit('click');
  assert.equal(env.get('#project-dialog').open, true);
  assert.equal(env.document.activeElement.id, 'project-path');
  env.get('#project-path').value = 'D:/work/repo';
  env.run('renderSessionList();');
  assert.equal(env.get('#project-dialog').open, true);
  assert.equal(env.get('#project-path').value, 'D:/work/repo');
  assert.equal(env.document.activeElement.id, 'project-path');
  env.get('#project-dialog-cancel').emit('click');
  assert.equal(env.get('#project-dialog').open, false);
  assert.equal(env.document.activeElement.dataset.sidebarKey, 'add-project');
  assert.equal(env.get('#message-input').value, '当前会话草稿');
  assert.equal(env.run('state.context.sessionId'), 'a');
  assert.equal(env.get('#project-details'), null);
});

test('项目行新建会话按钮复用草稿流程，绑定点击的项目并保持项目展开', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    state.collapsed.add('project:q'); renderSessionList();
    api = async () => {throw new Error('新建草稿不能请求接口');};
    await $('button[data-sidebar-key="new-session:q"]').listeners.click[0]({preventDefault() {}});
    if (state.projectId !== 'q' || state.draft.projectId !== 'q' || state.context) throw new Error('没有进入目标项目的草稿');
    if (state.collapsed.has('project:q') || $('#project-sessions-q').hidden) throw new Error('未展开项目');
    if (document.activeElement.id !== 'message-input') throw new Error('没有聚焦输入框');
    if ($('#agent-choice').hidden || $('#project-select').hidden) throw new Error('未复用新建会话输入区');
  })()`);
  const row = env.get('button[data-sidebar-key="new-session:q"]').parentElement;
  assert.equal(row.className, 'project-row');
  assert.equal(row.querySelector('.project-button').parentElement, row);
  assert.equal(row.querySelectorAll('button button').length, 0);
});

test('项目弹窗关闭入口、遮罩和键盘事件独立于侧栏抽屉', () => {
  const env = sidebar(390);
  env.run('bindEvents();');
  const dialog = env.get('#project-dialog');
  env.get('button[data-sidebar-key="add-project"]').emit('click');
  dialog.emit('click', { target: env.get('#project-path') });
  assert.equal(dialog.open, true);
  let stopped = false;
  dialog.emit('keydown', { key: 'Escape', stopPropagation() {stopped = true;} });
  assert.equal(stopped, true);
  env.get('#project-dialog-close').emit('click');
  assert.equal(dialog.open, false);
  env.get('button[data-sidebar-key="add-project"]').emit('click');
  dialog.emit('click');
  assert.equal(dialog.open, false);
});

test('项目弹窗复用路径创建接口，失败保留输入可重试，提交中禁止重复创建', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    bindEvents(); openProjectDialog();
    let calls = 0; api = async () => {calls++; throw new Error('路径不是 Git 根目录');};
    $('#project-path').value = '  ';
    await createProject({preventDefault() {}});
    if (calls || !$('#project-error').textContent) throw new Error('空路径发送了请求');
    $('#project-path').value = ' D:/work/repo ';
    await createProject({preventDefault() {}});
    if (!$('#project-dialog').open || $('#project-path').value !== ' D:/work/repo ' || $('#project-error').textContent !== '路径不是 Git 根目录') throw new Error('创建失败丢失表单');
    if ($('#project-create-submit').disabled || $('#project-path').disabled) throw new Error('失败后不能重试');
    let finish; const requests = [];
    api = (path, options) => {requests.push({path,body:JSON.parse(options.body)}); return new Promise(resolve => {finish = resolve;});};
    const creating = createProject({preventDefault() {}});
    await createProject({preventDefault() {}});
    if (requests.length !== 1 || !$('#project-create-submit').disabled || !$('#project-path').disabled) throw new Error('提交中未锁定');
    if (requests[0].path !== '/api/projects' || JSON.stringify(requests[0].body) !== JSON.stringify({path:'D:/work/repo'})) throw new Error('创建接口改变');
    loadProjects = async () => {state.projects.push({id:'new-project',name:'repo',path:'D:/work/repo'}); renderProjectOptions(); renderSessionList();};
    finish({id:'new-project'}); await creating;
    if ($('#project-dialog').open || $('#project-path').value || $('#project-create-submit').disabled) throw new Error('成功后弹窗未复位');
    if (state.projectId !== 'new-project' || state.draft.projectId !== 'new-project') throw new Error('没有选择新建项目');
  })()`);
});

test('拖入目录地址支持引号、中文空格和 file URL，不自动创建', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    bindEvents(); openProjectDialog();
    api = async () => { throw new Error('拖入完整地址不应请求创建'); };
    await dropProjectDirectory({preventDefault() {}, stopPropagation() {}, dataTransfer: {
      files: [], items: [], getData(type) { return type === 'text/plain' ? '"D:/项目 甲"' : ''; }
    }});
    if ($('#project-path').value !== 'D:/项目 甲' || !$('#project-dialog').open || $('#project-error').textContent) throw new Error('引号路径未正确填入');
    await dropProjectDirectory({preventDefault() {}, stopPropagation() {}, dataTransfer: {
      files: [], items: [], getData(type) { return type === 'text/uri-list' ? '# comment\\r\\nfile:///D:/%E9%A1%B9%E7%9B%AE%20%E7%94%B2' : ''; }
    }});
    if ($('#project-path').value !== 'D:/项目 甲') throw new Error('file URI 未正确解码');
    if (normalizeProjectPath('file://server/share/repo') !== '//server/share/repo') throw new Error('UNC 地址丢失');
  })()`);
});

test('浏览器拖入文件夹仅提供目录名时选择服务端路径，确认只填表不创建', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    bindEvents(); openProjectDialog();
    let requests = 0;
    api = async url => {
      if (!url.startsWith('/api/project-directories')) throw new Error('不应创建项目');
      requests++;
      return {path:'D:/准确目录/简历', parent:'D:/准确目录', roots:['D:/'], directories:[]};
    };
    await dropProjectDirectory({preventDefault() {}, stopPropagation() {}, dataTransfer: {
      files: [{name:'简历'}], items: [{kind:'file', webkitGetAsEntry: () => ({isDirectory:true, name:'简历'})}], getData() {return '';}
    }});
    if (requests !== 1 || $('#project-directory-browser').hidden || !$('#project-directory-notice').textContent.includes('简历')) throw new Error('未进入目录选择');
    if ($('#project-path').value) throw new Error('不能从目录名猜测地址');
    $('#project-directory-use').emit('click');
    if ($('#project-path').value !== 'D:/准确目录/简历' || !$('#project-directory-browser').hidden || requests !== 1) throw new Error('确认目录没有只填入表单');
  })()`);
});

test('拖入文件或多个目录不改原值，提交中禁止覆盖地址', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    $('#project-path').value = 'D:/原目录';
    const event = transfer => ({preventDefault() {}, stopPropagation() {}, dataTransfer: transfer});
    await dropProjectDirectory(event({files:[{name:'a.txt'}],items:[{kind:'file',webkitGetAsEntry:()=>({isFile:true})}]}));
    if (!$('#project-error').textContent.includes('不是文件')) throw new Error('文件没有被拒绝');
    await dropProjectDirectory(event({files:[{},{}],items:[]}));
    if (!$('#project-error').textContent.includes('一个')) throw new Error('多个目录没有被拒绝');
    state.projectCreating = true;
    await dropProjectDirectory(event({files:[], items:[], getData:()=> 'D:/新目录'}));
    if ($('#project-path').value !== 'D:/原目录') throw new Error('改变了原输入');
  })()`);
});

test('目录读取乱序和关闭后的响应不能覆盖新选择，失败可从主目录重试', async () => {
  const env = sidebar();
  await env.run(`(async () => {
    bindEvents(); openProjectDialog();
    let finish;
    api = () => new Promise(resolve => { finish = resolve; });
    const old = openProjectDirectoryBrowser('');
    api = async () => ({path:'D:/新目录', parent:'D:/', roots:['D:/'], directories:[]});
    await browseProjectDirectory('D:/新目录');
    finish({path:'D:/旧目录', parent:'D:/', roots:['D:/'], directories:[]});
    await old;
    if ($('#project-directory-current').textContent !== 'D:/新目录') throw new Error('旧响应覆盖新目录');
    api = async () => { throw new Error('没有权限读取该目录'); };
    await browseProjectDirectory('D:/禁止目录');
    if (!$('#project-directory-use').disabled || !$('#project-error').textContent.includes('没有权限')) throw new Error('错误目录仍然可选');
    api = async () => ({path:'D:/主目录', parent:'D:/', roots:['D:/'], directories:[]});
    await $('#project-directory-home').listeners.click[0]();
    if ($('#project-directory-current').textContent !== 'D:/主目录' || $('#project-directory-use').disabled || $('#project-error').textContent) throw new Error('不能从主目录恢复选择');
    api = () => new Promise(resolve => { finish = resolve; });
    const pending = browseProjectDirectory('D:/迟到目录');
    $('#project-dialog').close();
    finish({path:'D:/迟到目录', parent:'D:/', roots:[], directories:[]});
    await pending;
    if (!$('#project-directory-browser').hidden || $('#project-directory-current').textContent !== 'D:/主目录') throw new Error('关闭后迟到请求仍然生效');
  })()`);
});

test('窄屏项目行新建会话关闭抽屉并聚焦可用的输入框', () => {
  const env = sidebar(390);
  env.run('WorkbenchLayout.init();');
  env.get('#left-toggle').emit('click');
  assert.equal(env.get('#sidebar').hidden, false);
  env.get('button[data-sidebar-key="new-session:q"]').emit('click');
  assert.equal(env.get('#sidebar').hidden, true);
  assert.equal(env.document.activeElement.id, 'message-input');
  assert.equal(env.get('.conversation').inert, false);
  assert.equal(env.run('state.draft.projectId'), 'q');
});
