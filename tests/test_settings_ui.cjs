const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { environment } = require('./frontend_dom.cjs');
const { addSkillsElements } = require('./skills_dom.cjs');

const ids = ['settings-error', 'harness-list', 'harness-detail', 'settings-agent-icon',
  'settings-title', 'settings-status', 'settings-cwd', 'harness-fields',
  'settings-save', 'settings-feedback', 'harness-settings-form'];

function setup(hash = '#settings') {
  const env = environment();
  const { document } = env;
  const shell = document.createElement('div');
  shell.className = 'app-shell';
  shell.append(...document.body.children);
  document.body.append(shell);
  const open = document.createElement('button');
  open.id = 'open-settings';
  shell.append(open);
  const view = document.createElement('div');
  view.id = 'settings-view';
  document.body.append(view);
  for (const id of ids) {
    const node = document.createElement(id === 'harness-settings-form' ? 'form' : 'div');
    node.id = id;
    view.append(node);
  }
  addSkillsElements(env);
  env.window.location = { hash };
  env.run(`
    globalThis.calls = [];
    globalThis.hold = false;
    globalThis.pending = [];
    globalThis.failProbe = false;
    globalThis.failList = false;
    globalThis.fetch = async (url, options) => {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, method: options.method, body });
      if (url === '/api/settings/harnesses' && failList) return { ok: false, json: async () => ({detail:'列表加载失败'}) };
      if (url === '/api/settings/harnesses') return { ok: true, json: async () => ['codex','deepseek'].map(id => ({
        id, name: id, enabled: true, installed: true, status: {state:'unverified'},
        settings: { config_values: {}, revision: 0 }
      })) };
      if (options.method === 'PUT') return { ok: true, json: async () => ({ config_values: body.config_values, revision: 1 }) };
      const data = { cwd: 'D:/test', invalid: body.config_values.model === 'fast' && body.config_values.effort === 'high'
        ? [{id:'effort',reason:'需重新选择'}] : [], fields: [
        { id:'model',name:'模型',supported:true,current_value:'m1',default_value:'m1',options:[{value:'m1',name:'Model 1'},{value:'fast',name:'Fast'}]},
        { id:'effort',name:'思考强度',supported:true,current_value:'high',default_value:'high',options:
          (body.config_values.model === 'fast' ? ['low'] : ['low','high']).map(value => ({value,name:value}))},
        { id:'sandbox',name:'权限',supported:false,description:'此 Harness 未提供此配置项',options:[]}
      ]};
      if (hold) await new Promise(resolve => pending.push(resolve));
      return { ok: !failProbe, json: async () => failProbe ? {detail:'握手失败'} : data };
    };
  `);
  env.run(fs.readFileSync(path.join(__dirname, '../app/static/settings.js'), 'utf8'));
  return env;
}

async function tick() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
async function navigate(env, hash) {
  env.window.location.hash = hash;
  for (const listener of env.window.listeners.hashchange) listener();
  await tick();
}
async function initialize(env) { env.run('HarnessSettingsUI.init(() => null)'); await tick(); }

test('进入和返回设置保留工作台、草稿及 SSE', async () => {
  const env = setup();
  env.get('#message-input').value = '未发送正文';
  env.run('state.source = { close() { throw new Error("不应关闭 SSE"); } }; state.context = { sessionId:"session-a" };');
  await initialize(env);
  assert.equal(env.get('.app-shell').hidden, true);
  assert.equal(env.get('#harness-list').children.length, 2);
  assert.equal(env.get('#harness-list').hidden, false);
  assert.equal(env.get('#harness-detail').hidden, false);
  assert.equal(env.get('#settings-title').textContent, 'codex');
  await navigate(env, '#workbench');
  assert.equal(env.get('.app-shell').hidden, false);
  assert.equal(env.get('#settings-view').hidden, true);
  assert.equal(env.get('#message-input').value, '未发送正文');
  assert.equal(env.run('state.context.sessionId'), 'session-a');
});

test('详情 hash 可恢复，原生下拉默认继承，不支持项禁用', async () => {
  const env = setup('#settings/deepseek');
  await initialize(env);
  assert.equal(env.get('#settings-title').textContent, 'deepseek');
  assert.equal(env.get('#setting-model').value, '');
  assert.equal(env.get('#setting-sandbox').disabled, true);
  assert.equal(env.get('#settings-save').disabled, false);
  assert.equal(env.run('calls.filter(call=>call.method==="POST").length'), 1);
  assert.equal(env.run('calls.some(call=>call.method==="PUT")'), false);
});

test('Skills 导航独立显示，跳转时不探测 Harness，返回保留配置草稿', async () => {
  const env = setup('#settings/codex');
  await initialize(env);
  env.get('#setting-effort').value = 'low';
  env.get('#setting-effort').emit('change');
  env.run(`globalThis.skillsShows=0; globalThis.skillsHides=0;
    HarnessSkillsUI.show=async()=>{skillsShows++}; HarnessSkillsUI.hide=()=>{skillsHides++};`);
  const probes = env.run('calls.filter(call=>call.method==="POST").length');
  await navigate(env, '#settings/skills');
  assert.equal(env.get('#settings-skills-panel').hidden, false);
  assert.equal(env.get('#settings-harness-panel').hidden, true);
  assert.equal(env.run('skillsShows'), 1);
  assert.equal(env.run('calls.filter(call=>call.method==="POST").length'), probes);
  assert.equal(env.get('#settings-skills-nav').getAttribute('aria-current'), 'page');
  await navigate(env, '#settings/codex');
  assert.equal(env.get('#settings-skills-panel').hidden, true);
  assert.equal(env.get('#setting-effort').value, 'low');
});

