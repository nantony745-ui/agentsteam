const assert = require('node:assert/strict');
const {test} = require('node:test');
const {environment} = require('./frontend_dom.cjs');

test('原生 Markdown 方案按 planId 更新，移除后无执行入口', () => {
  const env = environment();
  env.run(`
    globalThis.parent = document.createElement('div'); parent.hidden=true; document.body.append(parent);
    renderCodexPlan({payload:{plan:{planId:'p',content:'# 方案一'}}},parent,()=>{});
    renderCodexPlan({payload:{plan:{planId:'p',content:'# 方案二'}}},parent,()=>{});
  `);
  assert.equal(env.document.querySelectorAll('.codex-plan').length, 1);
  assert.equal(env.run('parent.hidden'), false);
  assert.match(env.get('.codex-plan').querySelector('.agent-text').innerHTML, /方案二/);
  assert.equal(env.get('.execute-plan').disabled, true);
  env.run(`updatePlanActions(parent,true)`);
  assert.equal(env.get('.execute-plan').disabled, false);
  env.run(`renderCodexPlan({payload:{sessionUpdate:'plan_removed',planId:'p'}},parent,()=>{})`);
  assert.equal(env.get('.execute-plan'), null);
});

test('问题刷新复用表单，回答后拒绝旧 pending 快照复活', () => {
  const env = environment();
  env.run(`
    globalThis.parent = document.createElement('div'); document.body.append(parent);
    globalThis.questions = new Map();
    globalThis.record = {id:'q',run_id:'r',status:'pending',payload:{schema:{type:'object',properties:{q:{type:'string',title:'做哪一个？',oneOf:[{const:'A',title:'方案 A'}]}},required:['q']}}};
    renderQuestionCard(record,questions,parent,'s',()=>true);
    parent.querySelector('select').value='A';
    renderQuestionCard(record,questions,parent,'s',()=>true);
  `);
  assert.equal(env.get('.question-card').querySelector('select').value, 'A');
  env.run(`renderQuestionCard({...record,status:'answered',response:JSON.stringify({action:'accept',content:{q:'A'}})},questions,parent,'s',()=>true);
    renderQuestionCard(record,questions,parent,'s',()=>true);`);
  assert.equal(env.get('.question-card').querySelector('button'), null);
  assert.equal(env.run(`questions.get('q').status`), 'answered');
});

test('不同会话的 Plan 选择隔离，其他 Harness 不展示', () => {
  const env = environment();
  env.run(`globalThis.sessionStorage={getItem:key=>key==='agentsteam.mode.one'?'plan':null,setItem(){}};
    globalThis.one=collaborationSelector('one','codex',null);
    globalThis.two=collaborationSelector('two','codex',null);
    globalThis.other=collaborationSelector('one','opencode',null);`);
  assert.equal(env.run('one.value'), 'plan');
  assert.equal(env.run('two.value'), '');
  assert.equal(env.run('other.hidden'), true);
});
