// 使用已有 Chromium/CDP 验证真实文件夹拖入、目录选择和项目创建。
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsteam-project-create-'));
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
  const repository = path.join(fs.realpathSync.native(directory), '中文 项目');
  fs.mkdirSync(repository);
  fs.writeFileSync(path.join(repository,'说明.txt'),'保留用户内容');
  const serverPort = await port(), debugPort = await port();
  const server = spawn(path.join(root, '.venv/Scripts/python.exe'), ['-c',
    'import sys; from pathlib import Path; from app.main import create_app; import uvicorn; uvicorn.run(create_app(Path(sys.argv[1])),host="127.0.0.1",port=int(sys.argv[2]),log_level="error")',
    path.join(directory, 'ui.db'), String(serverPort)], {cwd:root, windowsHide:true, stdio:['ignore','ignore','pipe']});
  let chrome, socket;
  let serverErrors = '';
  server.stderr.on('data', data => {serverErrors += data;});
  try {
    await until(async () => {try {return (await fetch(`http://127.0.0.1:${serverPort}/api/projects`)).ok;} catch {return false;}});
    chrome = spawn(chromium, ['--headless=new', '--no-first-run', '--disable-gpu',
      `--remote-debugging-port=${debugPort}`, `--user-data-dir=${path.join(directory,'profile')}`, 'about:blank'],
      {windowsHide:true, stdio:'ignore'});
    const target = await until(async () => {try {return (await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()).find(item=>item.type==='page');} catch {return false;}});
    socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve);socket.addEventListener('error',reject);});
    let sequence = 0;
    const pending = new Map(), browserErrors = [];
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const result = pending.get(message.id); pending.delete(message.id);
        if (message.error) result.reject(new Error(JSON.stringify(message.error)));
        else result.resolve(message.result);
      } else if (message.method === 'Runtime.exceptionThrown') browserErrors.push(message.params.exceptionDetails);
    });
    const send = (method, params = {}) => new Promise((resolve,reject)=>{
      pending.set(++sequence,{resolve,reject}); socket.send(JSON.stringify({id:sequence,method,params}));
    });
    const evaluate = async expression => {
      const response = await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
      if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
      return response.result.value;
    };
    await send('Runtime.enable'); await send('Page.enable');
    await send('Emulation.setDeviceMetricsOverride',{width:1200,height:900,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate',{url:`http://127.0.0.1:${serverPort}/`});
    await until(()=>evaluate("typeof state !== 'undefined' && state.sidebarPoll !== null"));
    await evaluate("document.querySelector('button[data-sidebar-key=\"add-project\"]').click()");
    const position = await evaluate("(()=>{const r=document.querySelector('#project-path').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()");
    for (const type of ['dragEnter','dragOver','drop']) await send('Input.dispatchDragEvent', {
      type, ...position, data:{items:[],files:[repository],dragOperationsMask:1},
    });
    await until(()=>evaluate("!document.querySelector('#project-directory-browser').hidden && !document.querySelector('#project-directory-use').disabled"));
    assert.ok(await evaluate("document.querySelector('#project-directory-notice').textContent.includes('中文 项目')"));
    assert.equal(await evaluate("document.querySelector('#project-path').value"), '');
    // 浏览器只给目录名，用户通过目录选择器定位，不推测同名路径。
    await evaluate(`browseProjectDirectory(${JSON.stringify(directory)})`);
    await evaluate("Array.from(document.querySelectorAll('#project-directory-entries button')).find(b=>b.textContent==='中文 项目').click()");
    await until(()=>evaluate(`document.querySelector('#project-directory-current').textContent===${JSON.stringify(repository)} && !document.querySelector('#project-directory-use').disabled`));
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
    assert.equal(await evaluate("document.documentElement.scrollWidth<=innerWidth"),true);
    const shot = await send('Page.captureScreenshot',{format:'png'});
    fs.writeFileSync(path.join(directory,'project-mobile.png'),Buffer.from(shot.data,'base64'));
    await evaluate("document.querySelector('#project-directory-use').click();document.querySelector('#project-create-submit').click()");
    await until(()=>evaluate("!document.querySelector('#project-dialog').open && state.projects.length===1"));
    assert.equal(await evaluate("state.projects[0].name"),'中文 项目');
    await until(()=>evaluate("document.querySelector('#project-git-label').textContent==='未初始化 Git'"));
    assert.equal(fs.existsSync(path.join(repository,'.git')),false);
    await evaluate("document.querySelector('#project-git-toggle').click()");
    await until(()=>evaluate("!document.querySelector('#project-git-refresh').disabled"));
    assert.equal(await evaluate("document.querySelector('#project-git-panel').hidden"),false);
    assert.equal(await evaluate("document.querySelector('#project-git-panel').matches(':popover-open')"),true);
    assert.equal(await evaluate("(()=>{const r=document.querySelector('#project-git-close').getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===document.querySelector('#project-git-close')})()"),true);
    assert.equal(await evaluate("document.querySelector('#project-git-toggle').getBoundingClientRect().left >= document.querySelector('#session-cwd').getBoundingClientRect().right"),true);
    await evaluate("document.querySelector('#project-git-init').click()");
    await until(()=>evaluate("document.querySelector('#project-git-message').textContent.includes('尚无提交')"));
    assert.equal(fs.readFileSync(path.join(repository,'说明.txt'),'utf8'),'保留用户内容');
    assert.equal(await evaluate("document.querySelector('#project-git-changes').textContent.includes('说明.txt')"),true);
    assert.equal(await evaluate("document.documentElement.scrollWidth<=innerWidth"),true);
    const gitShot = await send('Page.captureScreenshot',{format:'png'});
    fs.writeFileSync(path.join(directory,'project-git-mobile.png'),Buffer.from(gitShot.data,'base64'));
    assert.deepEqual(browserErrors,[]);
    console.log(JSON.stringify({result:'passed',directory,checks:['真实目录拖拽','服务端目录选择','普通目录创建','地址右侧Git入口','显式初始化及变更展示','390px布局'],browserErrors}));
  } finally {
    socket?.close(); chrome?.kill(); server.kill();
    if (serverErrors) console.error(serverErrors);
  }
}
run().then(()=>process.exit(0),error=>{console.error(error);process.exit(1);});
