const {spawn,execFileSync}=require('node:child_process');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),net=require('node:net'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),directory=fs.mkdtempSync(path.join(os.tmpdir(),'agentsteam-plan-'));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn){const end=Date.now()+20000;while(Date.now()<end){const value=await fn();if(value)return value;await sleep(50);}throw Error('等待超时');}
async function port(){const s=net.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const p=s.address().port;await new Promise(r=>s.close(r));return p;}
async function main(){
  const repo=path.join(directory,'repo');fs.mkdirSync(repo);execFileSync('git',['init',repo],{windowsHide:true,stdio:'pipe'});
  const p=await port(),debug=await port(),base=`http://127.0.0.1:${p}`;
  const server=spawn(process.env.AGENTSTEAM_TEST_PYTHON || path.join(root,'.venv/Scripts/python.exe'),['tests/fixtures/plan_browser_app.py',directory,String(p)],{cwd:root,windowsHide:true,env:{...process.env,PYTHONPATH:root},stdio:['ignore','ignore','pipe']});
  let chrome,socket;const errors=[];server.stderr.on('data',x=>errors.push(String(x)));
  try{
    await until(async()=>{try{return (await fetch(base+'/api/projects')).ok;}catch{return false;}});
    const post=async(url,data)=>(await fetch(base+url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})).json();
    const project=await post('/api/projects',{path:repo}),session=await post('/api/sessions',{project_id:project.id,harness:'codex'});
    chrome=spawn(path.join(process.env.LOCALAPPDATA,'ms-playwright/chromium-1228/chrome-win64/chrome.exe'),['--headless=new','--no-first-run',`--remote-debugging-port=${debug}`,`--user-data-dir=${path.join(directory,'profile')}`,'about:blank'],{windowsHide:true,stdio:'ignore'});
    const target=await until(async()=>{try{return (await (await fetch(`http://127.0.0.1:${debug}/json/list`)).json()).find(x=>x.type==='page');}catch{return false;}});
    socket=new WebSocket(target.webSocketDebuggerUrl);await new Promise(r=>socket.addEventListener('open',r));
    let seq=0;const calls=new Map(),browserErrors=[];
    socket.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const c=calls.get(m.id);calls.delete(m.id);m.error?c.reject(Error(JSON.stringify(m.error))):c.resolve(m.result);}else if(m.method==='Runtime.exceptionThrown')browserErrors.push(m.params.exceptionDetails);});
    const send=(method,params={})=>new Promise((resolve,reject)=>{calls.set(++seq,{resolve,reject});socket.send(JSON.stringify({id:seq,method,params}));});
    const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
    await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});await send('Page.navigate',{url:base+'/'});
    await until(()=>evaluate("typeof state!=='undefined' && state.projects.length>0"));
    await evaluate(`selectProject('${project.id}'); selectSession('${session.id}')`);
    await until(()=>evaluate("!!document.querySelector('#collaboration-mode') && state.context?.ready"));
    await evaluate("document.querySelector('#collaboration-mode').value='plan';document.querySelector('#collaboration-mode').dispatchEvent(new Event('change'));document.querySelector('#message-input').value='帮我规划';document.querySelector('#message-form').requestSubmit()");
    await until(()=>evaluate("!!document.querySelector('.question-card input[type=radio]')"));
    assert.equal(await evaluate("document.querySelector('.question-card').checkVisibility()"),true);
    assert.equal(await evaluate("document.querySelector('.question-submit').disabled"),true);
    const pendingDesktop=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(directory,'question-desktop.png'),Buffer.from(pendingDesktop.data,'base64'));
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    const pendingMobile=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(directory,'question-mobile.png'),Buffer.from(pendingMobile.data,'base64'));
    await send('Page.reload');await until(()=>evaluate("!!document.querySelector('.question-card input[type=radio]')"));
    assert.equal(await evaluate("document.querySelector('#collaboration-mode').value"),'plan');
    await evaluate("document.querySelector('.question-card input[type=radio]').focus()");
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'ArrowDown',code:'ArrowDown',windowsVirtualKeyCode:40});
    await send('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowDown',code:'ArrowDown',windowsVirtualKeyCode:40});
    assert.equal(await evaluate("document.querySelector('.question-card input[type=radio]:checked').value"),'B');
    await evaluate("document.querySelector('.question-card input[type=radio]').click();document.querySelector('.question-card textarea').value='保留我的说明';document.querySelector('.question-card textarea').dispatchEvent(new Event('input'));document.querySelector('.question-next').click();document.querySelector('.question-card form').requestSubmit()");
    await until(()=>evaluate("!!document.querySelector('.execute-plan:not(:disabled)')"));
    assert.equal(await evaluate("document.querySelector('.execute-plan').checkVisibility()"),true);
    assert.equal(await evaluate("document.querySelector('.codex-plan').textContent.includes('方案 A')"),true);
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await sleep(100);assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    const capture=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(directory,'plan-mobile.png'),Buffer.from(capture.data,'base64'));
    await evaluate("document.querySelector('.execute-plan').click()");
    await until(()=>evaluate("document.querySelector('#timeline').textContent.includes('执行模式')"));
    assert.equal(await evaluate("document.querySelector('#collaboration-mode').value"),'default');
    // 同一共享组件在团队窗格通过真实 HTTP/SSE 继续走完整问答。
    const second=await post('/api/sessions',{project_id:project.id,harness:'codex'});
    await evaluate(`globalThis.planPane=new TeamConversation(${JSON.stringify(second)},{profile:{employee_id:'member',name:'规划员工'}},{project:{team:{leader_id:'member'}},roster:{sessions:[{...${JSON.stringify(second)},employee_id:'member'}]},activate(){},file(){}});document.querySelector('main').hidden=true;document.body.append(planPane.root);globalThis.planSource=new EventSource('/api/stream?project_id=${project.id}&session_id=${second.id}');planSource.addEventListener('lab',e=>planPane.receive(JSON.parse(e.data)));`);
    await until(()=>evaluate('!planPane.loading'));
    await evaluate("planPane.mode.value='plan';planPane.input.value='团队规划';planPane.send()");
    // 项目 SSE 使用具名事件；显式补拉也覆盖事件读取与展示。
    await until(async()=>{await evaluate(`api('/api/events?project_id=${project.id}&session_id=${second.id}&after_event_id='+planPane.cursor).then(async rows=>{for(const row of rows)await planPane.receive(row)})`);return evaluate("!!planPane.root.querySelector('.question-card input[type=radio]')");});
    await evaluate("planPane.root.querySelector('.question-card input[type=radio]').click();planPane.root.querySelector('.question-next').click();planPane.root.querySelector('.question-card form').requestSubmit()");
    await until(async()=>{await evaluate(`api('/api/events?project_id=${project.id}&session_id=${second.id}&after_event_id='+planPane.cursor).then(async rows=>{for(const row of rows)await planPane.receive(row)})`);return evaluate("!!planPane.root.querySelector('.execute-plan:not(:disabled)')");});
    assert.deepEqual(browserErrors,[]);console.log(JSON.stringify({result:'passed',directory,browserErrors}));
  }finally{socket?.close();chrome?.kill();server.kill();if(errors.length)console.error(errors.join(''));}
}
main().then(()=>process.exit(0),e=>{console.error(e);process.exit(1);});
