const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const {environment} = require('./frontend_dom.cjs');

function setup() {
  const env = environment();
  env.run(`
    for(const id of ['team-workspace','team-new-leader','team-workspace-status','team-workspace-error','team-member-tabs']){
      const node=document.createElement('div');node.id=id;document.body.append(node);
    }
    document.createTextNode=text=>{const node=document.createElement('span');node.textContent=text;return node;};
    globalThis.TeamsUI={init(){}};globalThis.EmployeeSelect={icon:()=>document.createElement('span')};
    globalThis.EventSource=class {
      constructor(){this.listeners={};globalThis.source=this;}
      addEventListener(name,callback){this.listeners[name]=callback;}
      close(){this.closed=true;}
      emit(row){this.listeners.lab({data:JSON.stringify(row)});}
    };
  `);
  const source = fs.readFileSync(path.join(__dirname,'../app/static/team-workspace.js'),'utf8');
  env.run(source.replace('let workspace;', 'let workspace; globalThis.getWorkspace=()=>workspace;'));
  env.run(`
    const ws=getWorkspace();
    ws.project={id:'p',team:{leader_id:'captain'}};
    ws.roster={leader_session_id:'leader',members:[{profile:{employee_id:'captain',name:'队长',harness:'codex'}}],
      sessions:[{id:'a',employee_id:'captain',status:'running'},{id:'b',employee_id:'captain',status:'running'}]};
  `);
  return env;
}

test('一个窗口补拉阻塞时，其他窗口继续处理，确认游标仍按顺序推进', async () => {
  const env = setup();
  await env.run(`(async()=>{
    globalThis.order=[];
    ws.panes=[{session:{id:'a'},receive:()=>new Promise(resolve=>{globalThis.release=()=>{order.push('a');resolve();};})},
      {session:{id:'b'},receive:async()=>{order.push('b');}}];
    ws.subscribe();
    source.emit({id:1,session_id:'a',run_id:'ra',kind:'agent.text',payload:{}});
    source.emit({id:2,session_id:'b',run_id:'rb',kind:'agent.text',payload:{}});
    await ws.chain;
  })()`);
  assert.equal(env.run('order.join(",")'),'b');
  assert.equal(env.run('ws.cursor'),0);
  env.run('release()');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(env.run('ws.cursor'),2);
});

test('标签状态相同保留按钮，状态变化才重建', () => {
  const env = setup();
  env.run('ws.tabs();globalThis.firstTab=document.querySelector("#team-member-tabs").children[0];ws.tabs();');
  assert.equal(env.run('firstTab===document.querySelector("#team-member-tabs").children[0]'),true);
  env.run('ws.roster.sessions[0].status="waiting_permission";ws.tabs();');
  assert.equal(env.run('firstTab===document.querySelector("#team-member-tabs").children[0]'),false);
});

test('处理失败保留确认游标，断线重放未确认事件', async () => {
  const env = setup();
  await env.run(`(async()=>{
    ws.panes=[{session:{id:'a'},receive:async()=>{throw new Error('补拉失败');}}];ws.subscribe();
    source.emit({id:1,session_id:'a',kind:'agent.text',payload:{}});await ws.chain;
  })()`);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(env.run('ws.cursor'),0);
  assert.equal(env.run('ws.deliveries.size'),0);
  assert.equal(env.run('ws.source'),null);
});

test('慢窗口积压有界，排空后从最后确认游标重连并去重', async () => {
  const env = setup();
  await env.run(`(async()=>{
    globalThis.seen=new Set();globalThis.received=[];
    const pane={session:{id:'a'},events:Promise.resolve(),receive(row){
      this.events=this.events.then(async()=>{
        if(row.id===1&&!seen.has(1))await new Promise(resolve=>{globalThis.release=resolve;});
        if(!seen.has(row.id)){seen.add(row.id);received.push(row.id);}
      });return this.events;
    }};
    ws.panes=[pane];ws.subscribe();globalThis.oldSource=source;
    source.emit({id:1,session_id:'a',kind:'agent.text',payload:{}});await ws.chain;
    for(let id=2;id<=LIVE_EVENT_LIMIT;id++){
      source.emit({id,session_id:'a',kind:'agent.text',payload:{}});await ws.chain;
    }
  })()`);
  assert.equal(env.run('ws.pendingEvents'),256);
  assert.equal(env.run('ws.deliveries.size'),256);
  env.run(`source.emit({id:257,session_id:'a',kind:'agent.text',payload:{}})`);
  assert.equal(env.run('oldSource.closed'),true);
  assert.equal(env.run('ws.cursor'),0);
  assert.equal(env.run('ws.pendingEvents'),0);
  const reconnect = env.run('ws.reconnect');
  env.timers.get(reconnect)();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(env.run('ws.source'),null);
  env.run('release()');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(env.run('ws.source!==null&&source!==oldSource'),true);
  await env.run(`(async()=>{for(let id=1;id<=257;id++){
    source.emit({id,session_id:'a',kind:'agent.text',payload:{}});await ws.chain;await ws.panes[0].events;
  }})()`);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(env.run('received.length'),257);
  assert.equal(env.run('ws.cursor'),257);
  assert.equal(env.run('ws.pendingEvents'),0);
});
