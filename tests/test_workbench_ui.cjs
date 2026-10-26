const assert = require('node:assert/strict');
const { test } = require('node:test');
const { environment } = require('./frontend_dom.cjs');

const preferences = (leftWidth, rightWidth, rightOpen = true) => ({ left: { isOpen: true, width: leftWidth }, right: { isOpen: rightOpen, width: rightWidth } });
const key = 'agentsteam.layout.v1';
const style = (env, name) => parseFloat(env.document.documentElement.style[`--${name}`]);
const click = (env, selector) => env.get(selector).emit('click');
const press = (env, selector, key) => env.get(selector).emit('keydown', { key });

test('新建项目只提交目录，创建成功后选择项目', async () => {
  const env = environment();
  env.get('#project-path').value = 'D:/demo';
  await env.run(`(async () => {
    let request; let selected;
    api = async (path, options) => { request = {path, body: JSON.parse(options.body)}; return {id: 'new-project'}; };
    loadProjects = async () => {};
    selectProject = id => { selected = id; };
    await createProject({preventDefault() {}});
    if (request.path !== '/api/projects' || Object.keys(request.body).join(',') !== 'path') throw new Error('创建参数错误');
    if (request.body.path !== 'D:/demo') throw new Error('目录丢失');
    if (selected !== 'new-project') throw new Error('未选择创建的项目');
    if ($('#project-error').textContent) throw new Error('出现无关的表单校验错误');
  })()`);
});

test('默认左开右关；独立开合、双侧收起和刷新恢复不触碰会话', () => {
  const env = environment();
  env.run('WorkbenchLayout.init(); state.source = {close() { throw new Error("布局不能关闭 SSE"); }};');
  env.get('#message-input').value = '尚未发送';
  assert.equal(style(env, 'left-width'), 300);
  assert.equal(style(env, 'right-width'), 0);
  click(env, '#right-toggle');
  assert.equal(style(env, 'right-width'), 320);
  click(env, '#left-toggle');
  assert.equal(style(env, 'left-width'), 0);
  click(env, '#right-toggle');
  assert.equal(style(env, 'right-width'), 0);
  assert.equal(env.get('#message-input').value, '尚未发送');
  assert.equal(env.run('state.generation'), 0);
  const restored = environment(1440, JSON.parse(env.storage.get(key)));
  restored.run('WorkbenchLayout.init()');
  assert.equal(restored.get('#sidebar').hidden, true);
  assert.equal(restored.get('#inspector').hidden, true);
});

test('拖拽与键盘按左右方向调整；指针取消还原宽度并清理选择锁', () => {
  const env = environment(1440, preferences(300, 320));
  env.run('WorkbenchLayout.init()');
  const left = env.get('#left-resizer');
  left.emit('pointerdown', { clientX: 300 });
  left.emit('pointermove', { clientX: 350 });
  assert.equal(style(env, 'left-width'), 350);
  assert.equal(env.document.body.classList.contains('resizing'), true);
  left.emit('pointerup');
  assert.equal(JSON.parse(env.storage.get(key)).left.width, 350);
  assert.equal(env.document.body.classList.contains('resizing'), false);
  press(env, '#left-resizer', 'ArrowRight');
  assert.equal(style(env, 'left-width'), 366);
  press(env, '#right-resizer', 'ArrowLeft');
  assert.equal(style(env, 'right-width'), 336);
  const right = env.get('#right-resizer');
  right.emit('pointerdown', { clientX: 1000 });
  right.emit('pointermove', { clientX: 1050 });
  assert.equal(style(env, 'right-width'), 286);
  right.emit('pointercancel');
  assert.equal(style(env, 'right-width'), 336);
  assert.equal(env.document.body.classList.contains('resizing'), false);
});

test('尺寸约束保留 480px 中间空间；缩窗先压右栏，恢复时不丢失偏好', () => {
  const env = environment(1600, preferences(440, 480));
  env.run('WorkbenchLayout.init()');
  env.resize(1300);
  assert.equal(style(env, 'left-width'), 440);
  assert.equal(style(env, 'right-width'), 368);
  env.resize(1200);
  assert.equal(style(env, 'left-width'), 428);
  assert.equal(style(env, 'right-width'), 280);
  assert.equal(1200 - style(env, 'left-width') - style(env, 'right-width') - 12, 480);
  env.resize(1600);
  assert.equal(style(env, 'left-width'), 440);
  assert.equal(style(env, 'right-width'), 480);
  const handle = env.get('#left-resizer');
  handle.emit('pointerdown', { clientX: 440 });
  handle.emit('pointermove', { clientX: -1000 });
  handle.emit('pointerup');
  assert.equal(style(env, 'left-width'), 240);
});

