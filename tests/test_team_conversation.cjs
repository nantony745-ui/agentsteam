const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const {environment} = require('./frontend_dom.cjs');

function setup() {
  const env = environment();
  env.run(fs.readFileSync(path.join(__dirname,'../app/static/team-conversation.js'),'utf8'));
  env.run(`
    globalThis.sessionStorage = {data:new Map(),getItem(key){return this.data.get(key)||null;},setItem(key,value){this.data.set(key,value);},removeItem(key){this.data.delete(key);}};
    globalThis.setInterval = () => 1; globalThis.clearInterval = () => {};
    let sequence = 0; globalThis.crypto = {randomUUID:() => 'request-'+(++sequence)};
    globalThis.Option = function(text,value){const item=document.createElement('option');item.textContent=text;item.value=value;return item;};
    const originalLoad = TeamConversation.prototype.load;
    TeamConversation.prototype.load = async function(){this.loading=false;this.input.disabled=false;};
    const member = {profile:{employee_id:'employee',name:'员工'}};
    const owner = {project:{team:{leader_id:'captain'}},roster:{sessions:[{id:'one',employee_id:'employee'},{id:'two',employee_id:'employee'}]},activate(){},file(){}};
    const first = new TeamConversation({id:'one',project_id:'p',harness:'codex'},member,owner);
    const second = new TeamConversation({id:'two',project_id:'p',harness:'codex'},member,owner);
    api = async () => ({files:[]});
  `);
  return env;
}

test('团队输入框只有一个操作按钮，发送后切停止，取消后恢复发送并保留草稿', async () => {
  const env = setup();
  await env.run(`(async()=>{
    globalThis.calls=[];globalThis.originalButton=first.sendButton;
    api=async path=>{calls.push(path);return path.endsWith('/messages')?{run_id:'active',status:'queued'}:{id:'active',status:'canceled'};};
    first.update();first.input.value='开始';await first.sendButton.listeners.click[0]();
  })()`);
  assert.equal(env.run('first.root.querySelectorAll(".composer-actions button").length'), 1);
  assert.equal(env.run('first.sendButton.textContent'), '■');
  assert.equal(env.run('first.sendButton.getAttribute("aria-label")'), '停止本轮');
  await env.run(`first.input.value='保留下一条草稿';first.sendButton.listeners.click[0]()`);
  assert.equal(env.run('first.sendButton===originalButton'), true);
  assert.equal(env.run('first.sendButton.textContent'), '↑');
  assert.equal(env.run('first.sendButton.disabled'), false);
  assert.equal(env.run('first.input.value'), '保留下一条草稿');
  assert.equal(env.run('calls.join(",")'), '/api/sessions/one/messages,/api/runs/active/cancel');
});

test('停止中禁止重复点击，失败可重试，迟到取消响应不覆盖已完成状态', async () => {
  const env = setup();
  env.run(`first.ensureRun('active',{status:'running'});first.update();api=async()=>{throw Error('offline');};`);
  await env.run('first.sendButton.listeners.click[0]()');
  assert.equal(env.run('first.sendButton.disabled'), false);
  assert.match(env.run('first.error.textContent'), /offline/);
  await env.run(`(async()=>{
    globalThis.cancelCalls=0;
    api=path=>path.endsWith('/cancel')?new Promise(resolve=>{cancelCalls++;globalThis.finishCancel=resolve;}):Promise.resolve({files:[]});
    globalThis.stopping=first.stop();await first.stop();
  })()`);
  assert.equal(env.run('cancelCalls'), 1);
  assert.equal(env.run('first.sendButton.disabled'), true);
  assert.equal(env.run('first.sendButton.title'), '正在停止');
  await env.run(`first.apply({id:1,session_id:'one',run_id:'active',kind:'run.status',payload:{status:'completed'}})`);
  await env.run(`finishCancel({id:'active',status:'cancel_requested'});stopping`);
  assert.equal(env.run('first.runs.get("active").record.status'), 'completed');
  assert.equal(env.run('first.sendButton.textContent'), '↑');
  assert.equal(env.run('first.sendButton.disabled'), false);
});

