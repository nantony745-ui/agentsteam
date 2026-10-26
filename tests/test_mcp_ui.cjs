const test = require('node:test');
const assert = require('node:assert/strict');
const { environment } = require('./frontend_dom.cjs');
const { addSkillsElements } = require('./skills_dom.cjs');

function setup(hash = '#settings/mcp?agent=codex') {
  const env = environment();
  const view = env.document.createElement('div'); view.id = 'settings-view'; env.document.body.append(view);
  addSkillsElements(env);
  env.window.location = {hash};
  env.run(`
    globalThis.calls=[]; globalThis.waiting=[]; globalThis.hold=false; globalThis.fail=false;
    globalThis.items=['codex','opencode','codebuddy','deepseek'].map(id=>({id,name:id}));
    fetch=async url=>{
      calls.push(url);
      if(hold) await new Promise(resolve=>waiting.push(resolve));
      if(fail) return {ok:false,json:async()=>({detail:'读取失败'})};
      const agent=url.split('/')[4];
      return {ok:true,json:async()=>({note:agent==='deepseek'?'通过 ACP 会话参数接收 MCP':'配置记录不代表连接',
        locations:[{path:'D:/'+agent+'/config',exists:true,error:null}],servers:agent==='deepseek'?[]:[
          {id:'one',name:agent+' files',path:'D:/'+agent+'/config',transport:'stdio',enabled:true,config:{command:'node',args:['<script>alert(1)</script>']}},
          {id:'two',name:'remote',path:'D:/'+agent+'/config',transport:'http',enabled:false,config:{url:'https://example.invalid'}}]})};
    };
    HarnessMcpUI.init(()=>{globalThis.retried=true});
  `);
  return env;
}
async function tick() { for (let i=0;i<30;i++) await Promise.resolve(); }
async function show(env,hash) { if(hash) env.window.location.hash=hash; await env.run('HarnessMcpUI.show(items)'); await tick(); }

test('MCP hash 恢复选项、配置安全只读展示；搜索无需新请求', async()=>{
  const env=setup('#settings/mcp?agent=codebuddy&server=two'); await show(env);
  assert.equal(env.get('#mcp-harness').value,'codebuddy');
  assert.equal(env.get('#mcp-title').textContent,'remote');
  assert.equal(env.get('#mcp-enabled').textContent,'配置停用');
  const count=env.run('calls.length');
  env.get('#mcp-search').value='stdio'; env.get('#mcp-search').emit('input');
  assert.equal(env.get('#mcp-count').textContent,'1 / 2');
  await show(env,'#settings/mcp?agent=codebuddy&server=one');
  assert.match(env.get('#mcp-config').textContent,/<script>/);
  assert.equal(env.get('#mcp-config').innerHTML,'');
  assert.equal(env.run('calls.length'),count);
});

test('迟到列表不覆盖新 Harness，隐藏后不展示旧数据', async()=>{
  const env=setup(); env.run('hold=true; globalThis.first=HarnessMcpUI.show(items)'); await tick();
  env.run('hold=false'); await show(env,'#settings/mcp?agent=opencode');
  env.run('waiting.shift()()'); await env.run('first');
  assert.equal(env.get('#mcp-title').textContent,'opencode files');
  env.run('hold=true'); env.get('#mcp-refresh').click(); await tick();
  env.run('HarnessMcpUI.hide(); waiting.shift()()'); await tick();
  assert.equal(env.get('#mcp-detail').hidden,true);
});

test('失败可刷新恢复，DeepSeek 显示原生全局来源限制', async()=>{
  const env=setup(); env.run('fail=true'); await show(env);
  assert.match(env.get('#mcp-error').textContent,/读取失败/);
  assert.equal(env.get('#mcp-refresh').disabled,false);
  env.run('fail=false'); env.get('#mcp-refresh').click(); await tick();
  assert.equal(env.get('#mcp-detail').hidden,false);
  await show(env,'#settings/mcp?agent=deepseek');
  assert.equal(env.get('#mcp-detail').hidden,true);
  assert.match(env.get('#mcp-placeholder').textContent,/ACP 会话参数/);
  assert.equal(env.get('#mcp-config').textContent,'');
});