test('1113px 右栏抽屉不挤压中间；Esc、遮罩、焦点循环和返回正常', () => {
  const env = environment(1113);
  env.run('WorkbenchLayout.init()');
  const trigger = env.get('#right-toggle');
  trigger.focus();
  click(env, '#right-toggle');
  assert.equal(style(env, 'right-width'), 0);
  assert.equal(style(env, 'left-width'), 300);
  assert.equal(env.get('#inspector').dataset.drawer, 'true');
  assert.equal(env.get('#inspector').getAttribute('aria-modal'), 'true');
  assert.equal(env.get('.conversation').inert, true);
  assert.equal(env.document.activeElement, env.get('#right-close'));
  const tab = { key: 'Tab', preventDefault() { this.prevented = true; } };
  env.document.emit('keydown', tab);
  assert.equal(tab.prevented, true);
  env.document.emit('keydown', { key: 'Escape', preventDefault() {} });
  assert.equal(env.get('#inspector').hidden, true);
  assert.equal(env.document.activeElement, trigger);
  assert.equal(env.get('.conversation').inert, false);
  click(env, '#right-toggle');
  click(env, '#panel-backdrop');
  assert.equal(env.get('#inspector').hidden, true);
});

test('手机两侧初始关闭、互斥展开；回到桌面恢复原有开合和宽度', () => {
  const env = environment(390, preferences(410, 450));
  env.run('WorkbenchLayout.init()');
  assert.equal(env.get('#sidebar').hidden, true);
  assert.equal(env.get('#inspector').hidden, true);
  click(env, '#left-toggle');
  assert.equal(env.get('#sidebar').hidden, false);
  click(env, '#right-toggle');
  assert.equal(env.get('#sidebar').hidden, true);
  assert.equal(env.get('#inspector').hidden, false);
  assert.equal(env.get('#left-resizer').hidden, true);
  env.resize(1600);
  assert.equal(style(env, 'left-width'), 410);
  assert.equal(style(env, 'right-width'), 450);
});

test('中屏右抽屉刷新保留开合；手机切换抽屉后返回新的触发按钮', () => {
  const env = environment(1113);
  env.run('WorkbenchLayout.init()');
  click(env, '#right-toggle');
  const restored = environment(1113, JSON.parse(env.storage.get(key)));
  restored.run('WorkbenchLayout.init()');
  assert.equal(restored.get('#inspector').hidden, false);
  restored.resize(390);
  click(restored, '#left-toggle');
  click(restored, '#right-toggle');
  restored.document.emit('keydown', { key: 'Escape', preventDefault() {} });
  assert.equal(restored.document.activeElement, restored.get('#right-toggle'));
});

test('损坏或禁用存储不阻断布局初始化和操作', () => {
  const env = environment();
  env.storage.set(key, '{invalid');
  env.run('WorkbenchLayout.init(); localStorage.setItem = () => { throw new Error("disabled"); };');
  click(env, '#left-toggle');
  assert.equal(env.get('#sidebar').hidden, true);
  const invalid = environment(1440, { left: { isOpen: true, width: 'bad' }, right: { isOpen: true, width: 9999 } });
  invalid.run('WorkbenchLayout.init()');
  assert.equal(style(invalid, 'left-width'), 300);
  assert.equal(style(invalid, 'right-width'), 480);
});

function conversation() {
  const env = environment();
  env.run(`state.context = {generation: 0, projectId: 'p', sessionId: 's', lastEventId: 0};
    refreshRun = async () => {}; refreshSessionNativeId = async () => {};
    state.runRecords.set('r', {id: 'r', session_id: 's', status: 'running'});`);
  let id = 0;
  env.event = (kind, payload, runId = 'r') => env.run(`applyEvent(${JSON.stringify({ id: ++id, kind, payload, run_id: runId, session_id: 's', project_id: 'p' })}, state.context)`);
  return env;
}

test('Markdown 渲染标题、列表、引用、代码、表格和安全外链', () => {
  const env = conversation();
  env.event('agent.text', { channel: 'message', text: '# 标题\n\n- 条目\n\n> 引用\n\n`代码`\n\n```js\nconst x = 1;\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n[链接](https://example.com)' });
  env.flushFrames();
  const html = env.get('.agent-text').innerHTML;
  for (const value of ['<h1>', '<ul>', '<blockquote>', '<code>', '<pre>', '<div class="table-scroll"><table>', 'target="_blank"', 'rel="noopener noreferrer"']) assert.ok(html.includes(value), value);
});

