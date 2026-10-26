const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8')
  .replace(/\ninit\(\);\s*$/, '\n');

function environment() {
  const elements = new Map();
  const context = vm.createContext({
    console, URLSearchParams, setTimeout, clearTimeout,
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    document: {
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, { value: '', textContent: '', disabled: true, dataset: {},
          setAttribute(name, value) { this[name] = value; }, toggleAttribute() {} });
        return elements.get(selector);
      },
    },
  });
  vm.runInContext(source, context);
  return { elements, run: (code) => vm.runInContext(code, context) };
}

// 仅实现本文件卡片断言所需的 DOM 行为，不替代真实浏览器验收。
function permissionEnvironment() {
  const env = environment();
  env.run(`
    class Element {
      constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.disabled = false; }
      append(...children) {
        for (const child of children) {
          if (child.parent) child.parent.children = child.parent.children.filter(item => item !== child);
          child.parent = this; this.children.push(child);
        }
      }
      replaceChildren(...children) { this.children = [...children]; }
      addEventListener(kind, handler) { this.listeners[kind] = handler; }
      querySelector(selector) { return selector === '.work-items' ? this.items : ['.run-content', '.run-alerts'].includes(selector) ? this : null; }
    }
    document.createElement = tag => new Element(tag);
    const runNodes = new Map();
    ensureRun = id => {
      if (!runNodes.has(id)) { const node = new Element('article'); node.items = new Element('div'); runNodes.set(id, node); }
      return runNodes.get(id);
    };
    restoreRunAnswer = () => {}; settleRunContent = () => {}; updateWorkLog = () => {};
    const selectedContext = {generation: 0, sessionId: 'session-a', projectId: 'p', lastEventId: 0};
    state.context = selectedContext;
    const permissionRecord = {id: 'host-id', run_id: 'run-a', kind: 'permission', status: 'pending', payload: {
      tool_call: {toolCallId: 'native-tool', title: '<img src=x onerror=alert(1)>'},
      options: [{optionId: 'native/reject/id', name: '拒绝 <script>', kind: 'reject_once', _meta: {opaque: true}},
        {optionId: 'native/allow/id', name: '允许', kind: 'allow_once'}]}};
    const buttons = id => state.permissionCards.get(id).children[2].children;
  `);
  return env;
}

test('权限按钮提交原生 option_id，成功后禁用并阻止旧 pending 复活', async () => {
  const { run } = permissionEnvironment();
  await run(`(async () => {
    applyPermission(permissionRecord, selectedContext);
    const card = state.permissionCards.get('host-id');
    if (card.children[0].children[0].textContent !== '权限：<img src=x onerror=alert(1)>') throw new Error('未保留文本');
    if (buttons('host-id')[0].dataset.optionId !== 'native/reject/id') throw new Error('选项 ID 错误');
    let finish; let request;
    api = (url, options) => { request = {url, options}; return new Promise(resolve => {finish = resolve;}); };
    const answering = buttons('host-id')[0].listeners.click();
    if (!buttons('host-id').every(button => button.disabled)) throw new Error('提交中未禁用');
    if (request.url !== '/api/interactions/host-id/answer' ||
      JSON.parse(request.options.body).option_id !== 'native/reject/id') throw new Error('未提交原生拒绝 ID');
    finish({...permissionRecord, status: 'answered', payload: JSON.stringify(permissionRecord.payload)});
    await answering;
    applyPermission(permissionRecord, selectedContext);
    if (state.permissions.get('host-id').status !== 'answered' || !buttons('host-id').every(button => button.disabled))
      throw new Error('已回答请求被复活');
    if (runNodes.get('run-a').items.children.length !== 1) throw new Error('卡片重复');
  })()`);
});

test('权限 pending 从 Session 恢复，历史与实时按宿主 ID 合并', async () => {
  const { run } = permissionEnvironment();
  await run(`(async () => {
    restorePermissions([{...permissionRecord, payload: JSON.stringify(permissionRecord.payload)}], selectedContext);
    const requested = {id: 1, project_id: 'p', session_id: 'session-a', run_id: 'run-a', kind: 'permission.requested',
      payload: {...permissionRecord.payload, permission_id: 'host-id', status: 'pending'}};
    applyEvent(requested, selectedContext); applyEvent(requested, selectedContext);
    if (runNodes.get('run-a').items.children.length !== 1 || buttons('host-id').some(button => button.disabled))
      throw new Error('刷新或重复事件错误');
    applyEvent({...requested, id: 2, kind: 'permission.expired', payload: {permission_id: 'host-id', status: 'expired'}}, selectedContext);
    if (!buttons('host-id').every(button => button.disabled)) throw new Error('过期未禁用');
    // 迟到快照及旧 requested 都不能复活已过期卡片。
    restorePermissions([permissionRecord], selectedContext);
    if (state.permissions.get('host-id').status !== 'expired') throw new Error('过期被覆盖');
  })()`);
});

