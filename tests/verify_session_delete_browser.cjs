// 真实 Chromium + 临时数据库验证删除流程；不操作业务库或原生 Harness。
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsteam-session-delete-'));
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
  const serverPort = await port();
  const debugPort = await port();
  const server = spawn(path.join(root, '.venv/Scripts/python.exe'), ['-c',
    'import sys; from pathlib import Path; sys.path.insert(0,"tests"); from test_harness_settings import ConfigFactory; from app.main import create_app; import uvicorn; uvicorn.run(create_app(Path(sys.argv[1]),ConfigFactory()),host="127.0.0.1",port=int(sys.argv[2]),log_level="error")',
    path.join(directory, 'ui.db'), String(serverPort)], { cwd: root, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
  let chrome;
  let socket;
  const errors = [];
  server.stderr.on('data', data => errors.push(String(data)));
  try {
    await until(async () => { try { return (await fetch(`http://127.0.0.1:${serverPort}/api/projects`)).ok; } catch { return false; } });
    chrome = spawn(chromium, ['--headless=new', '--no-first-run', '--disable-gpu',
      `--remote-debugging-port=${debugPort}`, `--user-data-dir=${path.join(directory, 'profile')}`, 'about:blank'],
      { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
    chrome.stderr.on('data', () => {});
    const target = await until(async () => { try { return (await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()).find(item => item.type === 'page'); } catch { return false; } });
    socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { socket.addEventListener('open', resolve); socket.addEventListener('error', reject); });
    let sequence = 0;
    const pending = new Map();
    const browserErrors = [];
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const result = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) result.reject(new Error(JSON.stringify(message.error)));
        else result.resolve(message.result);
      } else if (message.method === 'Runtime.exceptionThrown') browserErrors.push(message.params.exceptionDetails);
    });
    const send = (method, params = {}) => new Promise((resolve, reject) => {
      pending.set(++sequence, { resolve, reject });
      socket.send(JSON.stringify({ id: sequence, method, params }));
    });
    const evaluate = async expression => {
      const response = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
      return response.result.value;
    };
    await send('Runtime.enable');
    await send('Page.enable');
    const repository = path.join(directory, 'repo');
    execFileSync('git', ['init', '--quiet', repository]);
    execFileSync('git', ['-C', repository, '-c', 'user.name=Browser Test', '-c', 'user.email=browser@example.invalid', '-c', 'commit.gpgsign=false', '-c', `core.hooksPath=${path.join(directory,'no-hooks')}`, 'commit', '--quiet', '--allow-empty', '-m', 'browser']);
    const endpoint = `http://127.0.0.1:${serverPort}`;
    const post = async (url, body) => { const result = await fetch(endpoint+url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); assert.ok(result.ok, await result.clone().text()); return result.json(); };
    const project = await post('/api/projects', {path:repository});
    const parent = await post('/api/sessions', {project_id:project.id,harness:'codex'});
    const other = await post('/api/sessions', {project_id:project.id,harness:'deepseek'});
    const first = await post(`/api/sessions/${parent.id}/messages`,{message:'待删除父会话',request_id:crypto.randomUUID()});
    const otherRun = await post(`/api/sessions/${other.id}/messages`,{message:'保留的运行会话',request_id:crypto.randomUUID()});
    await until(async()=> (await (await fetch(endpoint+`/api/runs/${first.run_id}`)).json()).status==='running');
    await send('Emulation.setDeviceMetricsOverride', {width:1440,height:900,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate', {url:endpoint});
    await until(()=>evaluate("typeof state!=='undefined' && state.sessions.length===2 && state.sidebarPoll!==null"));
    await evaluate(`openSidebarSession(state.sessions.find(item=>item.id==='${other.id}'))`);
    await until(()=>evaluate(`state.context?.sessionId==='${other.id}' && state.context.ready`));
    const screenshot = async name => { const data=await send('Page.captureScreenshot',{format:'png'}); const file=path.join(directory,name+'.png'); fs.writeFileSync(file,Buffer.from(data.data,'base64')); return file; };
    const rightClick = async id => {
      const bounds=await until(()=>evaluate(`(()=>{const buttons=[...document.querySelectorAll('button[data-session-id="${id}"]')];const b=buttons.map(button=>button.getBoundingClientRect()).find(b=>b.width>0 && b.height>0 && b.top>=0 && b.bottom<=innerHeight && b.left>=0 && b.right<=innerWidth);return b ? {x:b.left+Math.min(60,b.width/2),y:b.top+b.height/2} : null})()`));
      await send('Input.dispatchMouseEvent',{type:'mousePressed',button:'right',buttons:2,clickCount:1,...bounds});
      await send('Input.dispatchMouseEvent',{type:'mouseReleased',button:'right',buttons:0,clickCount:1,...bounds});
      await until(()=>evaluate("!document.querySelector('#session-menu').hidden"));
    };
    await rightClick(parent.id);
    const results=[{view:'desktop-context-menu',screenshot:await screenshot('desktop-context-menu')}];
    assert.equal(await evaluate('state.context.sessionId'),other.id);
    await evaluate("document.querySelector('#session-menu-delete').click()");
    assert.equal(await evaluate("document.querySelector('#session-delete-dialog').open"),true);
    results.push({view:'desktop-confirm',screenshot:await screenshot('desktop-confirm')});
    await evaluate("document.querySelector('#session-delete-cancel').click()");
    assert.equal((await (await fetch(endpoint+'/api/sessions')).json()).length,2);
    await rightClick(parent.id);
    await evaluate("document.querySelector('#session-menu-delete').click(); document.querySelector('#session-delete-form').requestSubmit()");
    await until(()=>evaluate(`!state.sessions.some(s=>s.id==='${parent.id}') && !document.querySelector('#session-delete-dialog').open`));
    assert.equal(await evaluate('state.context.sessionId'),other.id);
    assert.equal(await evaluate('state.source.readyState'),1);
    assert.equal((await fetch(endpoint+`/api/sessions/${parent.id}`)).status,404);
    assert.equal((await fetch(endpoint+`/api/runs/${first.run_id}`)).status,404);
    assert.equal((await (await fetch(endpoint+`/api/runs/${otherRun.run_id}`)).json()).status,'running');
    await send('Page.reload');
    await until(()=>evaluate(`typeof state!=='undefined' && state.sessions.length===1 && state.context?.sessionId==='${other.id}' && state.context.ready`));
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await until(()=>evaluate("document.querySelector('#sidebar').dataset.drawer==='true'"));
    await evaluate("if (document.querySelector('#sidebar').hidden) document.querySelector('[data-panel-toggle=left]').click()");
    await rightClick(other.id);
    assert.equal(await evaluate("(()=>{const b=document.querySelector('#session-menu').getBoundingClientRect();return b.left>=0 && b.right<=innerWidth && b.bottom<=innerHeight})()"),true);
    results.push({view:'mobile-context-menu',screenshot:await screenshot('mobile-context-menu')});
    await evaluate("document.querySelector('#session-menu-delete').click()");
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    results.push({view:'mobile-confirm',screenshot:await screenshot('mobile-confirm')});
    await evaluate("document.querySelector('#session-delete-form').requestSubmit()");
    await until(()=>evaluate("state.sessions.length===0 && state.context===null && !document.querySelector('#session-delete-dialog').open"));
    assert.equal(await evaluate('state.source'),null);
    await send('Page.reload');
    await until(()=>evaluate("typeof state!=='undefined' && state.sidebarPoll!==null && state.sessions.length===0"));
    assert.equal(await evaluate('state.context'),null);
    assert.deepEqual(browserErrors, []);
    console.log(JSON.stringify({ result: 'passed', directory, results, browserErrors }, null, 2));
  } finally {
    if (socket) socket.close();
    if (chrome) chrome.kill();
    server.kill();
    if (errors.length) console.error(errors.join(''));
  }
}

run().then(() => process.exit(0), error => { console.error(error); process.exit(1); });