test('团队窗持久化实际模型，重开无需发送，较旧历史与其他会话事件不覆盖', async () => {
  const env = setup();
  await env.run(`first.apply({id:20,session_id:'one',kind:'session.model',payload:{model_id:'actual-model'}})`);
  env.run(`first.destroy();globalThis.reopened=new TeamConversation({id:'one',project_id:'p',harness:'codex',configured_model:'configured'},member,owner);`);
  assert.equal(env.run('reopened.model.textContent'), 'Codex actual-model');
  await env.run(`reopened.apply({id:10,session_id:'one',kind:'session.model',payload:{model_id:'older'}})`);
  await env.run(`reopened.apply({id:30,session_id:'two',kind:'session.model',payload:{model_id:'other'}})`);
  assert.equal(env.run('reopened.model.textContent'), 'Codex actual-model');
  assert.equal(env.run('state.modelId'), null);
});

test('团队首屏从启动配置恢复模型，不需要模型事件或运行记录', async () => {
  const env = setup();
  await env.run(`(async()=>{
    api=async url=>url.endsWith('/history')?{runs:[],active_runs:[],metadata:[],high_watermark:0,before:null}:{session:{configured_model:'snapshot-model'},interactions:[]};
    first.readEvents=async()=>{};
    await originalLoad.call(first);
  })()`);
  assert.equal(env.run('first.model.textContent'), 'Codex snapshot-model');
  assert.equal(env.run('first.runs.size'), 0);
});

test('完整历史未超预算时不读取布局，避免终态额外同步排版', () => {
  const env = setup();
  env.run(`
    for(let n=0;n<HISTORY_RUN_LIMIT;n++)first.ensureRun('r'+n,{id:'r'+n,status:'completed'});
    first.timeline.getBoundingClientRect=()=>{throw new Error('不需要淘汰时不应读取布局');};
    first.trimHistory();
  `);
  assert.equal(env.run('first.runs.size'),60);
});

test('相同工具 ID 在不同窗口和 Run 中互不污染', async () => {
  const env = setup();
  await env.run(`(async()=>{
    await first.apply({id:1,session_id:'one',run_id:'a',kind:'agent.tool',payload:{toolCallId:'same',title:'甲',status:'completed'}});
    await second.apply({id:2,session_id:'two',run_id:'b',kind:'agent.tool',payload:{toolCallId:'same',title:'乙',status:'completed'}});
    await first.apply({id:3,session_id:'one',run_id:'c',kind:'agent.tool',payload:{toolCallId:'same',title:'丙',status:'completed'}});
  })()`);
  assert.equal(env.run('first.tools.size'),2);
  assert.equal(env.run('second.tools.size'),1);
  assert.equal(env.run('first.tools.get("a:same").data.title'),'甲');
  assert.equal(env.run('second.tools.get("b:same").data.title'),'乙');
});

test('高频文本合并渲染，完成时刷新尾片，销毁清理待绘制内容', async () => {
  const env = setup();
  await env.run(`(async()=>{
    globalThis.renders=0;const originalRender=renderMarkdown;
    renderMarkdown=(...args)=>{renders++;originalRender(...args);};
    for(let id=1;id<=1000;id++)await first.apply({id,session_id:'one',run_id:'a',kind:'agent.text',payload:{channel:'message',text:'文字'}});
  })()`);
  assert.equal(env.run('renders'),0);
  env.run('first.flushText()');
  assert.equal(env.run('renders'),1);
  assert.equal(env.run('first.runs.get("a").segments[0].text.length'),2000);
  await env.run(`first.apply({id:1001,session_id:'one',run_id:'a',kind:'agent.text',payload:{channel:'message',text:'尾'}})`);
  await env.run(`first.apply({id:1002,session_id:'one',run_id:'a',kind:'run.status',payload:{status:'completed'}})`);
  assert.equal(env.run('renders'),2);
  assert.match(env.run('first.runs.get("a").segments[0].body.innerHTML'),/尾/);
  await env.run(`second.apply({id:1003,session_id:'two',run_id:'b',kind:'agent.text',payload:{channel:'message',text:'不再绘制'}})`);
  env.run('second.destroy()');
  assert.equal(env.run('second.textTimer'),null);
  assert.equal(env.run('second.dirtyText.size'),0);
});

test('工具输出增量不重复拉取详情，终态刷新一次', async () => {
  const env = setup();
  await env.run(`(async()=>{
    globalThis.detailCalls=0;
    api=async()=>{detailCalls++;return {records:[],next:null,high_watermark:99};};
    await first.apply({id:1,session_id:'one',run_id:'a',kind:'agent.tool',payload:{toolCallId:'t',status:'in_progress',rawOutput:{$detail:'rawOutput'}}});
    const tool=first.tools.get('a:t');tool.root.open=true;await first.toolDetails(tool);
  })()`);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(env.run('detailCalls'),1);
  await env.run(`(async()=>{for(let id=2;id<30;id++)await first.apply({id,session_id:'one',run_id:'a',kind:'agent.tool',payload:{toolCallId:'t',rawOutput:{$detail:'rawOutput',bytes:id}}});})()`);
  assert.equal(env.run('detailCalls'),1);
  await env.run(`first.apply({id:30,session_id:'one',run_id:'a',kind:'agent.tool',payload:{toolCallId:'t',status:'completed'}})`);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(env.run('detailCalls'),2);
});

