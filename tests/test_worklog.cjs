const assert = require('node:assert/strict');
const { test } = require('node:test');
const { environment } = require('./frontend_dom.cjs');

function conversation() {
  const env = environment();
  env.run(`
    globalThis.now = Date.parse('2026-10-18T00:00:00Z');
    Date.now = () => now;
    state.context = {generation: 0, projectId: 'p', sessionId: 's', lastEventId: 0, ready: true};
    refreshSessionNativeId = async () => {};
    api = async () => { throw new Error('离线'); };
    globalThis.sequence = 0;
    globalThis.event = (kind, payload, runId = 'r') => applyEvent({
      id: ++sequence, project_id: 'p', session_id: 's', run_id: runId,
      kind, payload, created_at: new Date(now).toISOString(),
    }, state.context);
  `);
  return env;
}

test('发送第一时刻显示用户消息和计时；HTTP 响应复用同一轮，不等待首个 token', async () => {
  const env = conversation();
  env.get('#message-input').value = '检查工程';
  env.run(`
    globalThis.crypto = {randomUUID: () => 'request-1'};
    api = async (url) => {
      if (url.endsWith('/messages')) return new Promise(resolve => { globalThis.accept = resolve; });
      return {id: 'r', session_id: 's', request_id: 'request-1', status: 'queued', created_at: new Date(now).toISOString()};
    };
    globalThis.sending = sendMessage({preventDefault() {}});
  `);
  const run = env.get('.run');
  assert.equal(env.get('.user-message').textContent, '检查工程');
  assert.equal(env.get('.work-label').textContent, 'Worked for 0秒');
  assert.equal(env.get('.work-status').textContent, '发送中');
  env.run('now += 3000; tickWorkLogs();');
  assert.equal(env.get('.work-label').textContent, 'Worked for 3秒');
  await env.run(`accept({run_id: 'r', status: 'queued'}); sending;`);
  assert.equal(env.get('.run'), run);
  assert.equal(run.dataset.runId, 'r');
  assert.equal(env.document.querySelectorAll('.run').length, 1);
  assert.equal(env.get('.work-status').textContent, '排队中');
  assert.equal(env.get('#message-input').value, '');
});

test('SSE 先于发送响应到达时，按 request_id 合并占位与服务端回合', async () => {
  const env = conversation();
  env.run(`
    globalThis.pending = {projectId: 'p', sessionId: 's', message: '正文', requestId: 'same-id'};
    state.pendingBySession.set('s', pending);
    api = async url => url.endsWith('/messages')
      ? new Promise(resolve => { globalThis.accept = resolve; })
      : {id: 'r', request_id: 'same-id', session_id: 's', status: 'running'};
    globalThis.sending = submitPending(pending);
    event('user.message', {message: '正文'});
    event('agent.text', {channel: 'thought', text: '检查中'});
  `);
  await env.run('Promise.resolve()');
  assert.equal(env.document.querySelectorAll('.run').length, 1);
  assert.equal(env.document.querySelectorAll('.user-message').length, 1);
  await env.run(`accept({run_id: 'r'}); sending;`);
  assert.equal(env.document.querySelectorAll('.run').length, 1);
  env.flushFrames();
  assert.match(env.get('.thought-block .agent-text').innerHTML, /检查中/);
});

test('过程收纳全部中间文本、思考、工具和计划；结束后只将最后正文移到外面', () => {
  const env = conversation();
  env.run(`
    event('user.message', {message: '排查问题'});
    event('run.status', {status: 'running'});
    event('agent.text', {channel: 'message', text: '先检查配置'});
    event('agent.text', {channel: 'thought', text: '思考内容'});
    event('agent.tool', {toolCallId: 'a', kind: 'execute', status: 'in_progress'});
    event('agent.tool', {toolCallId: 'b', kind: 'execute', status: 'completed'});
    event('agent.plan', {entries: [{content: '验证', status: 'completed'}]});
    event('agent.text', {channel: 'message', text: '**最终结论**'});
    now += 346000;
    event('run.status', {status: 'completed'});
  `);
  env.flushFrames();
  assert.equal(env.get('.work-label').textContent, 'Worked for 5分钟 46秒');
  assert.equal(env.get('.work-log').open, false);
  assert.equal(env.get('.work-status').textContent, '');
  assert.match(env.get('.run-answer .agent-text').innerHTML, /<strong>最终结论<\/strong>/);
  const process = env.get('.work-items');
  assert.equal(process.children[0].className, 'agent-text text-block');
  assert.match(process.children[0].innerHTML, /先检查配置/);
  assert.ok(process.querySelector('.thought-block'));
  assert.ok(process.querySelector('.tool-group'));
  assert.ok(process.querySelector('.plan-card'));
  assert.equal(env.get('.user-message').parentElement, env.get('.run-content'));
  env.run('now += 10000; tickWorkLogs();');
  assert.equal(env.get('.work-label').textContent, 'Worked for 5分钟 46秒');
});

