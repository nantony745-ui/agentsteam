const test = require('node:test');
const assert = require('node:assert/strict');
const { environment } = require('./frontend_dom.cjs');

function setup() {
  const env = environment();
  env.run(`
    state.projects=[{id:'p',name:'repo',path:'D:/repo'}]; state.projectId='p';
    state.sessions=[
      {id:'parent',project_id:'p',parent_id:null,harness:'codex',title:'父会话',created_at:'2026-10-20'},
      {id:'child',project_id:'p',parent_id:'parent',harness:'opencode',title:'子会话',created_at:'2026-10-20'},
      {id:'other',project_id:'p',parent_id:null,harness:'codex',title:'其他会话',created_at:'2026-10-20'}];
    globalThis.calls=[]; globalThis.fail=false; globalThis.hold=null;
    globalThis.fetch=async (url,options={})=>{
      calls.push({url,method:options.method});
      if(hold) await new Promise(resolve=>hold=resolve);
      return {ok:!fail,json:async()=>fail?{detail:'清理连接失败'}:{deleted_session_ids:['parent','child']}};
    };
    bindSessionActions(); renderSessionList();
  `);
  return env;
}

async function tick() { for (let i=0;i<20;i++) await Promise.resolve(); }
function open(env, id='parent') {
  env.get(`button[data-session-id="${id}"]`).emit('contextmenu',{clientX:80,clientY:90});
}

test('项目树与最近列表均可右键，菜单不改变选中，点外和 Escape 关闭', () => {
  const env=setup();
  env.run("state.context={sessionId:'other'}");
  for(const button of env.document.querySelectorAll('button[data-session-id="parent"]')) {
    const event=button.emit('contextmenu',{clientX:1439,clientY:899});
    assert.equal(event.defaultPrevented,true);
    assert.equal(env.get('#session-menu').hidden,false);
    assert.equal(env.run('state.context.sessionId'),'other');
    assert.equal(env.get('#session-menu').style.left,'1256px');
    assert.equal(env.get('#session-menu').style.top,'842px');
    env.document.emit('keydown',{key:'Escape',preventDefault(){},stopPropagation(){}});
    assert.equal(env.get('#session-menu').hidden,true);
    assert.equal(env.document.activeElement.dataset.sidebarKey,button.dataset.sidebarKey);
  }
  open(env);
  env.document.emit('pointerdown',{target:env.document.body});
  assert.equal(env.get('#session-menu').hidden,true);
});

test('删除确认明确包含后代和数据范围，取消不发删除请求', () => {
  const env=setup(); open(env);
  env.get('#session-menu-delete').click();
  assert.equal(env.get('#session-delete-dialog').open,true);
  assert.match(env.get('#session-delete-description').textContent,/全部子会话/);
  assert.match(env.get('#session-delete-description').textContent,/原生记录保留/);
  assert.equal(env.document.activeElement.id,'session-delete-cancel');
  env.get('#session-delete-cancel').click();
  assert.equal(env.get('#session-delete-dialog').open,false);
  assert.equal(env.run('calls.length'),0);
});

test('删除当前子会话的父节点清空消息和 SSE、缓存、草稿与折叠偏好', async () => {
  const env=setup();
  env.run(`state.context={sessionId:'child',projectId:'p',generation:0}; state.harness='opencode';
    state.source={close(){globalThis.closed=true}};
    state.pendingBySession.set('child',{message:'删除草稿'}); state.sendingBySession.set('parent',{});
    state.collapsed.add('children:parent'); state.sidebarLimits.set('children:parent',10);
    localStorage.setItem(MODEL_CACHE_KEY,JSON.stringify({'session:child':{eventId:7},'agent:p:opencode':{eventId:7},'session:other':{eventId:8}}));`);
  open(env); env.get('#session-menu-delete').click();
  env.get('#session-delete-form').emit('submit'); await tick();
  assert.equal(env.run('calls[0].method'),'DELETE');
  assert.equal(env.run('closed'),true);
  assert.equal(env.run('state.context'),null);
  assert.equal(env.run('state.pendingBySession.size + state.sendingBySession.size'),0);
  assert.equal(env.run("state.collapsed.has('children:parent')"),false);
  assert.equal(env.get('#session-delete-dialog').open,false);
  assert.deepEqual(JSON.parse(env.storage.get('agentsteam.models.v1')),{'session:other':{eventId:8}});
  assert.equal(JSON.parse(env.storage.get('agentsteam.selection')).sessionId,null);
  assert.deepEqual(env.document.querySelectorAll('.session-button').map(node=>node.dataset.sessionId),['other','other']);
});

test('删除其他会话保持当前内容与实时流，重复提交仅发送一次', async () => {
  const env=setup();
  env.run("state.context={sessionId:'other',projectId:'p',generation:0}; state.source={close(){throw Error('其他会话不应断流')}}; hold=true;");
  env.get('#message-input').value='保留当前草稿';
  open(env); env.get('#session-menu-delete').click();
  env.get('#session-delete-form').emit('submit');
  env.get('#session-delete-form').emit('submit');
  assert.equal(env.run('calls.length'),1);
  const cancel=env.get('#session-delete-dialog').emit('cancel');
  assert.equal(cancel.defaultPrevented,true);
  env.run('hold()'); await tick();
  assert.equal(env.run('state.context.sessionId'),'other');
  assert.equal(env.get('#message-input').value,'保留当前草稿');
});

test('删除失败保留会话并显示原因，可以重试', async () => {
  const env=setup(); env.run('fail=true');
  open(env); env.get('#session-menu-delete').click();
  env.get('#session-delete-form').emit('submit'); await tick();
  assert.match(env.get('#session-delete-error').textContent,/清理连接失败/);
  assert.equal(env.get('#session-delete-dialog').open,true);
  assert.equal(env.run('state.sessions.length'),3);
  env.run('fail=false');
  env.get('#session-delete-form').emit('submit'); await tick();
  assert.equal(env.run('state.sessions.length'),1);
});

test('SSE 删除事件可重复处理，清理选中会话，不生成回合', () => {
  const env=setup();
  env.run(`state.context={projectId:'p',sessionId:'child',generation:0,lastEventId:0}; globalThis.context=state.context;
    applyEvent({id:10,project_id:'p',session_id:null,kind:'session.deleted',payload:{session_ids:['parent','child']}},context);
    applyEvent({id:10,project_id:'p',session_id:null,kind:'session.deleted',payload:{session_ids:['parent','child']}},context);`);
  assert.equal(env.run('state.context'),null);
  assert.equal(env.run('state.runs.size'),0);
  assert.equal(env.run('state.sessions.length'),1);
});

test('轮询发现外部删除也清理当前选择，删除前迟到列表不能恢复会话', async () => {
  const env=setup();
  env.run(`state.context={projectId:'p',sessionId:'child',generation:0};
    globalThis.answer=null; fetch=async()=>{await new Promise(resolve=>answer=resolve);return {ok:true,headers:{get:()=>null},json:async()=>state.sessions}};
    globalThis.loading=loadSessions(); applyDeletedSessions(['parent','child']); answer();`);
  await env.run('loading');
  assert.equal(env.run('state.sessions.length'),1);
  env.run(`state.context={projectId:'p',sessionId:'other',generation:state.generation}; fetch=async()=>({ok:true,headers:{get:()=>null},json:async()=>[]});`);
  await env.run('loadSessions()');
  assert.equal(env.run('state.context'),null);
});