test('补拉失败后阻止后续事件越过缺口，重放成功后恢复', async () => {
  const env = setup();
  env.run(`globalThis.applied=[];globalThis.fail=true;first.apply=async row=>{if(fail)throw new Error('offline');applied.push(row.id);};`);
  await assert.rejects(env.run(`first.receive({id:1,session_id:'one',kind:'session.model',payload:{}})`));
  await assert.rejects(env.run(`first.receive({id:2,session_id:'one',kind:'session.model',payload:{}})`));
  assert.equal(env.run('first.cursor'),0);
  env.run('fail=false');
  await env.run(`(async()=>{for(const id of [1,2])await first.receive({id,session_id:'one',kind:'session.model',payload:{}});})()`);
  assert.equal(env.run('applied.join(",")'),'1,2');
  assert.equal(env.run('first.cursor'),2);
});

test('未知 Run 先补历史前缀，重复事件去重，父会话任务事件不产生空 Run', async () => {
  const env = setup();
  await env.run(`(async()=>{
    const records={a:{id:'a',session_id:'one',status:'running'},child:{id:'child',session_id:'two'}};
    api=async url=>url.startsWith('/api/runs/')?records[url.split('/')[3].split('?')[0]]:[];
    let prefix=0;
    first.readEvents=async(runs,high)=>{prefix=high;await first.apply({id:4,session_id:'one',run_id:'a',kind:'agent.text',payload:{channel:'message',text:'前缀'}});};
    globalThis.prefixValue=()=>prefix;
    const row={id:5,session_id:'one',run_id:'a',kind:'agent.text',payload:{channel:'message',text:'后缀'}};
    await first.receive(row);await first.receive(row);
    await first.receive({id:6,session_id:'one',run_id:'child',kind:'task.created',payload:{}});
  })()`);
  assert.equal(env.run('prefixValue()'),4);
  assert.equal(env.run('first.runs.get("a").segments[0].text'),'前缀后缀');
  assert.equal(env.run('first.runs.size'),1);
  assert.equal(env.run('first.cursor'),6);
});

test('加载期间缓冲增量，历史水位以前的重复消息不重放', async () => {
  const env = setup();
  await env.run(`(async()=>{
    first.loading=true;
    await first.receive({id:9,session_id:'one',kind:'session.model',payload:{model_id:'old'}});
    await first.receive({id:11,session_id:'one',kind:'session.model',payload:{model_id:'current'}});
    api=async path=>path.endsWith('/history')?{runs:[],active_runs:[],metadata:[],high_watermark:10,before:null}:{session:{},interactions:[]};
    first.readEvents=async()=>{};
    await originalLoad.call(first);
  })()`);
  assert.equal(env.run('first.cursor'),11);
  assert.match(env.run('first.model.textContent'),/current/);
  assert.equal(env.run('first.buffer.length'),0);
});

test('历史加载期间的实时缓冲也受同一事件预算约束', async () => {
  const env = setup();
  await env.run(`(async()=>{first.loading=true;for(let id=1;id<=LIVE_EVENT_LIMIT;id++)await first.receive({id,session_id:'one',kind:'session.model',payload:{}});})()`);
  await assert.rejects(env.run(`first.receive({id:257,session_id:'one',kind:'session.model',payload:{}})`), /事件积压/);
  assert.equal(env.run('first.buffer.length'),256);
  assert.equal(env.run('first.cursor'),0);
});

test('停止优先当前执行，窗口输入与取消只提交自己的会话和 Run', async () => {
  const env = setup();
  await env.run(`(async()=>{
    const calls=[];globalThis.calls=calls;
    api=async(path,options)=>{calls.push({path,body:options.body&&JSON.parse(options.body)});return {run_id:'sent',status:'queued'};};
    first.ensureRun('active',{status:'running'});first.ensureRun('queue',{status:'queued'});first.update();
    await first.stop();
    second.input.value='给第二窗';await second.send();
  })()`);
  assert.equal(env.run('calls[0].path'),'/api/runs/active/cancel');
  assert.equal(env.run('calls[1].path'),'/api/sessions/two/messages');
  assert.equal(env.run('calls[1].body.message'),'给第二窗');
});