test('手动收起和展开跨流式增量、工具更新、终态保持；最终回答始终在外', () => {
  const env = conversation();
  env.run(`event('agent.text', {channel: 'thought', text: '分析'});`);
  const work = env.get('.work-log');
  env.get('.work-summary').emit('click');
  assert.equal(work.open, false);
  env.run(`event('agent.tool', {toolCallId: 'a', status: 'in_progress'});`);
  assert.equal(work.open, false);
  env.get('.work-summary').emit('click');
  const tool = env.get('.tool-card');
  tool.open = true;
  env.run(`
    event('agent.text', {channel: 'message', text: '结果'});
    event('agent.tool', {toolCallId: 'a', status: 'completed'});
    event('run.status', {status: 'completed'});
  `);
  assert.equal(work.open, true);
  assert.equal(tool.open, true);
  assert.equal(env.get('.run-answer').hidden, false);
  env.get('.work-summary').emit('click');
  env.run(`event('agent.tool', {toolCallId: 'a', status: 'completed', rawOutput: '晚到输出'});`);
  assert.equal(work.open, false);
  assert.equal(env.get('.run-answer').hidden, false);
});

test('历史回放按 Run 时间恢复总耗时和末尾回答，旧排队事件不能重启计时', () => {
  const env = conversation();
  env.run(`
    state.runRecords.set('r', {id: 'r', status: 'completed',
      created_at: '2026-10-17T23:58:51Z', finished_at: '2026-10-18T00:00:00Z'});
    event('run.status', {status: 'queued'});
    event('agent.text', {channel: 'message', text: '中间说明'});
    event('agent.tool', {toolCallId: 'a', status: 'completed'});
    event('agent.text', {channel: 'message', text: '最终回答'});
  `);
  env.flushFrames();
  assert.equal(env.get('.work-label').textContent, 'Worked for 1分钟 9秒');
  assert.equal(env.get('.work-log').open, false);
  assert.match(env.get('.run-answer .agent-text').innerHTML, /最终回答/);
  assert.match(env.get('.work-items .agent-text').innerHTML, /中间说明/);
  assert.equal(env.run('state.workTimer'), null);
});

test('纯文本回答不被隐藏；取消且没有最终回答时保持过程可见', () => {
  const env = conversation();
  env.run(`event('agent.text', {channel: 'message', text: '直接回答'}); event('run.status', {status: 'completed'});`);
  env.flushFrames();
  assert.match(env.get('.run-answer .agent-text').innerHTML, /直接回答/);
  assert.equal(env.get('.work-items').children.length, 0);
  env.run(`event('agent.tool', {toolCallId: 'a', status: 'in_progress'}, 'canceled');
    event('run.status', {status: 'canceled'}, 'canceled');`);
  const canceled = env.run(`state.runs.get('canceled')`);
  assert.equal(canceled.querySelector('.work-log').open, true);
  assert.equal(canceled.querySelector('.work-status').textContent, '已取消');
});

test('收起过程后授权仍能操作，回答后归回过程；错误不被过程折叠隐藏', async () => {
  const env = conversation();
  env.run(`event('run.status', {status: 'waiting_permission'});
    event('permission.requested', {permission_id: 'permission', status: 'pending',
      tool_call: {title: '写文件'}, options: [{optionId: 'allow', name: '允许', kind: 'allow_once'}]});`);
  env.get('.work-summary').emit('click');
  assert.equal(env.get('.permission-card').parentElement, env.get('.run-alerts'));
  assert.equal(env.get('.run-alerts').hidden, false);
  assert.equal(env.get('.work-status').textContent, '等待权限');
  await env.run(`api = async () => ({id: 'permission', run_id: 'r', status: 'answered', payload: '{}'});
    answerPermission('permission', 'allow', state.context);`);
  assert.equal(env.get('.permission-card').parentElement.className, 'permission-slot');
  assert.equal(env.get('.run-alerts').hidden, true);
  env.run(`event('error', {error: '具体错误'}); event('run.status', {status: 'failed', error: '执行失败'});`);
  assert.equal(env.get('.work-log').open, false);
  assert.equal(env.get('.event-error').parentElement, env.get('.run-alerts'));
  assert.equal(env.get('.run-alerts').hidden, false);
  assert.equal(env.get('.run-header').hidden, false);
});