test('历史之后查询 pending，已从服务端消失的卡片不可操作', async () => {
  const { run } = permissionEnvironment();
  await run(`(async () => {
    applyPermission(permissionRecord, selectedContext);
    api = async () => ({interactions: []});
    await refreshPermissions(selectedContext);
    if (state.permissions.get('host-id').status !== 'resolved' || !buttons('host-id').every(button => button.disabled))
      throw new Error('旧历史仍可回答');
  })()`);
});

test('权限提交失败后查询 pending，恢复可重试状态', async () => {
  const { run } = permissionEnvironment();
  await run(`(async () => {
    applyPermission(permissionRecord, selectedContext);
    api = async (url, options) => {
      if (options) throw new Error('提交失败');
      return {interactions: [{...permissionRecord, payload: JSON.stringify(permissionRecord.payload)}]};
    };
    await answerPermission('host-id', 'native/reject/id', selectedContext);
    if (buttons('host-id').some(button => button.disabled) || state.permissions.get('host-id').error !== '提交失败')
      throw new Error('未恢复重试状态');
  })()`);
});

test('回答和核对均失败时保持禁用，重新加载成功后恢复 pending 按钮', async () => {
  const { run } = permissionEnvironment();
  await run(`(async () => {
    applyPermission(permissionRecord, selectedContext);
    api = async () => { throw new Error('网络中断'); };
    await answerPermission('host-id', 'native/reject/id', selectedContext);
    if (!buttons('host-id').every(button => button.disabled)) throw new Error('未核对状态就恢复按钮');
    api = async () => ({interactions: [permissionRecord]});
    await refreshPermissions(selectedContext);
    if (buttons('host-id').some(button => button.disabled)) throw new Error('重新加载后仍无法回答');
  })()`);
});

test('权限回答和 pending 查询的迟到响应不污染 A→B→A', async () => {
  const { run } = permissionEnvironment();
  await run(`(async () => {
    applyPermission(permissionRecord, selectedContext);
    let finish; api = () => new Promise(resolve => {finish = resolve;});
    const answering = answerPermission('host-id', 'native/reject/id', selectedContext);
    state.generation = 2; state.context = {...selectedContext, generation: 2};
    state.permissions.clear(); state.permissionCards.clear();
    finish({...permissionRecord, status: 'answered', payload: JSON.stringify(permissionRecord.payload)}); await answering;
    if (state.permissions.size !== 0 || state.permissionCards.size !== 0) throw new Error('旧回答污染新会话');
    const querying = refreshPermissions(state.context);
    state.generation = 4; state.context = {...selectedContext, generation: 4};
    finish({interactions: [permissionRecord]}); await querying;
    if (state.permissions.size !== 0) throw new Error('旧查询污染新会话');
  })()`);
});

test('pending 查询期间收到已回答事件，迟到快照不可重新启用按钮', async () => {
  const { run } = permissionEnvironment();
  await run(`(async () => {
    applyPermission(permissionRecord, selectedContext);
    let finish; let calls = 0;
    api = () => { calls += 1; return calls === 1 ? new Promise(resolve => {finish = resolve;}) : Promise.resolve({interactions: []}); };
    const querying = refreshPermissions(selectedContext);
    applyPermission({...permissionRecord, status: 'answered'}, selectedContext);
    finish({interactions: [permissionRecord]}); await querying;
    if (calls !== 2 || state.permissions.get('host-id').status !== 'answered' || !buttons('host-id').every(button => button.disabled))
      throw new Error('迟到快照覆盖回答');
  })()`);
});

test('新建会话可进入草稿，未选择项目时禁止发送', () => {
  const { elements, run } = environment();
  run(`resetConversation = () => {}; renderSessionList = () => {};
    saveSelection = () => {}; loadSessions = async () => {};
    $('#create-session-button').disabled = true;
    selectProject('project-a');`);
  assert.equal(elements.get('#project-select').value, 'project-a');
  assert.equal(elements.get('#create-session-button').disabled, false);
  run('selectProject(null)');
  assert.equal(elements.get('#create-session-button').disabled, false);
  assert.equal(elements.get('#send-button').disabled, true);
});

test('创建项目的迟到响应不抢走新项目选择', async () => {
  const { run } = environment();
  await run(`(async () => {
    $('#project-path').value = 'D:\\agentdemo';
    let complete; api = () => new Promise(resolve => { complete = resolve; });
    let selected = null; selectProject = id => { selected = id; };
    loadProjects = async () => {};
    const pending = createProject({preventDefault() {}});
    state.generation += 1; state.projectId = 'project-b';
    complete({id: 'new-project'}); await pending;
    if (selected !== null) throw new Error('迟到创建结果覆盖了当前项目');
  })()`);
});