test('历史缓冲回放途中到达的新增量保持顺序，不跳过缓冲消息', async () => {
  const env = setup();
  await env.run(`(async()=>{
    first.loading=true;
    for(const id of [11,12])await first.receive({id,session_id:'one',kind:'session.model',payload:{}});
    api=async path=>path.endsWith('/history')?{runs:[],active_runs:[],metadata:[],high_watermark:10,before:null}:{session:{},interactions:[]};
    first.readEvents=async()=>{};
    globalThis.order=[];
    globalThis.started=new Promise(resolve=>{globalThis.markStarted=resolve;});
    first.apply=async row=>{order.push(row.id);if(row.id===11){markStarted();await new Promise(resolve=>{globalThis.release=resolve;});}};
    globalThis.draining=originalLoad.call(first);
    await started;
    const incoming=first.receive({id:13,session_id:'one',kind:'session.model',payload:{}});
    release();await Promise.all([draining,incoming]);
  })()`);
  assert.equal(env.run('order.join(",")'),'11,12,13');
  assert.equal(env.run('first.cursor'),13);
});

test('发送失败保留幂等 ID，关窗重开恢复草稿与同一次重试', async () => {
  const env = setup();
  await env.run(`(async()=>{
    api=async()=>{throw new Error('offline');};
    first.input.value='待确认消息';await first.send();
    globalThis.originalRequest=first.pending.request_id;first.destroy();
    globalThis.reopened=new TeamConversation({id:'one',project_id:'p',harness:'codex'},member,owner);
    api=async(path,options)=>{globalThis.retried=JSON.parse(options.body);return {run_id:'accepted',status:'queued'};};
    await reopened.send();
  })()`);
  assert.equal(env.run('retried.request_id===originalRequest'),true);
  assert.equal(env.run('retried.message'),'待确认消息');
  assert.equal(env.run('sessionStorage.getItem(reopened.pendingKey())'),null);
  assert.equal(env.run('reopened.input.value'),'');
});

test('发送响应迟到已关闭的窗口不会污染另一窗草稿', async () => {
  const env = setup();
  await env.run(`(async()=>{
    let resolve;api=()=>new Promise(done=>{resolve=done;});
    second.input.value='另一窗草稿';second.saveDraft();
    first.input.value='已发送';const sending=first.send();first.destroy();
    resolve({run_id:'accepted',status:'queued'});await sending;
  })()`);
  assert.equal(env.run('second.input.value'),'另一窗草稿');
  assert.equal(env.run('sessionStorage.getItem(second.draftKey())'),'另一窗草稿');
  assert.equal(env.run('sessionStorage.getItem(first.pendingKey())'),null);
});

test('权限回答网络失败后核对服务端结果，已解决权限不重新启用', async () => {
  const env = setup();
  await env.run(`(async()=>{
    api=async(path)=>{if(path.includes('/answer'))throw new Error('lost response');return {interactions:[]};};
    first.permission({id:'permission',run_id:'r',status:'pending',payload:{options:[{name:'允许',optionId:'allow'}]}});
    first.permissions.get('permission').root.querySelector('button').click();
  })()`);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(env.run('first.permissions.get("permission").status'),'resolved');
  assert.equal(env.run('first.permissions.get("permission").root.querySelectorAll("button").length'),0);
  assert.equal(env.run('second.permissions.size'),0);
});

