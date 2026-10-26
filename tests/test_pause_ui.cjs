const assert = require('node:assert/strict');
const { test } = require('node:test');
const { environment } = require('./frontend_dom.cjs');

function conversation() {
  const env = environment();
  env.run(`
    state.context = {generation: 0, projectId: 'p', sessionId: 's', lastEventId: 0, ready: true};
    refreshRun = async () => {}; refreshSessionNativeId = async () => {}; refreshRunFileChanges = async () => {};
    globalThis.crypto = {randomUUID: () => 'request-1'};
    bindEvents(); updateMessageForm();
  `);
  let sequence = 0;
  env.event = (status, runId = 'r') => env.run(`applyEvent(${JSON.stringify({
    id: ++sequence, project_id: 'p', session_id: 's', run_id: runId,
    kind: 'run.status', payload: {status},
  })}, state.context)`);
  env.clickPause = () => env.get('#send-button').listeners.click[0]();
  return env;
}

function action(env, title, disabled = false) {
  const button = env.get('#send-button');
  const pausing = title !== '发送消息';
  assert.equal(button.title, title);
  assert.equal(button.getAttribute('aria-label'), title);
  assert.equal(button.type, pausing ? 'button' : 'submit');
  assert.equal(button.disabled, disabled);
  assert.equal(env.get('#send-icon').getAttribute('hidden') !== null, pausing);
  assert.equal(env.get('#pause-icon').getAttribute('hidden') !== null, !pausing);
}

test('发送按钮在排队、生成、等待权限时切换暂停，各种终态恢复发送', () => {
  const env = conversation();
  action(env, '发送消息');
  for (const status of ['queued', 'running', 'waiting_permission']) {
    env.event(status);
    action(env, '暂停生成');
  }
  env.event('cancel_requested');
  action(env, '暂停中', true);
  for (const status of ['completed', 'failed', 'canceled', 'interrupted']) {
    env.event(status);
    action(env, '发送消息');
  }
});

test('空输入也可暂停；重复点击只取消指定回合，保留输出并恢复发送', async () => {
  const env = conversation();
  env.event('running');
  env.run(`
    appendText({payload: {channel: 'message', text: '已经生成的内容'}}, state.runs.get('r'));
    globalThis.calls = [];
    api = (url, options) => { calls.push({url, method: options.method}); return new Promise(resolve => {globalThis.finish = resolve;}); };
  `);
  assert.equal(env.get('#message-input').value, '');
  const canceling = env.clickPause();
  action(env, '暂停中', true);
  await env.clickPause();
  assert.equal(env.run('calls.length'), 1);
  assert.equal(env.run('calls[0].url'), '/api/runs/r/cancel');
  assert.equal(env.run('calls[0].method'), 'POST');
  await env.run(`finish({id: 'r', session_id: 's', status: 'canceled'});`);
  await canceling;
  action(env, '发送消息');
  env.flushFrames();
  assert.match(env.get('.agent-text').innerHTML, /已经生成的内容/);
  assert.equal(env.get('#message-error').textContent, '');
});

test('暂停失败显示错误且恢复可重试状态', async () => {
  const env = conversation();
  env.event('running');
  env.run(`api = async () => {throw new Error('网络断开');};`);
  await env.clickPause();
  action(env, '暂停生成');
  assert.match(env.get('#message-error').textContent, /取消失败：网络断开/);
  env.run(`api = async () => ({id: 'r', session_id: 's', status: 'canceled'});`);
  await env.clickPause();
  action(env, '发送消息');
});

test('SSE 先确认完成时恢复发送，迟到取消响应不能回退终态', async () => {
  const env = conversation();
  env.event('running');
  env.run(`api = () => new Promise(resolve => {globalThis.finish = resolve;});`);
  const canceling = env.clickPause();
  env.event('completed');
  action(env, '发送消息');
  await env.run(`finish({id: 'r', session_id: 's', status: 'cancel_requested'});`);
  await canceling;
  action(env, '发送消息');
  assert.equal(env.run("state.runRecords.get('r').status"), 'completed');
});

