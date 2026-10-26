const assert = require('node:assert/strict');
const {test} = require('node:test');
const {environment} = require('./frontend_dom.cjs');

function history() {
  const env = environment();
  env.run(`
    state.projectId = 'p';
    state.context = {projectId: 'p', sessionId: 's', generation: 0, lastEventId: 0, nativeIdResolved: true};
    const context = state.context;
    const records = Array.from({length: 70}, (_, n) => ({id: 'r'+(n+1), cursor: n+1, session_id: 's', status: 'completed'}));
    const rows = records.flatMap((run, n) => [
      {id: n*4+1, project_id:'p', session_id:'s', run_id:run.id, kind:'user.message', payload:{message:'输入'+run.id}},
      {id: n*4+2, project_id:'p', session_id:'s', run_id:run.id, kind:'agent.tool', payload:{toolCallId:'t', title:'执行', status:'completed', rawOutput:{$detail:'rawOutput',bytes:900000,preview:'摘要',format:'text',operation:'replace'}}},
      {id: n*4+3, project_id:'p', session_id:'s', run_id:run.id, kind:'agent.text', payload:{channel:'message',text:'回答'+run.id}},
      {id: n*4+4, project_id:'p', session_id:'s', run_id:run.id, kind:'run.status', payload:{status:'completed'}},
    ]);
    const requests = [];
    refreshPermissions = async () => {};
    refreshSessionNativeId = () => {};
    refreshTaskChanges = () => {};
    scheduleSidebarRefresh = () => {};
    globalThis.EventSource = class {constructor(url) {this.url=url;} addEventListener() {} close() {}};
    api = async path => {
      requests.push(path);
      const [base, search] = path.split('?'); const q = new URLSearchParams(search);
      if (base === '/api/sessions/s/history') {
        const end = q.has('before') ? Number(q.get('before'))-1 : records.length;
        const start = Math.max(0,end-20);
        return {runs: records.slice(start,end),active_runs:[],high_watermark:280,before:start ? start+1 : null,metadata:[]};
      }
      if (base === '/api/events') {
        const selected = q.getAll('run_id');
        return rows.filter(row => selected.includes(row.run_id) && row.id > Number(q.get('after_event_id')) && row.id <= Number(q.get('through_event_id'))).slice(0,100);
      }
      if (base.includes('/tools/')) return {high_watermark:280, records:[{payload:{},parts:[{field:'rawOutput',text:'完整工具内容'}]}],next:null};
      if (base.endsWith('/file-changes')) return {files:[],additions:0,deletions:0};
      if (base.startsWith('/api/runs/')) return records.find(run => base.endsWith('/'+run.id));
      throw new Error('意外接口 '+path);
    };
  `);
  return env;
}

test('首屏仅加载最近20轮，工具折叠不读详情，完成正文释放源字符串', async () => {
  const env = history();
  await env.run('loadHistory(context)');
  assert.equal(env.document.querySelectorAll('.run').length, 20);
  assert.equal(env.run('state.tools.size'), 20);
  assert.equal(env.run('requests.some(path => path.includes("/tools/"))'), false);
  assert.equal(env.document.querySelectorAll('.tool-card pre').length, 0);
  assert.equal(env.run('[...state.textBlocks.values()].every(data => data.text === undefined)'), true);
  assert.equal(env.run('context.lastEventId'), 280);
  assert.match(env.run('state.source.url'), /after_event_id=280/);
  env.run(`const card = document.querySelector('.tool-card'); card.open=true; card.emit('toggle');`);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(env.get('.tool-card pre').textContent, '完整工具内容');
  env.run(`card.open=false; card.emit('toggle');`);
  assert.equal(env.get('.tool-card pre'), null);
});

test('旧页独立游标，60轮窗口释放工具和正文，点击占位可重新读取', async () => {
  const env = history();
  await env.run('loadHistory(context)');
  for (let n=0;n<3;n++) await env.run(`loadOlderHistory(context, document.querySelector('.history-more'))`);
  assert.equal(env.document.querySelectorAll('.run').length, 60);
  assert.equal(env.document.querySelectorAll('.history-placeholder').length, 10);
  assert.equal(env.run('state.toolData.size'), 60);
  assert.equal(env.run('state.textBlocks.size'), 60);
  assert.equal(env.run('context.lastEventId'), 280);
  assert.equal(env.get('.history-more').hidden, true);
  env.run(`document.querySelector('.history-placeholder').click()`);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(env.document.querySelectorAll('.run').length, 60);
  assert.equal(env.run('context.lastEventId'), 280);
  assert.equal(env.run('state.toolData.size'), 60);
});

test('实时遇到未加载Run先读取前缀，随后应用增量并去重', async () => {
  const env = history();
  await env.run('loadHistory(context)');
  await env.run(`receiveLiveEvent({id:281,project_id:'p',session_id:'s',run_id:'r1',kind:'agent.text',payload:{channel:'message',text:'新增'}}, context)`);
  assert.equal(env.run('context.loadedRuns.has("r1")'), true);
  assert.equal(env.run('context.lastEventId'), 281);
  assert.equal(env.run('state.runs.get("r1").querySelector(".user-message").textContent'), '输入r1');
  const size = env.run('state.textBlocks.size');
  await env.run(`receiveLiveEvent({id:281,project_id:'p',session_id:'s',run_id:'r1',kind:'agent.text',payload:{channel:'message',text:'重复'}}, context)`);
  assert.equal(env.run('state.textBlocks.size'), size);
});