test('团队历史保留60轮并释放关联记录，占位补拉保持实时游标且失败可重试', async () => {
  const env = setup();
  await env.run(`(async()=>{
    globalThis.records=Array.from({length:100},(_,i)=>({id:'r'+i,session_id:'one',status:'completed',created_at:new Date(1700000000000+i*1000).toISOString()}));
    globalThis.rows=records.flatMap((run,i)=>[
      {id:i*5+1,session_id:'one',run_id:run.id,kind:'user.message',payload:{message:'输入'+i}},
      {id:i*5+2,session_id:'one',run_id:run.id,kind:'agent.tool',payload:{toolCallId:'t',status:'completed'}},
      {id:i*5+3,session_id:'one',run_id:run.id,kind:'agent.text',payload:{channel:'message',text:'正文'+i}},
      {id:i*5+4,session_id:'one',run_id:run.id,kind:'permission.expired',payload:{permission_id:'p'+i,status:'expired'}},
      {id:i*5+5,session_id:'one',run_id:run.id,kind:'run.status',payload:{status:'completed'},created_at:run.created_at},
    ]);
    api=async path=>{
      const [base,search]=path.split('?'),q=new URLSearchParams(search);
      if(base.endsWith('/file-changes'))return {files:[]};
      if(base==='/api/events')return rows.filter(row=>q.getAll('run_id').includes(row.run_id)&&row.id>Number(q.get('after_event_id'))&&row.id<=Number(q.get('through_event_id'))).slice(0,100);
      return records.find(run=>base.endsWith('/'+run.id));
    };
    for(const run of records){first.ensureRun(run.id,run);first.questions.set(run.id,{run_id:run.id,status:'answered'});for(const row of rows.filter(row=>row.run_id===run.id))await first.apply(row);}
    first.cursor=500;
  })()`);
  assert.equal(env.run('first.runs.size'),60);
  assert.equal(env.run('first.slots.size'),40);
  assert.equal(env.run('first.tools.size'),60);
  assert.equal(env.run('first.permissions.size'),60);
  assert.equal(env.run('first.questions.size'),60);
  assert.equal(env.run('[...first.runs.values()].every(run=>run.segments.every(block=>block.text===undefined))'),true);
  await env.run(`first.restoreRun('r0')`);
  assert.equal(env.run('first.runs.size'),60);
  assert.equal(env.run('first.runs.get("r0").root.querySelector(".team-final-answer").innerHTML.includes("正文0")'),true);
  assert.equal(env.run('first.cursor'),500);
  await env.run(`(async()=>{globalThis.originalApi=api;api=async path=>{if(path.startsWith('/api/events'))throw new Error('offline');return originalApi(path);};try{await first.restoreRun('r1');}catch{};})()`);
  assert.equal(env.run('first.slots.get("r1").root.disabled'),false);
  await env.run(`api=originalApi;first.restoreRun('r1')`);
  assert.equal(env.run('first.runs.has("r1")'),true);
  assert.equal(env.run('first.runs.size'),60);
  env.run(`first.ensureRun('active',{status:'running'});first.trimHistory();`);
  assert.equal(env.run('first.runs.has("active")'),true);
  env.run('first.destroy()');
  assert.equal(env.run('first.runs.size+first.slots.size+first.tools.size+first.permissions.size+first.questions.size'),0);
});

test('计时只更新活动轮，隐藏窗和隐藏页面暂停且恢复后补刷新', () => {
  const env = setup();
  env.run(`
    globalThis.startedTimers=0;globalThis.stoppedTimers=0;
    setInterval=callback=>{globalThis.tick=callback;return ++startedTimers;};clearInterval=()=>{stoppedTimers++;};
    for(let i=0;i<50;i++)first.ensureRun('done'+i,{status:'completed',created_at:'2026-10-29T00:00:00Z',finished_at:'2026-10-29T00:01:00Z'});
    first.ensureRun('active',{status:'running',created_at:'2026-10-29T00:00:00Z'});
    first.questions.set('q',{run_id:'active',status:'pending'});
    const originalValues=first.questions.values.bind(first.questions);globalThis.questionScans=0;
    first.questions.values=()=>{questionScans++;return originalValues();};
    first.update();globalThis.scansAfterUpdate=questionScans;
    globalThis.historySummary=first.runs.get('done0').summary.textContent;
    for(let i=0;i<10;i++)tick();
  `);
  assert.equal(env.run('questionScans'),env.run('scansAfterUpdate'));
  assert.equal(env.run('first.runs.get("done0").summary.textContent===historySummary'),true);
  assert.match(env.run('first.runs.get("active").summary.textContent'),/等待回答/);
  env.run('first.setVisible(false)');
  assert.equal(env.run('first.timer'),null);
  env.run('first.setVisible(true)');
  assert.equal(env.run('startedTimers'),2);
  env.run('document.hidden=true;document.emit("visibilitychange",{})');
  assert.equal(env.run('first.timer'),null);
  env.run('document.hidden=false;document.emit("visibilitychange",{})');
  assert.equal(env.run('startedTimers'),3);
  env.run('first.runs.get("active").record.status="completed";first.update();');
  assert.equal(env.run('first.timer'),null);
  env.run('first.destroy()');
  assert.equal(env.run('document.listeners.visibilitychange.includes(first.visibilityChanged)'),false);
});