test('Skills 初始化失败显示错误，刷新重试 Harness 列表后恢复原 hash', async () => {
  const env = setup('#settings/skills?agent=deepseek');
  env.run('failList=true; globalThis.recoveredAgent=null; HarnessSkillsUI.show=async records=>{recoveredAgent=records[1].id};');
  await initialize(env);
  assert.equal(env.get('#settings-skills-panel').hidden, false);
  assert.match(env.get('#skills-error').textContent, /列表加载失败/);
  assert.equal(env.get('#skills-refresh').disabled, false);
  env.run('failList=false');
  env.get('#skills-refresh').click(); await tick();
  assert.equal(env.run('recoveredAgent'), 'deepseek');
  assert.equal(env.run("calls.filter(call=>call.url==='/api/settings/harnesses').length"), 2);
  assert.equal(env.window.location.hash, '#settings/skills?agent=deepseek');
  assert.equal(env.run('calls.some(call=>call.method==="POST")'), false);
});

test('MCP 初始化失败可重试，三页切换保留工作台及配置草稿且不额外探测', async () => {
  const env = setup('#settings/mcp?agent=deepseek');
  env.run(`failList=true; globalThis.mcpShows=0; HarnessMcpUI.show=async()=>{mcpShows++};
    HarnessSkillsUI.show=async()=>{}; state.source={close(){throw Error('不应关闭 SSE')}};`);
  env.get('#message-input').value='草稿';
  await initialize(env);
  assert.match(env.get('#mcp-error').textContent,/列表加载失败/);
  assert.equal(env.get('#settings-mcp-panel').hidden,false);
  env.run('failList=false'); env.get('#mcp-refresh').click(); await tick();
  assert.equal(env.run('mcpShows'),1);
  assert.equal(env.run('calls.some(call=>call.method==="POST")'),false);
  await navigate(env,'#settings/codex');
  env.get('#setting-effort').value='low'; env.get('#setting-effort').emit('change');
  const probes=env.run('calls.filter(call=>call.method==="POST").length');
  for (const hash of ['#settings/mcp','#settings/skills','#settings/codex']) await navigate(env,hash);
  assert.equal(env.run('calls.filter(call=>call.method==="POST").length'),probes);
  assert.equal(env.get('#setting-effort').value,'low');
  assert.equal(env.get('#message-input').value,'草稿');
});

test('切换模型重新获取思考强度，无效旧值阻止保存', async () => {
  const env = setup('#settings/codex');
  await initialize(env);
  env.get('#setting-effort').value = 'high';
  env.get('#setting-effort').emit('change');
  env.get('#setting-model').value = 'fast';
  env.get('#setting-model').emit('change');
  assert.equal(env.get('#settings-save').disabled, true);
  const callbacks = [...env.timers.values()];
  env.timers.clear();
  for (const callback of callbacks) callback();
  await tick();
  assert.equal(env.get('#settings-save').disabled, true);
  assert.equal(env.get('#setting-effort').value, 'high');
  env.get('#setting-effort').value = 'low';
  env.get('#setting-effort').emit('change');
  assert.equal(env.get('#settings-save').disabled, false);
  env.get('#harness-settings-form').emit('submit');
  await tick();
  assert.equal(env.run('calls.find(call=>call.method==="PUT").body.config_values.effort'), 'low');
  assert.match(env.get('#settings-feedback').textContent, /已保存/);
});

test('跨 Harness 和返回工作台保留未保存草稿', async () => {
  const env = setup('#settings/codex');
  await initialize(env);
  env.get('#setting-effort').value = 'low';
  env.get('#setting-effort').emit('change');
  await navigate(env, '#settings/deepseek');
  assert.equal(env.get('#setting-effort').value, '');
  await navigate(env, '#workbench');
  await navigate(env, '#settings/codex');
  assert.equal(env.get('#setting-effort').value, 'low');
  assert.match(env.get('#settings-feedback').textContent, /未保存/);
});

test('迟到探测响应不能覆盖当前 Harness，失败可重新检测', async () => {
  const env = setup('#settings/codex');
  env.run('hold = true;');
  await initialize(env);
  await navigate(env, '#settings/deepseek');
  env.run('hold = false; pending.shift()();');
  await tick();
  assert.equal(env.get('#settings-title').textContent, 'deepseek');
  env.run('pending.shift()();');
  await tick();
  env.run('failProbe = true;');
  env.get('#harness-list').children[1].querySelector('.harness-detect').click();
  await tick();
  assert.equal(env.get('#settings-save').disabled, true);
  assert.match(env.get('#harness-fields').children[0].textContent, /握手失败/);
  env.run('failProbe = false;');
  env.get('#harness-list').children[1].querySelector('.harness-detect').click();
  await tick();
  assert.equal(env.get('#settings-save').disabled, false);
});

test('实际配置及警告走同一事件入口，切换会话清空旧警告', () => {
  const env = environment();
  const warning = env.document.createElement('p');
  warning.id = 'session-config-warning';
  env.document.body.append(warning);
  env.run(`state.generation=1; state.context={projectId:'p',sessionId:'s',generation:1,lastEventId:0};
    applyEvent({id:1,project_id:'p',session_id:'s',kind:'session.config',payload:{actual:{permission:'allow'},warnings:[{id:'permission',reason:'需要新建会话'}]}},state.context);`);
  assert.match(warning.textContent, /permission：需要新建会话/);
  assert.equal(warning.hidden, false);
  assert.equal(env.run('state.runs.size'), 0);
  env.run('clearConversationDom()');
  assert.equal(warning.hidden, true);
});