test('失败重试沿用原始计时与 request_id；服务端已接收时可由详情消除占位', async () => {
  const env = conversation();
  await env.run(`globalThis.pending = {sessionId: 's', projectId: 'p', message: '正文', requestId: 'stable'};
    state.pendingBySession.set('s', pending); submitPending(pending);`);
  assert.equal(env.get('.work-status').textContent, '发送未确认');
  const startedAt = env.run('pending.startedAt');
  await env.run(`
    now += 4000;
    api = async () => ({id: 'r', run_id: 'r', session_id: 's', request_id: 'stable', status: 'running'});
    retryMessage();
  `);
  assert.equal(env.document.querySelectorAll('.run').length, 1);
  assert.equal(env.run('pending.startedAt'), startedAt);
  assert.equal(env.get('.work-label').textContent, 'Worked for 4秒');
  assert.equal(env.get('.work-status').textContent, '运行中');
});

test('放弃本地重试后收到服务端回合，仍按请求身份消除占位，不重复显示', async () => {
  const env = conversation();
  await env.run(`globalThis.pending = {sessionId: 's', projectId: 'p', message: '正文', requestId: 'stable'};
    state.pendingBySession.set('s', pending); submitPending(pending);`);
  env.run('discardPending();');
  assert.equal(env.get('.work-status').textContent, '已放弃本地重试');
  assert.equal(env.timers.size, 0);
  env.run(`api = async () => ({id: 'r', session_id: 's', request_id: 'stable', status: 'running'});
    event('user.message', {message: '正文'});`);
  await env.run('Promise.resolve()');
  assert.equal(env.document.querySelectorAll('.run').length, 1);
  assert.equal(env.document.querySelectorAll('.user-message').length, 1);
  assert.equal(env.get('.work-status').textContent, '运行中');
});

test('所有回合结束后清理计时器；刷新详情不会重新开始累计耗时', () => {
  const env = conversation();
  env.run(`event('run.status', {status: 'running'}); now += 12000;
    event('run.status', {status: 'completed'});`);
  assert.equal(env.timers.size, 1); // 仅侧栏合并刷新，工作计时已停止。
  assert.equal(env.run('state.workTimer'), null);
  assert.equal(env.get('.work-label').textContent, 'Worked for 12秒');
  env.run(`now += 30000; updateRunHeader(state.runs.get('r'), state.runRecords.get('r'));`);
  assert.equal(env.run('state.workTimer'), null);
  assert.equal(env.get('.work-label').textContent, 'Worked for 12秒');
});

test('错误隔开前后工具与正文，末尾的错误不遮住已生成的最终回答', () => {
  const env = conversation();
  env.run(`
    event('agent.tool', {toolCallId: 'a', kind: 'execute', status: 'completed'});
    event('agent.tool', {toolCallId: 'b', kind: 'execute', status: 'completed'});
    event('error', {error: '工具错误'});
    event('agent.tool', {toolCallId: 'c', kind: 'execute', status: 'completed'});
    event('agent.text', {channel: 'message', text: '当前结论'});
    event('error', {error: '连接中断'});
    event('run.status', {status: 'failed'});
  `);
  env.flushFrames();
  assert.equal(env.get('.tool-group').querySelectorAll('.tool-card').length, 2);
  assert.equal(env.get('.work-items').querySelectorAll('.tool-card').length, 3);
  assert.match(env.get('.run-answer .agent-text').innerHTML, /当前结论/);
  assert.equal(env.get('.run-alerts').querySelectorAll('.event-error').length, 2);
});

test('切换会话清理计时器；旧发送响应不能在新会话生成计时或消息', async () => {
  const env = conversation();
  env.run(`globalThis.pending = {sessionId: 's', projectId: 'p', message: '正文', requestId: 'stable'};
    api = () => new Promise(resolve => {globalThis.accept = resolve;});
    globalThis.sending = submitPending(pending);`);
  assert.equal(env.timers.size, 1);
  env.run(`clearConversationDom(); state.generation++;
    state.context = {generation: state.generation, projectId: 'p', sessionId: 'other', ready: true};`);
  assert.equal(env.timers.size, 0);
  await env.run(`accept({run_id: 'old-run'}); sending;`);
  assert.equal(env.document.querySelectorAll('.work-log').length, 0);
  assert.equal(env.run('state.runs.size'), 0);
});
