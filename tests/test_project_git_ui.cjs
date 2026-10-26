const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const {environment} = require('./frontend_dom.cjs');

function setup() {
  const env = environment();
  const root = env.document.createElement('div');
  root.id = 'project-git';
  env.get('#message-form').append(root);
  for (const id of ['toggle','label','panel','message','path','root','relative','head','changes','init','refresh','close']) {
    const element = env.document.createElement(['toggle','init','refresh','close'].includes(id) ? 'button' : 'div');
    element.id = `project-git-${id}`;
    root.append(element);
  }
  env.run(fs.readFileSync(path.join(__dirname,'../app/static/project-git.js'),'utf8'));
  env.run(`$('#project-git-panel').showPopover = () => {}; $('#project-git-panel').hidePopover = () => {};`);
  env.run('ProjectGitUI.bind();');
  return env;
}

test('普通目录提供显式初始化，空仓库显示分支和未跟踪文件', async () => {
  const env = setup();
  await env.run(`(async () => {
    const calls = [];
    globalThis.fetch = async (url, options) => {
      calls.push({url, options});
      return {ok:true,json:async()=>({is_repo:options.method==='POST', branch:'main', has_commits:false, root:'D:/项目',relative_path:'.',changes:[{status:'??',path:'中文.txt'}]})};
    };
    await ProjectGitUI.setContext({projectId:'p',sessionId:null,path:'D:/项目'});
    if ($('#project-git-label').textContent !== '未初始化 Git' || $('#project-git-init').hidden) throw new Error('普通目录状态不正确');
    if (calls.some(call=>call.options.method==='POST')) throw new Error('读取状态不能自动初始化');
    await ProjectGitUI.refresh(true);
    if ($('#project-git-label').textContent !== 'main' || !$('#project-git-message').textContent.includes('尚无提交') || !$('#project-git-init').hidden) throw new Error('空仓库状态不正确');
    if ($('#project-git-relative').textContent !== '仓库根目录' || $('#project-git-changes').children[0].children[1].textContent !== '中文.txt') throw new Error('路径或文件状态丢失');
    if (calls.at(-1).url !== '/api/projects/p/git/init' || calls.at(-1).options.method !== 'POST') throw new Error('初始化请求不正确');
  })()`);
});

test('切换项目及初始化的迟到结果不能覆盖新目录，按会话绑定请求', async () => {
  const env = setup();
  await env.run(`(async () => {
    let finish;
    globalThis.fetch = async()=>({ok:true,json:async()=>({is_repo:false,changes:[]})});
    await ProjectGitUI.setContext({projectId:'p',sessionId:null,path:'D:/旧目录'});
    globalThis.fetch = ()=>new Promise(resolve=>{finish=resolve;});
    const old = ProjectGitUI.refresh(true);
    let url;
    globalThis.fetch = async value=>{url=value;return {ok:true,json:async()=>({is_repo:true,branch:'new',has_commits:true,changes:[]})};};
    await ProjectGitUI.setContext({projectId:'q',sessionId:'s',path:'D:/新目录'});
    finish({ok:true,json:async()=>({is_repo:true,branch:'old',changes:[]})}); await old;
    if ($('#project-git-label').textContent !== 'new' || $('#project-git-path').textContent !== 'D:/新目录') throw new Error('旧初始化覆盖了新项目');
    if (url !== '/api/projects/q/git?session_id=s') throw new Error('请求未关联会话目录');
    await ProjectGitUI.setContext({projectId:null,sessionId:null,path:''});
    if (!$('#project-git').hidden || !$('#project-git-panel').hidden) throw new Error('无项目时仍显示 Git 入口');
  })()`);
});

test('检查失败显示错误并支持刷新，游离 HEAD 有独立展示', async () => {
  const env = setup();
  await env.run(`(async () => {
    globalThis.fetch=async()=>({ok:false,json:async()=>({detail:'Git 读取失败'})});
    await ProjectGitUI.setContext({projectId:'p',sessionId:null,path:'D:/项目'});
    if ($('#project-git-label').textContent !== 'Git 检查失败' || !$('#project-git-init').hidden || $('#project-git-refresh').disabled) throw new Error('错误被误当成普通目录');
    globalThis.fetch=async()=>({ok:true,json:async()=>({is_repo:true,detached:true,short_sha:'123abcd',has_commits:true,changes:[]})});
    await ProjectGitUI.refresh();
    if (!$('#project-git-label').textContent.includes('游离 HEAD · 123abcd') || $('#project-git-message').textContent !== '工作区干净') throw new Error('游离 HEAD 未恢复');
    $('#project-git-panel').hidden=false;
    $('#project-git-toggle').setAttribute('aria-expanded','true');
    $('#project-git').emit('keydown',{key:'Escape'});
    if (!$('#project-git-panel').hidden || $('#project-git-toggle').getAttribute('aria-expanded') !== 'false') throw new Error('Escape 不能关闭面板');
  })()`);
});
