const assert = require('node:assert/strict');
const { test } = require('node:test');
const { environment } = require('./frontend_dom.cjs');

const cacheKey = 'agentsteam.models.v1';

test('未发送消息的会话优先显示配置模型，不误用同 Harness 其他会话模型', () => {
  const env = environment();
  select(env, 'other', 'codex'); confirm(env, 20, 'other-model');
  env.run(`renderSessionDetail({id:'new', project_id:'p', harness:'codex', configured_model:'configured-model'});`);
  assert.equal(env.get('#composer-agent').textContent, 'Codex configured-model');
  assert.equal(env.get('#composer-agent').hidden, false);
});

test('草稿配置在发送前显示，员工覆盖全局模型，迟到响应不覆盖新选择', async () => {
  const env = environment();
  await env.run(`(async()=>{
    state.draft={projectId:'p',harness:'codex',employeeId:'e'};
    api=async url=>url==='/api/settings/harnesses'?[{id:'codex',settings:{config_values:{model:'global'}}}]:{config_values:{model:'employee'}};
    await refreshDraftModel();
    if($('#composer-agent').hidden || !$('#composer-agent').textContent.includes('employee'))throw Error('未显示员工模型');
    let finish;api=()=>new Promise(resolve=>{finish=resolve;});
    state.draft={projectId:'p',harness:'codex'};
    const previous=refreshDraftModel();
    state.draft={projectId:'p',harness:'opencode'};
    api=async()=>[{id:'opencode',settings:{config_values:{model:'new'}}}];
    await refreshDraftModel();
    finish([{id:'codex',settings:{config_values:{model:'stale'}}}]);await previous;
  })()`);
  assert.equal(env.get('#composer-agent').textContent, 'OpenCode new');
});

function select(env, sessionId, harness = 'opencode', projectId = 'p') {
  env.run(`
    resetConversation();
    state.context = {generation: state.generation, projectId: ${JSON.stringify(projectId)},
      sessionId: ${JSON.stringify(sessionId)}, lastEventId: 0};
    renderSessionDetail({id: ${JSON.stringify(sessionId)}, harness: ${JSON.stringify(harness)},
      project_id: ${JSON.stringify(projectId)}});
  `);
}

function confirm(env, id, modelId) {
  env.run(`applyEvent({id: ${id}, project_id: state.context.projectId,
    session_id: state.context.sessionId, kind: 'session.model',
    payload: {model_id: ${JSON.stringify(modelId)}}}, state.context);`);
}

test('关闭后重新打开，在读取历史与连接 ACP 前显示已确认模型', () => {
  const original = environment();
  select(original, 's');
  confirm(original, 10, 'zerocat/glm-5.3');
  const reopened = environment();
  reopened.storage.set(cacheKey, original.storage.get(cacheKey));
  reopened.run(`api = () => {throw new Error('显示模型不能请求 ACP 或历史');};`);
  select(reopened, 's');
  assert.equal(reopened.get('#composer-agent').textContent, 'OpenCode zerocat/glm-5.3');
  assert.equal(reopened.document.querySelectorAll('.run').length, 0);
  assert.equal(reopened.run('state.context.lastEventId'), 0);
});

test('新会话沿用本项目同 Agent 最近确认模型，其他项目和 Agent 不串用', () => {
  const env = environment();
  select(env, 's');
  confirm(env, 10, 'zerocat/glm-5.3');
  select(env, 'new-session');
  assert.equal(env.get('#composer-agent').textContent, 'OpenCode zerocat/glm-5.3');
  select(env, 'codex-session', 'codex');
  assert.equal(env.get('#composer-agent').textContent, 'Codex 默认模型');
  select(env, 'another-project-session', 'opencode', 'other-project');
  assert.equal(env.get('#composer-agent').textContent, 'OpenCode 默认模型');
});

test('真实模型变化立即替换缓存；重放较旧记录不能使本会话模型回退', () => {
  const env = environment();
  select(env, 's', 'codex');
  confirm(env, 10, 'gpt-6.1-sol');
  confirm(env, 20, 'changed-model');
  select(env, 's', 'codex');
  assert.equal(env.get('#composer-agent').textContent, 'Codex changed-model');
  confirm(env, 10, 'gpt-6.1-sol');
  assert.equal(env.get('#composer-agent').textContent, 'Codex changed-model');
  confirm(env, 21, 'actual-new-model');
  const reopened = environment();
  reopened.storage.set(cacheKey, env.storage.get(cacheKey));
  select(reopened, 's', 'codex');
  assert.equal(reopened.get('#composer-agent').textContent, 'Codex actual-new-model');
});

test('各会话保留独立模型，打开旧会话不覆盖同 Agent 较新的默认缓存', () => {
  const env = environment();
  select(env, 'old-session');
  confirm(env, 10, 'old-model');
  select(env, 'recent-session');
  confirm(env, 20, 'recent-model');
  select(env, 'old-session');
  confirm(env, 10, 'old-model');
  assert.equal(env.get('#composer-agent').textContent, 'OpenCode old-model');
  select(env, 'new-session');
  assert.equal(env.get('#composer-agent').textContent, 'OpenCode recent-model');
});

test('旧上下文或其他会话的模型事件不污染当前显示和持久缓存', () => {
  const env = environment();
  select(env, 's');
  confirm(env, 10, 'confirmed-model');
  env.run('globalThis.previous = state.context;');
  select(env, 'other', 'codex');
  const saved = env.storage.get(cacheKey);
  env.run(`applyEvent({id: 30, project_id: 'p', session_id: 's', kind: 'session.model',
    payload: {model_id: 'stale'}}, previous);
    applyEvent({id: 31, project_id: 'p', session_id: 's', kind: 'session.model',
    payload: {model_id: 'wrong-session'}}, state.context);`);
  assert.equal(env.get('#composer-agent').textContent, 'Codex 默认模型');
  assert.equal(env.storage.get(cacheKey), saved);
});

test('损坏缓存或浏览器禁用存储不影响页面与实际模型更新', () => {
  const env = environment();
  for (const value of ['{broken', 'null', '42', '[]', '{"session:s":{"modelId":{"bad":true},"eventId":9,"harness":"opencode","projectId":"p"}}']) {
    env.storage.set(cacheKey, value);
    select(env, 's');
    assert.equal(env.get('#composer-agent').textContent, 'OpenCode 默认模型');
  }
  env.run(`localStorage.getItem = () => {throw new Error('disabled');};
    localStorage.setItem = () => {throw new Error('disabled');};`);
  select(env, 's');
  confirm(env, 10, 'actual-model');
  assert.equal(env.get('#composer-agent').textContent, 'OpenCode actual-model');
});
