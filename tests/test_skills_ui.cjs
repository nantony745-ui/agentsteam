const test = require('node:test');
const assert = require('node:assert/strict');
const { environment } = require('./frontend_dom.cjs');
const { addSkillsElements } = require('./skills_dom.cjs');

function setup(hash = '#settings/skills?agent=codex') {
  const env = environment();
  const view = env.document.createElement('div'); view.id='settings-view'; env.document.body.append(view);
  addSkillsElements(env);
  env.window.location = {hash};
  env.run(`
    globalThis.calls=[]; globalThis.waiting=[]; globalThis.hold=false; globalThis.fail=false; globalThis.contentVersion=1;
    globalThis.items=[{id:'codex',name:'Codex'},{id:'deepseek',name:'DeepSeek'}];
    fetch=async url=>{
      calls.push(url);
      const captured=contentVersion;
      if(hold) await new Promise(resolve=>waiting.push(resolve));
      if(fail) return {ok:false,json:async()=>({detail:'文件已消失'})};
      const agent=url.includes('/deepseek/')?'DeepSeek':'Codex';
      if(url.includes('/skills/')) return {ok:true,json:async()=>({skill:{name:agent+' 技能',path:'D:/skills/'+agent+'/SKILL.md'},metadata:'name: '+agent,body:'# '+agent+' v'+captured+'\\n\\n**预览** <script>alert(1)</script> [危险](javascript:alert(1))'})};
      return {ok:true,json:async()=>({locations:[{path:'D:/skills/'+agent,exists:true}],skills:[
        {id:'one',slug:'alpha',name:agent+' 技能',description:'读取说明',path:'D:/skills/'+agent+'/SKILL.md'},
        {id:'two',slug:'beta',name:'第二个技能',description:'处理文件',path:'D:/skills/second/SKILL.md'}]})};
    };
    HarnessSkillsUI.init();
  `);
  return env;
}

async function tick() { for(let i=0;i<30;i++) await Promise.resolve(); }
async function show(env,hash) { if(hash) env.window.location.hash=hash; await env.run('HarnessSkillsUI.show(items)'); await tick(); }

test('hash 恢复 Harness 和选中项，Markdown 安全渲染且仅发 GET', async()=>{
  const env=setup('#settings/skills?agent=deepseek&skill=two');
  await show(env);
  assert.equal(env.get('#skills-harness').value,'deepseek');
  assert.equal(env.get('#skills-list').children[1].getAttribute('aria-current'),'true');
  assert.match(env.get('#skill-title').textContent,/DeepSeek/);
  assert.match(env.get('#skill-markdown').innerHTML,/<h1>DeepSeek v1<\/h1>/);
  assert.match(env.get('#skill-markdown').innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(env.get('#skill-markdown').innerHTML,/<script>|href="javascript:/);
  assert.equal(env.document.querySelector('textarea'),env.get('#message-input'));
  assert.ok(env.run("calls.some(url=>url.endsWith('/two'))"));
});

test('按名称、描述、ID 和路径搜索，计数随筛选变化且不重新请求',async()=>{
  const env=setup(); await show(env); const count=env.run('calls.length');
  for(const term of ['beta','处理文件','second']) {
    env.get('#skills-search').value=term; env.get('#skills-search').emit('input');
    assert.equal(env.get('#skills-list').children.length,1);
    assert.equal(env.get('#skills-count').textContent,'1 / 2');
  }
  env.get('#skills-search').value='无匹配'; env.get('#skills-search').emit('input');
  assert.match(env.get('#skills-list').children[0].textContent,/没有匹配/);
  assert.equal(env.run('calls.length'),count);
});

test('Harness 选择切换全局列表，请求不依赖项目',async()=>{
  const env=setup(); await show(env);
  env.get('#skills-harness').value='deepseek'; env.get('#skills-harness').emit('change');
  await show(env);
  assert.match(env.get('#skill-title').textContent,/DeepSeek/);
  assert.ok(env.run("calls.every(url=>url.startsWith('/api/settings/harnesses/'))"));
});

test('快速切换 Harness 的旧列表响应不能恢复旧技能',async()=>{
  const env=setup(); env.run('hold=true; globalThis.first=HarnessSkillsUI.show(items)'); await tick();
  const count=env.run('waiting.length'); assert.equal(count,1);
  env.run('hold=false');
  await show(env,'#settings/skills?agent=deepseek');
  env.run('waiting.shift()()'); await env.run('first');
  assert.match(env.get('#skill-title').textContent,/DeepSeek/);
  assert.match(env.get('#skills-list').children[0].querySelector('strong').textContent,/DeepSeek/);
});

test('迟到详情及隐藏后的响应不覆盖新预览，工作台草稿与 SSE 保留',async()=>{
  const env=setup(); await show(env);
  env.run("state.context={sessionId:'s'}; state.source={close(){throw Error('SSE 不应关闭')}}; hold=true;");
  env.get('#message-input').value='草稿';
  env.window.location.hash='#settings/skills?agent=codex&skill=two';
  env.run('globalThis.previous=HarnessSkillsUI.show(items)'); await tick();
  env.run('hold=false'); await show(env,'#settings/skills?agent=deepseek');
  env.run('waiting.shift()()'); await env.run('previous');
  assert.match(env.get('#skill-title').textContent,/DeepSeek/);
  env.run('hold=true'); env.get('#skills-refresh').click(); await tick();
  env.run('HarnessSkillsUI.hide(); waiting.shift()()'); await tick();
  assert.equal(env.get('#skill-detail').hidden,true);
  assert.equal(env.get('#message-input').value,'草稿');
  assert.equal(env.run('state.context.sessionId'),'s');
});

test('刷新重新读取相同技能内容，读取失败显示重试并可恢复',async()=>{
  const env=setup(); await show(env);
  env.run('contentVersion=2'); env.get('#skills-refresh').click(); await tick();
  assert.match(env.get('#skill-markdown').innerHTML,/v2/);
  env.run('fail=true'); env.get('#skills-refresh').click(); await tick();
  assert.match(env.get('#skills-error').textContent,/文件已消失/);
  assert.equal(env.get('#skill-detail').hidden,true);
  env.run('fail=false'); env.get('#skills-refresh').click(); await tick();
  assert.equal(env.get('#skill-detail').hidden,false);
});