test('消息不能注入 HTML、图片、脚本或文件协议；用户消息保持纯文本', () => {
  const env = conversation();
  const text = '<img src=x onerror=alert(1)>\n\n[危险](javascript:alert(1)) [文件](file:///D:/a) [数据](data:text/html,a) ![图片](https://example.com/a.png)';
  env.event('user.message', { message: text });
  env.event('agent.text', { channel: 'message', text });
  env.flushFrames();
  assert.equal(env.get('.user-message').textContent, text);
  const html = env.get('.agent-text').innerHTML;
  assert.ok(html.includes('&lt;img'));
  assert.doesNotMatch(html, /<img|<script|href="(?:javascript|file|data):/);
});

test('流式同块每帧只渲染一次，工具前后正文分块且历史重放一致', () => {
  const env = conversation();
  for (const text of ['# ', '完整', '标题']) env.event('agent.text', { channel: 'message', text });
  assert.equal(env.run('state.dirtyText.size'), 1);
  assert.equal(env.run('state.textBlocks.size'), 1);
  assert.equal(env.frames.size, 1); // 正文等待 100ms，滚动仍按帧更新。
  env.event('agent.tool', { toolCallId: 'tool-a', title: '读取文件', status: 'in_progress' });
  env.event('agent.text', { channel: 'message', text: '后续正文' });
  env.flushFrames();
  assert.equal(env.document.querySelectorAll('.agent-text').length, 2);
  const content = env.get('.work-items');
  assert.equal(content.children[1].tagName, 'details');
  assert.match(content.children[0].innerHTML, /<h1>完整标题/);
  const replay = conversation();
  replay.event('agent.text', { channel: 'message', text: '# 完整标题' });
  replay.flushFrames();
  assert.equal(replay.get('.agent-text').innerHTML, content.children[0].innerHTML);
});

test('思考默认折叠；流式更新和终态不重置用户展开，工具更新不重复卡片', () => {
  const env = conversation();
  env.event('agent.text', { channel: 'thought', text: '第一段' });
  const thought = env.get('.thought-block');
  assert.equal(thought.open, false);
  thought.open = true;
  env.event('agent.text', { channel: 'thought', text: '第二段' });
  env.event('agent.tool', { toolCallId: 'a', title: '读文件', status: 'in_progress' });
  const card = env.get('.tool-card');
  assert.equal(card.open, false);
  card.open = true;
  env.event('agent.tool', { toolCallId: 'a', status: 'completed', rawOutput: '结果' });
  env.event('run.status', { status: 'completed', stop_reason: 'end_turn' });
  assert.equal(env.document.querySelectorAll('.tool-card').length, 1);
  assert.equal(card.open, true);
  assert.equal(card.querySelector('h4').textContent, '读文件');
  assert.equal(card.querySelector('.tool-status').textContent, '已完成');
  assert.match(card.querySelector('pre').textContent, /结果/);
  card.open = false;
  env.event('agent.tool', { toolCallId: 'a', status: 'failed', rawOutput: '<失败详情>' });
  assert.equal(card.open, false);
  assert.equal(card.querySelector('.tool-status').textContent, '失败');
  assert.equal(card.querySelector('pre'), null);
  card.open = true; card.emit('toggle');
  assert.match(card.querySelector('pre').textContent, /<失败详情>/);
  assert.equal(thought.open, true);
  assert.equal(thought.querySelector('summary').textContent, '思考过程');
  assert.match(thought.querySelector('.agent-text').innerHTML, /第一段第二段/);
  assert.equal(env.get('.run-header').hidden, true);
});

test('工具摘要缩短命令和图片路径，完整标题保留在详情，更新不丢失类型', () => {
  const env = conversation();
  const command = "@' from pathlib import Path; path = Path('styles.css'); text = path.read_text(encoding='utf-8')";
  env.event('agent.tool', { toolCallId: 'command', kind: 'execute', title: command, status: 'in_progress' });
  const card = env.get('.tool-card');
  assert.equal(card.querySelector('h4').textContent, '执行命令');
  assert.equal(card.querySelector('pre'), null);
  card.open = true; card.emit('toggle');
  assert.equal(JSON.parse(card.querySelector('pre').textContent).title, command);
  card.open = true;
  env.event('agent.tool', { toolCallId: 'command', status: 'failed' });
  assert.equal(card.querySelector('h4').textContent, '执行命令');
  assert.equal(card.querySelector('.tool-status').textContent, '失败');
  assert.equal(card.open, true);
  assert.equal(env.run(`toolSummary({title: 'Get-ChildItem -LiteralPath C:/Users/example/AppData/Local/ms-playwright'})`), '执行命令');
  assert.equal(env.run(`toolSummary({title: 'View Image C:/Users/example/AppData/Local/Temp/cooling-hero-desktop.png'})`), '查看图片');
  assert.equal(env.run(`toolSummary({title: 'Guardian Review'})`), 'Guardian Review');
  assert.equal(env.run(`toolSummary({kind: 'read', toolCallId: 'long-internal-id'})`), '读取文件');
  const longTitle = '自定义工具详细名称'.repeat(8);
  const summary = env.run(`toolSummary(${JSON.stringify({ title: longTitle })})`);
  assert.equal(Array.from(summary).length, 32);
  assert.ok(summary.endsWith('…'));
});

test('连续同类调用合成一行计数，组与单项展开及滚动位置在更新后保留', () => {
  const env = conversation();
  const command = (id, status = 'completed') => env.event('agent.tool', { toolCallId: id, kind: 'execute', title: `node ${id}.js`, status });
  command('a');
  assert.equal(env.get('.tool-group'), null);
  const first = env.get('.tool-card');
  first.open = true;
  command('b', 'failed');
  const group = env.get('.tool-group');
  assert.equal(group.open, false);
  assert.equal(group.querySelector('h4').textContent, '执行命令');
  assert.equal(group.querySelector('.tool-group-count').textContent, '· 2 次');
  assert.equal(group.querySelector('.tool-status').textContent, '1 次失败');
  assert.equal(group.querySelectorAll('.tool-card').length, 2);
  assert.equal(first.open, true);
  group.open = true;
  const items = group.querySelector('.tool-group-items');
  items.scrollTop = 40;
  command('c', 'in_progress');
  assert.equal(env.get('.tool-group'), group);
  assert.equal(group.open, true);
  assert.equal(items.scrollTop, 40);
  assert.equal(group.querySelector('.tool-group-count').textContent, '· 3 次');
  assert.equal(group.querySelector('.tool-status').textContent, '执行中 · 1 次失败');
  group.open = false;
  env.event('agent.tool', { toolCallId: 'c', status: 'completed' });
  assert.equal(group.open, false);
  assert.equal(group.querySelectorAll('.tool-card').length, 3);
  assert.equal(env.get('.work-items').children.length, 1);
});

test('正文、思考、不同工具、权限和回合隔开分组，旧工具更新不移动消息', () => {
  const env = conversation();
  const command = id => env.event('agent.tool', { toolCallId: id, kind: 'execute', title: 'node --check app.js', status: 'completed' });
  command('a'); command('b');
  env.event('agent.text', { channel: 'message', text: '检查结果' });
  command('c'); command('d');
  env.event('agent.text', { channel: 'thought', text: '下一步' });
  command('e');
  env.event('agent.tool', { toolCallId: 'read', kind: 'read', title: '读文件', status: 'completed' });
  command('f');
  env.event('permission.requested', { permission_id: 'p', status: 'pending', tool_call: { title: '写文件' }, options: [] });
  command('g');
  command('h');
  env.event('agent.tool', { toolCallId: 'other', kind: 'execute', status: 'completed' }, 'another-run');
  const content = env.get('.work-items');
  const order = Array.from(content.children);
  env.event('agent.tool', { toolCallId: 'a', status: 'failed' });
  assert.deepEqual(Array.from(content.children), order);
  assert.equal(env.document.querySelectorAll('.tool-group').length, 3);
  assert.equal(env.document.querySelectorAll('.tool-card').length, 11);
  assert.equal(env.get('.permission-card').parentElement, env.get('.run-alerts'));
  assert.match(env.get('.tool-group').querySelector('.tool-status').textContent, /1 次失败/);
});

test('历史和实时分组一致，晚到工具类型可重分组，未知状态不显示完成', () => {
  const rows = [
    { toolCallId: 'a', title: '检查' },
    { toolCallId: 'b', title: '检查' },
    { toolCallId: 'a', kind: 'execute', status: 'completed' },
    { toolCallId: 'b', kind: 'execute', status: 'completed' },
  ];
  const live = conversation();
  live.event('agent.tool', rows[0]); live.event('agent.tool', rows[1]);
  assert.equal(live.get('.tool-group').querySelector('.tool-status').textContent, '未知状态');
  live.event('agent.tool', rows[2]);
  assert.equal(live.get('.tool-group'), null);
  live.event('agent.tool', rows[3]);
  const replay = conversation();
  rows.forEach(row => replay.event('agent.tool', row));
  assert.equal(live.get('.tool-group').querySelector('h4').textContent, '执行命令');
  assert.equal(live.get('.tool-group').querySelector('.tool-group-count').textContent, '· 2 次');
  assert.equal(replay.get('.tool-group').querySelector('h4').textContent, live.get('.tool-group').querySelector('h4').textContent);
  assert.equal(replay.get('.tool-group').open, false);
  assert.equal(replay.get('.tool-group').querySelectorAll('.tool-card').length, 2);
});

test('计划更新复用清单，错误直接可见，Run ID 只显示在详情中', () => {
  const env = conversation();
  env.event('agent.plan', { entries: [{ content: '检查布局', status: 'in_progress' }] });
  env.event('agent.plan', { entries: [{ content: '检查布局', status: 'completed' }] });
  assert.equal(env.document.querySelectorAll('.plan-card').length, 1);
  assert.match(env.get('.plan-list').children[0].textContent, /已完成/);
  env.event('error', { error: { message: '执行失败示例' } });
  assert.match(env.get('.event-error').querySelector('p').textContent, /执行失败示例/);
  assert.equal(env.get('.run').querySelector('.run-id'), null);
  env.event('run.status', { status: 'failed', error: '命令失败' });
  assert.match(env.get('.run-error').textContent, /命令失败/);
  assert.equal(env.get('#execution-status').textContent, '失败');
});

test('右栏关闭时待授权可见；停止按钮指定运行中的 Run，保留取消竞争结果', async () => {
  const env = conversation();
  env.run(`bindEvents(); state.runRecords.set('queued', {id: 'queued', status: 'queued'}); renderRunList();
    applyPermission({id: 'permission', run_id: 'r', status: 'pending', payload: {tool_call: {title: '写文件'}, options: []}}, state.context);`);
  assert.equal(env.get('#execution-status').textContent, '待授权 1');
  assert.equal(env.get('#stop-button').hidden, false);
  await env.run(`(async () => {
    let resolve; let endpoint; api = url => { endpoint = url; return new Promise(done => {resolve = done;}); };
    const cancelling = cancelRun(currentExecution().id);
    if (endpoint !== '/api/runs/r/cancel') throw new Error('停止了错误回合');
    if (!$('#stop-button').disabled || $('#stop-button').textContent !== '取消中') throw new Error('未展示取消中');
    resolve({id: 'r', session_id: 's', status: 'completed'}); await cancelling;
    if (state.runRecords.get('r').status !== 'completed') throw new Error('覆盖服务端结果');
  })()`);
});

test('向上阅读不被流式更新拉走，回到底部恢复跟随，切换会话清理待绘制块', () => {
  const env = conversation();
  env.run('bindEvents()');
  env.event('agent.text', { channel: 'message', text: '消息' });
  env.flushFrames();
  const timeline = env.get('#timeline');
  assert.equal(timeline.scrollTop, 1200);
  timeline.scrollTop = 100;
  timeline.emit('scroll');
  env.event('agent.text', { channel: 'message', text: '增量' });
  env.flushFrames();
  assert.equal(timeline.scrollTop, 100);
  assert.equal(env.get('#scroll-bottom').hidden, false);
  click(env, '#scroll-bottom');
  env.flushFrames();
  assert.equal(timeline.scrollTop, 1200);
  env.event('agent.text', { channel: 'message', text: '未绘制增量' });
  env.run('clearConversationDom()');
  env.flushFrames();
  assert.equal(env.document.querySelectorAll('.agent-text').length, 0);
  assert.equal(env.run('state.textBlocks.size'), 0);
});

test('页面初始化恢复项目、会话、历史 Markdown 和独立 SSE 状态', async () => {
  const env = environment();
  env.storage.set('agentsteam.selection', JSON.stringify({ projectId: 'p', sessionId: 's' }));
  await env.run(`(async () => {
    globalThis.EventSource = class {
      constructor(url) { this.url = url; this.listeners = {}; }
      addEventListener(name, handler) { this.listeners[name] = handler; }
      close() {}
    };
    const session = {id: 's', project_id: 'p', harness: 'opencode', cwd: 'D:/demo', native_id: 'native-s', created_at: '2026-10-17T00:00:00Z'};
    const run = {id: 'r', session_id: 's', status: 'completed'};
    api = async path => {
      if (path === '/api/projects') return [{id: 'p', name: '演示项目', path: 'D:/demo'}];
      if (path === '/api/sessions') return [session];
      if (path === '/api/sessions/s?summary=true') return {session, runs: [run], interactions: []};
      if (path === '/api/sessions/s/history') return {runs: [run], active_runs: [], high_watermark: 2, before: null, metadata: []};
      if (path === '/api/runs/r?summary=true') return run;
      if (path.startsWith('/api/events?')) {
        if (new URLSearchParams(path.split('?')[1]).get('after_event_id') !== '0') return [];
        return [
          {id: 1, project_id: 'p', session_id: 's', run_id: 'r', kind: 'session.model', payload: {model_id: 'actual-model'}},
          {id: 2, project_id: 'p', session_id: 's', run_id: 'r', kind: 'agent.text', payload: {channel: 'message', text: '**历史消息**'}},
        ];
      }
      throw new Error('意外接口：' + path);
    };
    await init();
  })()`);
  env.flushFrames();
  assert.equal(env.get('#project-title').textContent, '演示项目');
  assert.equal(env.get('#composer-agent').textContent, 'OpenCode actual-model');
  assert.equal(env.get('#session-cwd').textContent, 'D:/demo');
  assert.equal(env.get('#send-button').disabled, false);
  assert.match(env.get('.agent-text').innerHTML, /<strong>历史消息<\/strong>/);
  assert.equal(env.get('#execution-status').textContent, '已完成');
  assert.equal(env.run('state.context.lastEventId'), 2);
  assert.match(env.run('state.source.url'), /after_event_id=2/);
  assert.equal(env.get('#stream-status').textContent, '正在连接实时事件…');
  env.run('state.source.listeners.open()');
  assert.equal(env.get('#stream-status').textContent, '实时已连接');
});

for (const [harness, label] of [['codebuddy', 'CodeBuddy'], ['deepseek', 'DeepSeek']]) {
  test(`${label} 会话可选择，输入区与详情显示 Agent 名称`, async () => {
    const env = conversation();
    await env.run(`(async () => {
      state.projects = [{id: 'p', name: '演示项目', path: 'D:/demo'}];
      state.projectId = 'p';
      state.sessions = [{id: 's', project_id: 'p', title: '调研智能体', harness: '${harness}', created_at: '2026-10-17T00:00:00Z'}];
      renderSessionList();
      renderSessionDetail({id: 's', title: '调研智能体', harness: '${harness}', cwd: 'D:/demo', native_id: '${harness}-native'});
    })()`);
    assert.equal(env.get('#composer-agent').textContent, label);
    assert.equal(env.get('#session-title').textContent, '调研智能体');
    assert.equal(env.get('.session-button-title').textContent, '调研智能体');
  });
}

test('输入区显示实际模型，元数据不进入聊天；切换会话和迟到事件不串模型', () => {
  const env = conversation();
  env.run("renderSessionDetail({id: 's', harness: 'codex'});");
  assert.equal(env.get('#composer-agent').textContent, 'Codex');
  env.event('session.model', { model_id: 'gpt-6.1-sol' });
  assert.equal(env.get('#composer-agent').textContent, 'Codex gpt-6.1-sol');
  assert.equal(env.get('#composer-agent').title, 'Codex gpt-6.1-sol');
  assert.equal(env.document.querySelectorAll('.run').length, 0);
  env.event('session.model', { model_id: 'changed-model' });
  assert.equal(env.get('#composer-agent').textContent, 'Codex changed-model');
  env.run(`globalThis.oldContext = state.context; resetConversation();
    state.context = {generation: state.generation, projectId: 'p', sessionId: 'other', lastEventId: 0};
    renderSessionDetail({id: 'other', harness: 'codebuddy'});
    applyEvent({id: 100, project_id: 'p', session_id: 's', kind: 'session.model', payload: {model_id: 'stale'}}, oldContext);
    applyEvent({id: 101, project_id: 'p', session_id: 's', kind: 'session.model', payload: {model_id: 'wrong-session'}}, state.context);`);
  assert.equal(env.get('#composer-agent').textContent, 'CodeBuddy');
  env.run(`applyEvent({id: 1, project_id: 'p', session_id: 'other', kind: 'session.model', payload: {model_id: 'buddy-model'}}, state.context);`);
  assert.equal(env.get('#composer-agent').textContent, 'CodeBuddy buddy-model');
});
