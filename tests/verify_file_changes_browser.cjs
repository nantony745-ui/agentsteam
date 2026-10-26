// 真实 Chromium、HTTP/SSE 和临时 SQLite；不调用真实模型。
const {spawn, execFileSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsteam-file-cards-'));
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
    ['tests/fixtures/file_changes_browser_app.py',directory,String(serverPort)],
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
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate',{url:base+'/'});
    await until(()=>evaluate("typeof state!=='undefined' && state.projects.length===1 && state.sessions.length===1"));
    await evaluate(`selectProject(${JSON.stringify(fixture.project_id)});selectSession(${JSON.stringify(fixture.session_id)})`);
    await until(()=>evaluate("state.context?.ready && state.source?.readyState===1"));
    assert.equal(await evaluate("document.querySelectorAll('.reply-file-card').length"),0);
    await fetch(base+'/fixture/finish',{method:'POST'});
    await until(()=>evaluate("document.querySelectorAll('.reply-file-card').length===3"));
    assert.equal(await evaluate("document.querySelector('.reply-files').open"),true);
    const bounds = await evaluate("[...document.querySelectorAll('.reply-file-card')].map(node=>{const b=node.getBoundingClientRect();return {x:b.x,y:b.y,width:b.width}})");
    assert.equal(bounds[0].y,bounds[1].y);
    assert.ok(bounds[1].x>bounds[0].x);
    const results = [{view:'desktop',screenshot:await screenshot('desktop')}];
    await evaluate("document.querySelector('.reply-file-diff').click()");
    await until(()=>evaluate("document.querySelector('#file-view-dialog').open && Boolean(document.querySelector('.diff-added'))"));
    assert.equal(await evaluate("document.querySelector('#file-view-title').textContent.startsWith('本轮差异')"),true);
    results.push({view:'diff',screenshot:await screenshot('diff')});
    await evaluate("openFileView(state.runs.values().next().value,{path:'plan/中文 space.md'},'preview',state.context)");
    await delay(50);
    assert.equal(await evaluate("document.querySelector('#file-view-dialog').open"),true);
    assert.match(await evaluate("document.querySelector('#file-view-content').textContent"),/更新标题/,
      await evaluate("JSON.stringify({message:document.querySelector('#file-view-message').textContent,files:[...document.querySelectorAll('.reply-file-card')].map(card=>card.dataset.path),runs:[...state.runs.keys()]})"));
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await until(()=>evaluate("!document.querySelector('#file-view-dialog').open"));
    await send('Page.reload');
    await until(()=>evaluate("document.querySelectorAll('.reply-file-card').length===3"));
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await until(()=>evaluate("getComputedStyle(document.querySelector('.reply-files-grid')).gridTemplateColumns.split(' ').length===1"));
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    results.push({view:'mobile',screenshot:await screenshot('mobile')});
    await evaluate("document.querySelector('.reply-file-diff').click()");
    await until(()=>evaluate("Boolean(document.querySelector('.diff-added'))"));
    assert.equal(await evaluate("document.querySelector('#file-view-dialog').getBoundingClientRect().width<=innerWidth"),true);
    results.push({view:'mobile-diff',screenshot:await screenshot('mobile-diff')});
    assert.deepEqual(browserErrors,[]);
    console.log(JSON.stringify({result:'passed',directory,results,browserErrors},null,2));
  } finally {
    socket?.close();chrome?.kill();server.kill();
    if(errors.length) console.error(errors.join(''));
  }
}

run().then(()=>process.exit(0),error=>{console.error(error);process.exit(1);});
