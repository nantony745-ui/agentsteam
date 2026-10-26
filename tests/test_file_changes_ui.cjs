const assert = require('node:assert/strict');
const {test} = require('node:test');
const {environment} = require('./frontend_dom.cjs');
const tick = () => new Promise(resolve => setImmediate(resolve));

function setup() {
  const env = environment();
  env.run(`
    state.context = {generation:0,projectId:'p',sessionId:'s',lastEventId:0};
    const context = state.context;
    historyContext(context);
    const sample = {additions:12,deletions:3,files:[
      {path:'plan/中文 space.md',operation:'modified',additions:10,deletions:3,has_diff:true,previewable:true},
      {path:'new.txt',operation:'added',additions:2,deletions:0,has_diff:true,previewable:true}
    ]};
    const calls = [];
    api = async (url, options) => { calls.push({url, options}); return sample; };
    refreshRun = async () => {}; refreshSessionNativeId = () => {};
    refreshTaskChanges = () => {}; scheduleSidebarRefresh = () => {};
    let sequence = 0;
    const event = (kind, payload) => applyEvent({id:++sequence,project_id:'p',session_id:'s',run_id:'r',kind,payload},context);
    event('user.message',{message:'修改文件'});
  `);
  return env;
}

test('终态后显示卡片并放在最终回复之后，重复终态只请求一次，折叠状态保留', async () => {
  const env = setup();
  assert.equal(env.get('.run-file-changes').hidden, true);
  assert.equal(env.run('calls.length'), 0);
  env.run(`event('agent.text',{channel:'message',text:'已修改'});event('run.status',{status:'completed'});`);
  await tick();
  assert.equal(env.get('.run-file-changes').hidden, false);
  assert.equal(env.document.querySelectorAll('.reply-file-card').length, 2);
  assert.equal(env.get('.reply-files').open, true);
  const parent = env.get('.run-content');
  assert.ok(parent.children.indexOf(env.get('.run-answer')) < parent.children.indexOf(env.get('.run-file-changes')));
  assert.equal(env.get('.file-count').textContent, '2 个文件');
  assert.equal(env.get('.file-additions').textContent, '+12');
  assert.equal(env.get('.file-deletions').textContent, '−3');
  assert.equal(env.get('.reply-file-name').textContent, '中文 space.md');
  assert.equal(env.get('.reply-file-directory').textContent, 'plan');
  env.get('.reply-files').open = false;
  env.run(`event('run.status',{status:'completed'});`);
  await tick();
  assert.equal(env.run('calls.length'), 1);
  assert.equal(env.get('.reply-files').open, false);
});

test('空列表隐藏；失败或取消回合仍显示成功修改，未知统计和删除文件清晰可见', async () => {
  const empty = setup();
  empty.run(`api=async()=>({files:[],additions:0,deletions:0});event('run.status',{status:'completed'});`);
  await tick();
  assert.equal(empty.get('.run-file-changes').hidden, true);
  for (const status of ['failed','canceled']) {
    const env = setup();
    env.run(`sample.files[0]={path:'gone.txt',operation:'deleted',additions:null,deletions:null,has_diff:true,previewable:false};
      sample.additions=null;sample.deletions=null;event('run.status',{status:'${status}'});`);
    await tick();
    assert.equal(env.get('.reply-file-open').disabled, true);
    assert.equal(env.get('.reply-file-diff').disabled, false);
    assert.equal(env.get('.file-additions').textContent, '—');
    assert.equal(env.get('.file-stats-note').textContent, '统计不完整');
    assert.equal(env.get('.file-operation').textContent, '删除');
  }
});

test('文件列表请求合并，局部错误支持重试', async () => {
  const env = setup();
  env.run(`api=async()=>{throw new Error('暂时离线')};event('run.status',{status:'completed'});`);
  await tick();
  assert.match(env.get('.run-file-changes').children[0].textContent, /暂时离线/);
  env.run(`api=async url=>{calls.push(url);return new Promise(resolve=>globalThis.resolveList=resolve)};
    document.querySelector('.run-file-changes').querySelector('button').click();
    refreshRunFileChanges(state.runs.get('r'),context);`);
  assert.equal(env.run('calls.length'), 1);
  env.run('resolveList(sample)');
  await tick();
  assert.equal(env.document.querySelectorAll('.reply-file-card').length, 2);
});