test('旧会话历史请求失败不关闭新会话实时流', async () => {
  const { run } = environment();
  await run(`(async () => {
    const old = {generation: 0, projectId: 'p', sessionId: 'a', lastEventId: 0};
    state.context = old; setStreamStatus = () => {};
    let reject; api = () => new Promise((resolve, fail) => { reject = fail; });
    let closed = 0; closeStream = () => { closed += 1; };
    const pending = loadHistory(old);
    state.generation = 1; state.context = {generation: 1, sessionId: 'b'};
    reject(new Error('旧请求断开')); await pending;
    if (closed !== 0) throw new Error('旧会话错误关闭了新会话流');
  })()`);
});

test('发送失败重试复用正文、Session 与 request_id', async () => {
  const { run } = environment();
  await run(`(async () => {
    state.context = {sessionId: 'a', generation: 0, ready: true};
    updateMessageForm = () => {}; refreshRun = async () => {}; showPendingWork = () => {}; renderRunItem = () => {};
    const calls = []; api = async (url, options) => {
      calls.push({url, body: options.body});
      if (calls.length === 1) throw new Error('网络断开');
      return {run_id: 'run-a', status: 'queued'};
    };
    const pending = {projectId: 'p', sessionId: 'a', message: '原文', requestId: 'stable-id'};
    state.pendingBySession.set('a', pending);
    await submitPending(pending); await retryMessage();
    if (calls.length !== 2 || calls[0].url !== calls[1].url || calls[0].body !== calls[1].body)
      throw new Error('重试改变了请求身份');
    if (state.pendingBySession.has('a')) throw new Error('成功后仍保留失败请求');
  })()`);
});

test('取消使用选定的 run_id 并采用后端确认状态', async () => {
  const { run } = environment();
  await run(`(async () => {
    state.context = {sessionId: 'a', generation: 0};
    state.runItems.set('run-target', {querySelector() { return {}; }});
    renderRunItem = () => {};
    let endpoint; api = async url => {
      endpoint = url; return {id: 'run-target', session_id: 'a', status: 'completed'};
    };
    await cancelRun('run-target');
    if (endpoint !== '/api/runs/run-target/cancel') throw new Error('取消目标错误');
    if (state.runRecords.get('run-target').status !== 'completed') throw new Error('覆盖了完成竞争结果');
  })()`);
});

test('事件去重并拒绝旧上下文与其他会话', () => {
  const { run } = environment();
  assert.equal(run(`(() => {
    const current = {generation: 0, projectId: 'p', sessionId: 'a', lastEventId: 0};
    state.context = current;
    let count = 0; renderEvent = () => { count += 1; };
    const row = {id: 10, project_id: 'p', session_id: 'a'};
    applyEvent(row, current); applyEvent(row, current);
    applyEvent({...row, id: 11, session_id: 'b'}, current);
    applyEvent({...row, id: 12}, {...current});
    return count === 1 && current.lastEventId === 10;
  })()`), true);
});

test('A→B→A 时旧 Run 查询结果不能覆盖新上下文', async () => {
  const { run } = environment();
  await run(`(async () => {
    state.context = {sessionId: 'a', generation: 0};
    let complete; api = () => new Promise(resolve => { complete = resolve; });
    const pending = refreshRun('r');
    state.generation = 2; state.context = {sessionId: 'a', generation: 2};
    state.runFetchVersions.clear(); state.runFetchVersions.set('r', 1);
    state.runRecords.set('r', {id: 'r', status: 'completed'});
    complete({id: 'r', session_id: 'a', status: 'running'}); await pending;
    if (state.runRecords.get('r').status !== 'completed') throw new Error('旧查询覆盖新上下文');
  })()`);
});

test('原始通知跳过时间线节点并正常推进游标', () => {
  const { run } = environment();
  assert.equal(run(`(() => {
    const context = {generation: 0, projectId: 'p', sessionId: 'a', lastEventId: 0};
    state.context = context;
    let nodes = 0; ensureRun = () => { nodes += 1; return {}; };
    appendDetails = () => {};
    restoreRunAnswer = () => {}; settleRunContent = () => {}; updateWorkLog = () => {};
    const row = {id: 10, project_id: 'p', session_id: 'a', run_id: 'r', kind: 'raw',
      payload: {raw: {method: 'session/update'}}};
    if (!applyEvent(row, context) || nodes !== 0 || context.lastEventId !== 10) return false;
    applyEvent(row, context);
    appendText = () => {};
    return applyEvent({...row, id: 11, kind: 'agent.text', payload: {text: '正常回复', channel: 'message'}}, context)
      && nodes === 1 && context.lastEventId === 11;
  })()`), true);
});
