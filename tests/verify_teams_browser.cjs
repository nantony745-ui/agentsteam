// 真实 Chromium、HTTP/SSE 和临时 SQLite；不调用真实模型。
const {spawn, execFileSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsteam-teams-'));
const chromium = path.join(process.env.LOCALAPPDATA, 'ms-playwright/chromium-1228/chrome-win64/chrome.exe');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function port() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const value = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return value;
}

async function until(callback) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const result = await callback();
    if (result) return result;
    await delay(50);
  }
  throw new Error('浏览器验收等待超时');
}

async function run() {
  const repository = path.join(directory, 'repo');
  fs.mkdirSync(repository);
  const git = (...args) => execFileSync('git', args, {cwd: repository, windowsHide:true, stdio:'pipe'});
  git('init');
  git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'fixture');
  const serverPort = await port();
  const debugPort = await port();
  const base = `http://127.0.0.1:${serverPort}`;
  const server = spawn(path.join(root,'.venv/Scripts/python.exe'),
    ['tests/fixtures/teams_browser_app.py',directory,String(serverPort)],
    {cwd:root,windowsHide:true,env:{...process.env,PYTHONPATH:root},stdio:['ignore','ignore','pipe']});
  const errors = [];
  server.stderr.on('data', data => errors.push(String(data)));
  let chrome, socket;
  try {
    const fixture = await until(async () => {try {
      const result = await fetch(base + '/fixture');
      return result.ok && await result.json();
    } catch {return false;}});
    chrome = spawn(chromium,['--headless=new','--no-first-run','--disable-gpu',
      `--remote-debugging-port=${debugPort}`,`--user-data-dir=${path.join(directory,'profile')}`,'about:blank'],
      {windowsHide:true,stdio:'ignore'});
    const target = await until(async () => {try {
      return (await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()).find(item=>item.type==='page');
    } catch {return false;}});
    socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve);socket.addEventListener('error',reject);});
    let sequence = 0;
    const pending = new Map(), browserErrors = [];
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const request = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) request.reject(new Error(JSON.stringify(message.error)));
        else request.resolve(message.result);
      } else if (message.method === 'Runtime.exceptionThrown') browserErrors.push(message.params.exceptionDetails);
    });
    const send = (method,params={})=>new Promise((resolve,reject)=>{
      pending.set(++sequence,{resolve,reject});socket.send(JSON.stringify({id:sequence,method,params}));
    });
    const evaluate = async expression => {
      const result = await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
      if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const screenshot = async name => {
      const result = await send('Page.captureScreenshot',{format:'png'});
      const file = path.join(directory,name+'.png');fs.writeFileSync(file,Buffer.from(result.data,'base64'));return file;
    };
    await send('Runtime.enable');
    await send('Page.enable');
    await send('Page.addScriptToEvaluateOnNewDocument',{source:"window.fixtureSources=[];const NativeSource=EventSource;window.EventSource=class extends NativeSource{constructor(...args){super(...args);window.fixtureSources.push(this);}};"});
    await send('Emulation.setDeviceMetricsOverride',{width:1833,height:1000,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate',{url:base+'/'});


    const results = [];
    const teamRepo=path.join(directory,'team-repo'); fs.mkdirSync(teamRepo);
    for (const args of [['init'],['-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--allow-empty','-m','fixture']]) execFileSync('git',args,{cwd:teamRepo,windowsHide:true,stdio:'pipe'});
    await until(()=>evaluate("typeof TeamWorkspace!=='undefined' && state.projects.length===1"));
    await evaluate("location.hash='#settings/teams'");
    await until(()=>evaluate("document.querySelectorAll('#team-candidates .team-member-row').length===5"));
    assert.equal(await evaluate("document.querySelectorAll('#team-candidates button[disabled]').length"),1);
    await evaluate("document.querySelector('#team-name').value='产品研发团队';[...document.querySelectorAll('#team-candidates .team-member-add')].filter(button=>!button.disabled).forEach(button=>button.click())");
    assert.equal(await evaluate("document.querySelectorAll('#team-selected .team-member-row').length"),4);
    await evaluate("[...document.querySelectorAll('#team-selected .team-member-row')].find(row=>row.textContent.includes('林舟')).querySelector('.team-leader').click();document.querySelector('#team-form').requestSubmit()");
    await until(()=>evaluate("document.querySelector('#team-feedback').textContent.includes('已保存')"));
    await evaluate("document.querySelector('#team-candidates .team-member-info').click()");
    await until(()=>evaluate("document.querySelector('#team-capability').textContent.includes('Skills')"));
    results.push({view:'teams-settings',screenshot:await screenshot('teams-settings')});
    await evaluate("location.hash='#workbench';openProjectDialog()");
    await until(()=>evaluate("document.querySelector('#project-team').options.length===2"));
    await evaluate(`document.querySelector('#project-path').value=${JSON.stringify(teamRepo)};document.querySelector('#project-team').selectedIndex=1;document.querySelector('#project-team').dispatchEvent(new Event('change'));document.querySelector('#project-form').requestSubmit()`);
    await until(()=>evaluate("!document.querySelector('#team-workspace').hidden && document.querySelectorAll('.team-pane').length===1 && !document.querySelector('.team-pane textarea').disabled"));
    const projectId=await evaluate('state.projectId');
    const leader=(await (await fetch(base+`/api/projects/${projectId}/sessions`)).json()).find(item=>!item.parent_id);
    const team=(await (await fetch(base+`/api/sessions/${leader.id}/team`)).json());
    for(const employee of fixture.employees.filter(item=>item.employee_id!==team.team.leader_id).slice(0,2)) {
      await evaluate(`[...document.querySelectorAll('.team-member-tab')].find(button=>button.textContent.includes(${JSON.stringify(employee.name)})).click()`);
      await until(()=>evaluate(`document.querySelectorAll('.team-pane').length===${fixture.employees.filter(item=>item.employee_id!==team.team.leader_id).indexOf(employee)+2}`));
    }
    await until(()=>evaluate("[...document.querySelectorAll('.team-pane textarea')].every(input=>!input.disabled)"));
    assert.equal(await evaluate("document.querySelectorAll('.team-pane:not([hidden])').length"),3);
    assert.equal(await evaluate("[...document.querySelectorAll('.team-pane')].every(pane=>pane.getBoundingClientRect().width>=395)"),true);
    assert.equal(await evaluate("document.querySelector('.conversation').hidden"),true);
    await evaluate("document.querySelectorAll('.team-pane textarea').forEach((input,i)=>{input.value='独立草稿 '+i;input.dispatchEvent(new Event('input'))})");
    const sessionIds=await evaluate("[...document.querySelectorAll('.team-pane')].map(pane=>pane.dataset.sessionId)");
    await evaluate("document.querySelectorAll('.team-pane')[0].querySelector('.send-button').click()");
    await until(()=>evaluate("document.querySelectorAll('.team-pane')[0].querySelector('.user-message')?.textContent==='独立草稿 0'"));
    const updatedTeam=await(await fetch(base+`/api/sessions/${leader.id}/team`)).json();
    const workerId=updatedTeam.sessions.find(item=>item.id===sessionIds[1]).employee_id;
    const worker=team.members.find(item=>item.profile.employee_id===workerId).profile;
    const first=await(await fetch(base+'/fixture/delegate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({leader_id:leader.id,employee_id:worker.employee_id,goal:'定向核验',acceptance:'返回证据',request_id:crypto.randomUUID()})})).json();
    assert.equal(first.child_session_id,sessionIds[1]);
    await fetch(base+`/api/sessions/${sessionIds[2]}/messages`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:'第三窗独立执行',request_id:crypto.randomUUID()})});
    await until(()=>evaluate("document.querySelectorAll('.team-pane')[2].querySelector('.user-message')?.textContent==='第三窗独立执行'"));
    for(const [i,id] of sessionIds.entries()) await until(async()=>{
      const response=await fetch(base+'/fixture/emit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:id,text:'窗口隔离证据 '+i})});
      assert.equal(response.ok,true);
      return (await response.json()).ok;
    });
    await until(()=>evaluate("[...document.querySelectorAll('.team-pane')].every((pane,i)=>pane.textContent.includes('窗口隔离证据 '+i))"));
    assert.equal(await evaluate("document.querySelectorAll('.team-pane')[0].textContent.includes('窗口隔离证据 1')"),false);
    assert.equal(await evaluate("document.querySelectorAll('.team-pane')[1].textContent.includes('窗口隔离证据 2')"),false);
    assert.equal(await evaluate("[...document.querySelectorAll('.team-pane')].every(pane=>pane.querySelectorAll('.tool-card').length===1)"),true);
    assert.equal(await evaluate("document.querySelectorAll('.team-pane')[1].querySelector('textarea').value"),'独立草稿 1');
    await fetch(base+'/fixture/permission',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sessionIds[1]})});
    await until(()=>evaluate("document.querySelectorAll('.team-pane')[1].querySelector('.permission-card button')?.textContent==='允许本次'"));
    assert.equal(await evaluate("document.querySelectorAll('.team-pane')[0].querySelectorAll('.permission-card').length"),0);
    await evaluate("document.querySelectorAll('.team-pane')[1].querySelector('.permission-card button').click()");
    await until(()=>evaluate("document.querySelectorAll('.team-pane')[1].querySelector('.permission-card').textContent.includes('已回答')"));
    await evaluate("document.querySelectorAll('.team-pane')[2].querySelector('button[title=\"停止本轮\"]').click()");
    await until(async()=>['canceled','completed'].includes((await(await fetch(base+`/api/sessions/${sessionIds[2]}`)).json()).runs.at(-1).status));
    assert.equal((await(await fetch(base+`/api/runs/${first.run_id}`)).json()).status,'running');
    await evaluate("window.fixtureSources.at(-1).dispatchEvent(new Event('error'))");
    await until(()=>evaluate("document.querySelector('#team-workspace-status').textContent.includes('连接中断')"));
    await fetch(base+'/fixture/emit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sessionIds[0],text:'断线期间恢复证据'})});
    await until(()=>evaluate("[...document.querySelectorAll('.team-pane')[0].querySelectorAll('.agent-text')].some(node=>node.textContent.includes('断线期间恢复证据'))"));
    assert.equal(await evaluate("[...document.querySelectorAll('.team-pane')[0].querySelectorAll('.agent-text')].map(node=>node.textContent).join('').split('断线期间恢复证据').length-1"),1);
    await fetch(base+'/fixture/finish',{method:'POST'});
    await until(()=>evaluate("document.querySelectorAll('.team-pane')[0].textContent.includes('已完成核验')"));
    await evaluate("document.querySelectorAll('.team-pane')[0].querySelector('button[title=\"聚焦 / 恢复并排\"]').click()");
    assert.equal(await evaluate("document.querySelectorAll('.team-pane:not([hidden])').length"),1);
    await evaluate(`[...document.querySelectorAll('.team-member-tab')].find(button=>button.textContent.includes(${JSON.stringify(worker.name)})).click()`);
    assert.equal(await evaluate("document.querySelector('.team-pane:not([hidden])').dataset.sessionId"),sessionIds[1]);
    await evaluate("document.querySelectorAll('.team-pane')[1].querySelector('button[title=\"聚焦 / 恢复并排\"]').click()");
    assert.equal(await evaluate("document.querySelectorAll('.team-pane:not([hidden])').length"),3);
    results.push({view:'three-columns',screenshot:await screenshot('teams-three-columns')});
    await send('Page.reload');
    await until(()=>evaluate("document.querySelectorAll('.team-pane').length===3 && [...document.querySelectorAll('.team-pane textarea')].every(input=>!input.disabled)"));
    assert.equal(await evaluate("document.querySelectorAll('.team-pane')[1].querySelector('textarea').value"),'独立草稿 1');
    await evaluate("[...document.querySelectorAll('.team-member-tab')].find(button=>button.textContent.includes('苏禾')).click()");
    await until(()=>evaluate("document.querySelectorAll('.team-pane').length===3 && [...document.querySelectorAll('.team-pane strong')].some(item=>item.textContent.includes('苏禾'))"));
    await send('Emulation.setDeviceMetricsOverride',{width:1113,height:850,deviceScaleFactor:1,mobile:false});
    await until(()=>evaluate("document.querySelectorAll('.team-pane:not([hidden])').length<=2"));
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    results.push({view:'two-or-one-columns',screenshot:await screenshot('teams-medium')});
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await until(()=>evaluate("document.querySelectorAll('.team-pane:not([hidden])').length===1"));
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    results.push({view:'mobile',screenshot:await screenshot('teams-mobile')});
    await evaluate("document.querySelector('#open-employees').click()");
    await until(()=>evaluate(`!document.querySelector('#employees-view').hidden && [...document.querySelectorAll('#employee-list button')].some(button=>button.textContent.includes(${JSON.stringify(worker.name)}))`));
    assert.equal(await evaluate("getComputedStyle(document.querySelector('#team-workspace')).display"),'none');
    await evaluate(`[...document.querySelectorAll('#employee-list button')].find(button=>button.textContent.includes(${JSON.stringify(worker.name)})).click()`);
    await until(()=>evaluate(`document.querySelector('#employee-name').value===${JSON.stringify(worker.name)}`));
    await evaluate("document.querySelector('#employee-delete').click();document.querySelector('#employee-delete-form').requestSubmit()");
    await until(()=>evaluate("!document.querySelector('#employee-delete-dialog').open && document.querySelector('#team-member-tabs').textContent.includes('已被删除')"));
    assert.equal(await evaluate(`[...document.querySelectorAll('.team-member-tab')].some(button=>button.textContent.includes(${JSON.stringify(worker.name)}))`),false);
    await evaluate("document.querySelector('#employees-back').click()");
    await evaluate(`api('/api/sessions/${sessionIds[1]}').then(record=>TeamWorkspace.openSession(record.session))`);
    await until(()=>evaluate(`!!document.querySelector('.team-pane[data-session-id="${sessionIds[1]}"] button[title="队员已被删除"]')`));
    assert.equal(await evaluate(`document.querySelector('.team-pane[data-session-id="${sessionIds[1]}"] button[title="队员已被删除"]').disabled`),true);
    assert.equal((await fetch(base+`/api/sessions/${sessionIds[1]}/history`)).status,200);
    results.push({view:'deleted-member-history',screenshot:await screenshot('teams-deleted-member-history')});
    await evaluate("location.hash='#settings/teams'");
    await until(()=>evaluate("!document.querySelector('#settings-teams-panel').hidden"));
    await until(()=>evaluate("document.querySelector('#team-selected').textContent.includes('队员已被删除')"));
    assert.equal(await evaluate(`document.querySelector('#team-candidates').textContent.includes(${JSON.stringify(worker.name)})`),false);
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    assert.deepEqual(browserErrors,[]);
    console.log(JSON.stringify({result:'passed',directory,results,browserErrors},null,2));
  } finally {
    socket?.close();chrome?.kill();server.kill();
    if(errors.length) console.error(errors.join(''));
  }
}
run().then(()=>process.exit(0),error=>{console.error(error);process.exit(1);});