test('Diff 和当前内容分别按需读取，路径编码、颜色、纯文本和关闭行为', async () => {
  const env = setup();
  env.run(`event('run.status',{status:'completed'});`);
  await tick();
  assert.equal(env.run('calls.length'), 1);
  env.run(String.raw`api=async(url,options)=>{calls.push({url,options});return {diff:'--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+<script>bad</script>',truncated:false}};
    document.querySelector('.reply-file-diff').click();`);
  await tick();
  assert.equal(env.get('#file-view-dialog').open, true);
  assert.match(env.get('.diff-added').textContent, /<script>bad/);
  assert.equal(env.get('.diff-added').innerHTML, '');
  assert.match(env.get('#file-view-title').textContent, /本轮差异/);
  assert.equal(env.run(`new URLSearchParams(calls[1].url.split('?')[1]).get('path')`), 'plan/中文 space.md');
  env.get('#file-view-close').click();
  assert.equal(env.get('#file-view-dialog').open, false);
  env.run(`api=async()=>({kind:'text',content:'<img src=x>当前',reason:null});document.querySelector('.reply-file-open').click();`);
  await tick();
  assert.match(env.get('#file-view-title').textContent, /当前内容（只读）/);
  assert.equal(env.get('#file-view-content').textContent, '<img src=x>当前');
  env.get('#file-view-dialog').emit('cancel');
  assert.equal(env.get('#file-view-dialog').open, false);
});

test('预览错误可重试，二进制和截断原因可见', async () => {
  const env = setup();
  env.run(`event('run.status',{status:'completed'});`);
  await tick();
  env.run(`api=async()=>{throw new Error('文件已删除')};document.querySelector('.reply-file-open').click();`);
  await tick();
  assert.match(env.get('#file-view-message').textContent, /文件已删除/);
  env.run(`api=async()=>({kind:'binary',content:null,reason:'内容包含 NUL 字节'});document.querySelector('#file-view-content').querySelector('button').click();`);
  await tick();
  assert.equal(env.get('#file-view-message').textContent, '内容包含 NUL 字节');
  await env.run(`api=async()=>({diff:'+部分内容',truncated:true,reason:'差异超过 1 MiB，已截断'});openFileView(state.runs.get('r'),sample.files[0],'diff',context)`);
  assert.match(env.get('#file-view-message').textContent, /截断/);
});

test('切换会话和淘汰历史轮次取消请求，迟到响应不写入新视图', async () => {
  for (const cleanup of ['clearConversationDom()', "evictRun(context,'r')"]) {
    const env = setup();
    env.run(`api=async(url,options)=>{globalThis.signal=options.signal;return new Promise(resolve=>globalThis.finish=resolve)};
      event('run.status',{status:'completed'});${cleanup};finish(sample);`);
    await tick();
    assert.equal(env.run('signal.aborted'), true);
    assert.equal(env.get('.reply-file-card'), null);
  }
});

test('关闭弹窗、切换文件和会话时取消详情，迟到响应不覆盖', async () => {
  const env = setup();
  env.run(`event('run.status',{status:'completed'});`);
  await tick();
  env.run(`api=async(url,options)=>{globalThis.oldSignal=options.signal;return new Promise(resolve=>globalThis.finish=resolve)};
    document.querySelector('.reply-file-diff').click();document.querySelector('#file-view-close').click();finish({diff:'+迟到'});`);
  await tick();
  assert.equal(env.run('oldSignal.aborted'), true);
  assert.equal(env.get('#file-view-content').children.length, 0);
  env.run(`document.querySelector('.reply-file-diff').click();globalThis.firstSignal=oldSignal;globalThis.finishFirst=finish;
    api=async()=>({kind:'text',content:'最新文件'});document.querySelector('.reply-file-open').click();finishFirst({diff:'+过时'});`);
  await tick();
  assert.equal(env.run('firstSignal.aborted'), true);
  assert.equal(env.get('#file-view-content').textContent, '最新文件');
  env.run('clearConversationDom()');
  assert.equal(env.get('#file-view-dialog').open, false);
});

test('历史结束后从相同接口恢复卡片，不读取当前 Git 状态', async () => {
  const env = setup();
  await env.run(`api=async url=>url.startsWith('/api/events')?[]:sample;
    readRunHistory(context,[{id:'r',session_id:'s',status:'completed'}],10,false)`);
  await tick();
  assert.equal(env.document.querySelectorAll('.reply-file-card').length, 2);
  assert.equal(env.run("state.runs.get('r').fileChangesLoaded"), true);
});
