// Capture actual app pages and local artifacts via Chromium's DevTools protocol.
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const root=path.resolve(__dirname,'../..');
const runtime=process.argv[2];
const base=process.argv[3] || 'http://127.0.0.1:8876';
const executable=process.argv[4] || process.env.CHROME_EXECUTABLE;
if(!runtime || !executable)throw Error('Usage: node capture.cjs <runtime-directory> <base-url> <chromium-executable>');
const out=path.join(root,'docs/images');
const delay=ms=>new Promise(r=>setTimeout(r,ms));
async function until(f){const end=Date.now()+25000;while(Date.now()<end){try{const v=await f();if(v)return v;}catch{}await delay(150);}throw Error('Page readiness timed out');}
async function main(){
  const listener=net.createServer();await new Promise(r=>listener.listen(0,'127.0.0.1',r));const port=listener.address().port;await new Promise(r=>listener.close(r));
  const chrome=spawn(executable,['--headless=new','--no-first-run','--disable-gpu',`--remote-debugging-port=${port}`,`--user-data-dir=${path.join(runtime,'browser-profile')}`,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let socket;
  const errors=[],writes=[],screens=[];
  try {
    const target=await until(async()=>(await(await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t=>t.type==='page'));
    socket=new WebSocket(target.webSocketDebuggerUrl);await new Promise((r,j)=>{socket.addEventListener('open',r);socket.addEventListener('error',j);});
    let id=0;const pending=new Map();
    socket.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result);}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails);else if(m.method==='Network.requestWillBeSent'&&!['GET','HEAD','OPTIONS'].includes(m.params.request.method))writes.push(m.params.request.url);});
    const send=(method,params={})=>new Promise((resolve,reject)=>{pending.set(++id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
    const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
    await send('Runtime.enable');await send('Network.enable');await send('Page.enable');
    await send('Emulation.setDeviceMetricsOverride',{width:1920,height:1200,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate',{url:base+'/'});
    await until(()=>evaluate('typeof state!=="undefined" && state.projects.length===4'));
    const metadata=JSON.parse(fs.readFileSync(path.join(runtime,'metadata.json'),'utf8'));
    const capture=async name=>{
      await evaluate('document.fonts.ready');await delay(700);
      const untranslated=await evaluate(`(()=>{const w=document.createTreeWalker(document.documentElement,NodeFilter.SHOW_TEXT);let n;const a=[];while(n=w.nextNode()){if(/[\\u4e00-\\u9fff]/.test(n.data)&&n.parentElement.checkVisibility({checkVisibilityCSS:true})&&n.parentElement.getBoundingClientRect().bottom>0&&n.parentElement.getBoundingClientRect().top<innerHeight)a.push(n.data);}for(const e of document.querySelectorAll('input,textarea'))if(e.checkVisibility()&&/[\\u4e00-\\u9fff]/.test(e.value))a.push(e.value);return [...new Set(a)];})()`);
      const data=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,name+'.png'),Buffer.from(data.data,'base64'));
      screens.push({name,untranslated});console.log(JSON.stringify(screens.at(-1)));
    };
    const open=async(index,count)=>{
      const m=metadata[index];const ids=m.sessions.slice(0,count).map(s=>s.id);
      await evaluate(`localStorage.setItem(${JSON.stringify('agentsteam.team.layout.'+m.project.id)},${JSON.stringify(JSON.stringify({leader:ids[0],sessions:ids,active:ids[0]}))});location.hash='#workbench'`);
      await evaluate(`selectProject(${JSON.stringify(m.project.id)})`);
      await until(()=>evaluate(`document.querySelectorAll('#team-pane-grid .team-pane').length===${count}`));
      await delay(600);
      await evaluate(`document.querySelectorAll('.team-pane .timeline').forEach(e=>e.scrollTop=0)`);
    };
    await open(0,3);await capture('team-workspace');
    await evaluate("document.querySelector('.team-pane-file button').click()");
    await until(()=>evaluate("document.querySelector('.team-file-dialog')?.open"));
    await capture('file-preview');
    await evaluate("document.querySelector('.team-file-dialog button').click()");
    await open(2,1);await capture('workbench');
    await evaluate("document.querySelectorAll('.team-pane details.work-log').forEach(e=>e.open=true);document.querySelector('.team-pane .timeline').scrollTop=0");
    await capture('activity-log');
    await open(1,2);await capture('research-workspace');
    await open(3,2);await capture('learning-workspace');
    await evaluate("location.hash='#employees'");
    await until(()=>evaluate("document.querySelectorAll('#employee-list button').length===10"));
    await evaluate("[...document.querySelectorAll('#employee-list button')].find(e=>e.textContent.includes('Research Lead')).click()");
    await until(()=>evaluate("document.querySelector('#employee-name').value==='Research Lead'"));
    await evaluate("document.querySelector('#employee-instructions').style.height='480px'");
    await capture('employees');
    await evaluate("[...document.querySelectorAll('#employee-skills-list button')].find(e=>e.textContent.includes('evidence-handoff')).click()");
    await until(()=>evaluate("document.querySelector('#employee-skill-content').textContent.includes('Evidence-based handoff')"));
    await evaluate("document.querySelector('#employee-skills-section').scrollIntoView({block:'start'})");
    await capture('personal-skills');
    await evaluate("location.hash='#settings/teams'");
    await until(()=>evaluate("document.querySelectorAll('#team-list button').length===4"));
    await evaluate("[...document.querySelectorAll('#team-list button')].find(e=>e.textContent.includes('Research & Evidence')).click()");
    await delay(500);
    await evaluate("document.querySelector('#team-selected .team-member-info').click()");
    await delay(500);await capture('teams');
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
    for(const [name,url] of [['operations-dashboard','northstar-operations/dashboard.html'],['product-page','atlas-product-launch/index.html'],['workshop-map','learning-path-workshop/concept-map.svg']]){
      if(name==='workshop-map')await send('Emulation.setDeviceMetricsOverride',{width:1100,height:430,deviceScaleFactor:1,mobile:false});
      await send('Page.navigate',{url:base+'/artifacts/'+url});await delay(800);await capture(name);
    }
    fs.writeFileSync(path.join(runtime,'browser-verification.json'),JSON.stringify({errors,writes,screens},null,2));
    if(errors.length||writes.length||screens.some(s=>s.untranslated.length))throw Error('Review browser-verification.json');
  }finally{socket?.close();chrome.kill();}
}
main().then(()=>process.exit(0),e=>{console.error(e);process.exit(1);});