test('同时存在排队回合时优先暂停正在执行的回合，不自动取消下一轮', async () => {
  const env = conversation();
  env.event('queued', 'next');
  env.event('waiting_permission');
  env.run(`globalThis.endpoint = null; api = async url => {
    endpoint = url; return {id: 'r', session_id: 's', status: 'completed'};
  };`);
  await env.clickPause();
  assert.equal(env.run('endpoint'), '/api/runs/r/cancel');
  assert.equal(env.run("state.runRecords.get('next').status"), 'queued');
  action(env, '暂停生成');
  env.run(`api = async url => {endpoint = url; return {id: 'next', session_id: 's', status: 'canceled'};};`);
  await env.clickPause();
  assert.equal(env.run('endpoint'), '/api/runs/next/cancel');
  action(env, '发送消息');
});

test('刷新后的快照与重放事件恢复暂停状态，旧运行事件不能覆盖终态', () => {
  const env = conversation();
  env.run(`
    state.context.ready = false;
    state.runRecords.set('r', {id: 'r', session_id: 's', status: 'waiting_permission'});
    renderRunList();
  `);
  action(env, '暂停生成');
  env.event('cancel_requested');
  action(env, '暂停中', true);
  env.run(`state.runRecords.set('r', {id: 'r', session_id: 's', status: 'canceled'}); renderRunList();`);
  env.event('running');
  action(env, '发送消息', true);
  env.run('state.context.ready = true; updateMessageForm();');
  action(env, '发送消息');
});

test('发送尚未取得回合 ID 时禁用，响应后无需等待详情即可暂停', async () => {
  const env = conversation();
  env.get('#message-input').value = '开始生成';
  env.run(`
    api = (url) => new Promise(resolve => {globalThis.accept = resolve;});
    refreshRun = () => new Promise(resolve => {globalThis.details = resolve;});
    globalThis.sending = sendMessage({preventDefault() {}});
  `);
  action(env, '发送消息', true);
  await env.run(`accept({run_id: 'r', status: 'queued'}); Promise.resolve();`);
  action(env, '暂停生成');
  assert.equal(env.get('#message-input').value, '');
  env.run(`api = async () => ({id: 'r', session_id: 's', status: 'canceled'});`);
  await env.clickPause();
  action(env, '发送消息');
  await env.run('details(); sending;');
});

test('切换会话后迟到取消结果和错误不影响当前按钮或内容', async () => {
  for (const fail of [false, true]) {
    const env = conversation();
    env.event('running');
    env.run(`api = () => new Promise((resolve, reject) => {globalThis.finish = resolve; globalThis.fail = reject;});`);
    const canceling = env.clickPause();
    env.run(`resetConversation(); state.context = {generation: state.generation, projectId: 'p', sessionId: 'other', ready: true}; updateMessageForm();`);
    action(env, '发送消息');
    env.get('#message-input').value = '新会话草稿';
    await env.run(fail ? "fail(new Error('旧会话失败'));" : "finish({id: 'r', session_id: 's', status: 'canceled'});");
    await canceling;
    action(env, '发送消息');
    assert.equal(env.get('#message-input').value, '新会话草稿');
    assert.equal(env.get('#message-error').textContent, '');
    assert.equal(env.run('state.runRecords.size'), 0);
  }
});

test('生成期间发送快捷键仍提交新消息，不触发暂停', async () => {
  const env = conversation();
  env.event('running');
  env.get('#message-input').value = '下一轮输入';
  env.run(`
    globalThis.urls = [];
    api = async url => {urls.push(url); return {run_id: 'next', status: 'queued'};};
    $('#message-form').requestSubmit = () => {globalThis.submitting = sendMessage({preventDefault() {}});};
  `);
  env.get('#message-input').emit('keydown', {key: 'Enter', ctrlKey: true});
  await env.run('submitting');
  assert.equal(env.run('urls.length'), 1);
  assert.equal(env.run('urls[0]'), '/api/sessions/s/messages');
  assert.equal(env.run("state.runRecords.get('r').status"), 'running');
  action(env, '暂停生成');
});
