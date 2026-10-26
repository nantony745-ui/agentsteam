// 真实 Chromium + 临时数据库验证 Skills/MCP 只读预览；不操作业务库或原生 Harness。
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsteam-skills-ui-'));
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
  const home = path.join(directory, 'home');
  const makeSkill = (base, slug, title) => {
    const target = path.join(base, slug, 'SKILL.md'); fs.mkdirSync(path.dirname(target), {recursive:true});
    fs.writeFileSync(target, `---\nname: ${title}\ndescription: A preview of the configured skill.\n---\n# ${title}\n\nSkill content with **Markdown** formatting.\n\n## Instructions\n\n- Read the project context.\n- Keep changes focused.\n- Verify the result.\n\n\`\`\`python\nprint("Read only preview")\n\`\`\`\n\n<script>window.skillExecuted=true</script>\n`);
    return target;
  };
  const codexRoot = path.join(home,'.codex','skills');
  const original = makeSkill(codexRoot,'algorithmic-art','algorithmic-art');
  const before = fs.readFileSync(original,'utf8');
  for(let i=0;i<14;i++) makeSkill(codexRoot,'skill-'+i,i===0?'coding-standards':'sample-skill-'+i);
  makeSkill(path.join(home,'.dsh','skills'),'deepseek-skill','DeepSeek Skill');
  const mcpFile=path.join(home,'.codex','config.toml');
  const mcpOriginal=Array.from({length:15},(_,i)=>`[mcp_servers.${i===0?'filesystem':'sample-'+i}]\ncommand="node"\nargs=["server.js"]\nenabled=${i!==1}\n`).join('\n')+'\n[mcp_servers.filesystem.env]\nTOKEN="private-token"\n';
  fs.writeFileSync(mcpFile,mcpOriginal);
  const opencodeRoot=path.join(home,'.config','opencode'); fs.mkdirSync(opencodeRoot,{recursive:true});
  fs.writeFileSync(path.join(opencodeRoot,'opencode.jsonc'),'{// native\n"mcp":{"docs":{"type":"remote","url":"https://example.invalid/mcp",}},}');
  fs.writeFileSync(path.join(home,'.codebuddy.json'),'{"mcpServers":{"buddy":{"command":"node"}}}');
  const server = spawn(path.join(root, '.venv/Scripts/python.exe'), ['-c',
    'import sys; from pathlib import Path; sys.path.insert(0,"tests"); from test_harness_settings import ConfigFactory; from app.main import create_app; import uvicorn; uvicorn.run(create_app(Path(sys.argv[1]),ConfigFactory()),host="127.0.0.1",port=int(sys.argv[2]),log_level="error")',
    path.join(directory, 'ui.db'), String(serverPort)], { cwd: root, windowsHide: true,
      env:{...process.env,USERPROFILE:home,HOME:home,CODEX_HOME:path.join(home,'.codex'),DSH_HOME:path.join(home,'.dsh'),DSH_AGENTS_HOME:path.join(home,'.agents'),XDG_CONFIG_HOME:path.join(home,'.config'),OPENCODE_CONFIG:path.join(home,'extra.json'),OPENCODE_CONFIG_DIR:path.join(home,'extra-dir')},
      stdio: ['ignore', 'ignore', 'pipe'] });
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
    const session = await post('/api/sessions', {project_id:project.id,harness:'codex'});
    const running = await post(`/api/sessions/${session.id}/messages`,{message:'Skills preview keeps this running',request_id:crypto.randomUUID()});
    await send('Emulation.setDeviceMetricsOverride', {width:1440,height:900,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate', {url:endpoint});
    await until(()=>evaluate("typeof state!=='undefined' && state.sessions.length===1 && state.sidebarPoll!==null"));
    await evaluate(`openSidebarSession(state.sessions[0])`);
    await until(()=>evaluate(`state.context?.sessionId==='${session.id}' && state.context.ready`));
    await evaluate("document.querySelector('#message-input').value='Keep draft'; globalThis.originalSource=state.source; location.hash='#settings/skills?agent=codex'");
    await until(()=>evaluate("document.querySelector('#skills-count').textContent==='15' && !document.querySelector('#skill-detail').hidden"));
    assert.equal(await evaluate("document.querySelectorAll('#settings-skills-panel textarea,#settings-skills-panel [contenteditable=true]').length"),0);
    assert.equal(await evaluate("window.skillExecuted"),undefined);
    assert.equal(await evaluate("document.querySelector('#skill-markdown script')"),null);
    const results=[];
    const screenshot = async name => { const data=await send('Page.captureScreenshot',{format:'png'}); const file=path.join(directory,name+'.png'); fs.writeFileSync(file,Buffer.from(data.data,'base64')); return file; };
    const listWidth = selector => evaluate(`document.querySelector('${selector}').getBoundingClientRect().width`);
    const dragDivider = async (selector, distance) => {
      const point = await evaluate(`(()=>{const r=document.querySelector('${selector}').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
      await send('Input.dispatchMouseEvent',{type:'mouseMoved',...point});
      await send('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',buttons:1,clickCount:1,...point});
      await send('Input.dispatchMouseEvent',{type:'mouseMoved',button:'left',buttons:1,x:point.x+distance,y:point.y});
      await send('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',buttons:0,clickCount:1,x:point.x+distance,y:point.y});
    };
    assert.equal(await listWidth('.skills-browser'),280);
    const positions = await evaluate("(()=>{const a=document.querySelector('.skills-browser').getBoundingClientRect();const b=document.querySelector('.skills-preview').getBoundingClientRect();return {sideBySide:a.right<=b.left, bottom:a.bottom, scrollable:document.querySelector('#skills-list').scrollHeight>document.querySelector('#skills-list').clientHeight}})()");
    assert.ok(positions.sideBySide && positions.bottom>=880 && positions.scrollable);
    results.push({view:'desktop-skills',screenshot:await screenshot('desktop-skills')});
    await dragDivider('#settings-skills-resizer',80);
    assert.equal(await listWidth('.skills-browser'),360);
    await evaluate("{const search=document.querySelector('#skills-search');search.value='coding-standards';search.dispatchEvent(new Event('input'))}");
    assert.equal(await evaluate("document.querySelectorAll('.skill-list-item').length"),1);
    await evaluate("document.querySelector('.skill-list-item').click()");
    await until(()=>evaluate("document.querySelector('#skill-title').textContent==='coding-standards'"));
    assert.equal(await evaluate('state.source===originalSource && state.source.readyState===1'),true);
    assert.equal(await evaluate("document.querySelector('#message-input').value"),'Keep draft');
    assert.equal((await (await fetch(endpoint+`/api/runs/${running.run_id}`)).json()).status,'running');
    await evaluate("location.hash='#settings/mcp?agent=codex'");
    await until(()=>evaluate("document.querySelector('#mcp-count').textContent==='15' && !document.querySelector('#mcp-detail').hidden"));
    assert.equal(await listWidth('#settings-mcp-panel .skills-browser'),360);
    await dragDivider('#settings-mcp-resizer',-24);
    assert.equal(await listWidth('#settings-mcp-panel .skills-browser'),336);
    assert.equal(await evaluate("document.querySelector('#mcp-config').textContent.includes('private-token')"),false);
    assert.equal(await evaluate("document.querySelectorAll('#settings-mcp-panel textarea,#settings-mcp-panel [contenteditable=true]').length"),0);
    assert.equal(await evaluate('state.source===originalSource && state.source.readyState===1'),true);
    results.push({view:'desktop-mcp',screenshot:await screenshot('desktop-mcp')});
    await evaluate("{const search=document.querySelector('#mcp-search');search.value='sample-1';search.dispatchEvent(new Event('input'))}");
    assert.equal(await evaluate("document.querySelector('#mcp-count').textContent"),'6 / 15');
    await evaluate("document.querySelector('#mcp-list .skill-list-item').click()");
    await until(()=>evaluate("document.querySelector('#mcp-title').textContent==='sample-1'"));
    await send('Page.reload');
    await until(()=>evaluate("document.querySelector('#mcp-title').textContent==='sample-1' && !document.querySelector('#mcp-detail').hidden"));
    assert.equal(await listWidth('#settings-mcp-panel .skills-browser'),336);
    for (const [agent,name] of [['opencode','docs'],['codebuddy','buddy']]) {
      await evaluate(`location.hash='#settings/mcp?agent=${agent}'`);
      await until(()=>evaluate(`document.querySelector('#mcp-title').textContent==='${name}' && !document.querySelector('#mcp-detail').hidden`));
    }
    await evaluate("location.hash='#settings/mcp?agent=deepseek'");
    await until(()=>evaluate("document.querySelector('#mcp-placeholder').textContent.includes('ACP 会话参数') && document.querySelector('#mcp-detail').hidden"));
    await evaluate("location.hash='#settings/skills?agent=codex'");
    await until(()=>evaluate("!document.querySelector('#skill-detail').hidden"));
    await evaluate("{const search=document.querySelector('#skills-search');search.value='coding-standards';search.dispatchEvent(new Event('input'));document.querySelector('#skills-list .skill-list-item').click()}");
    await send('Page.reload');
    await until(()=>evaluate("document.querySelector('#skill-title').textContent==='coding-standards' && !document.querySelector('#skill-detail').hidden"));
    assert.equal(await evaluate("document.querySelector('#settings-skills-nav').getAttribute('aria-current')"),'page');
    assert.equal(await listWidth('.skills-browser'),336);
    await evaluate("document.querySelector('#settings-harness-nav').click()");
    await until(()=>evaluate("document.querySelector('#setting-model') && !document.querySelector('#settings-save').disabled"));
    assert.equal(await evaluate("document.querySelector('#sdk-heading').textContent"),'Agent SDK管理');
    assert.equal(await listWidth('.harness-list'),336);
    await dragDivider('#settings-harness-resizer',-48);
    assert.equal(await listWidth('.harness-list'),288);
    results.push({view:'desktop-sdk',screenshot:await screenshot('desktop-sdk')});
    await evaluate("location.hash='#settings/skills?agent=deepseek'");
    await until(()=>evaluate("document.querySelector('#skill-title').textContent==='DeepSeek Skill' && !document.querySelector('#skill-detail').hidden"));
    assert.equal(await listWidth('.skills-browser'),288);
    await send('Emulation.setDeviceMetricsOverride',{width:800,height:900,deviceScaleFactor:1,mobile:false});
    await until(()=>evaluate("innerWidth===800 && document.querySelector('.skills-preview').getBoundingClientRect().width>=320"));
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    assert.equal(await evaluate("document.querySelector('.skills-preview').getBoundingClientRect().width>=320"),true);
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await until(()=>evaluate("document.querySelector('.skills-workspace').getBoundingClientRect().width<390"));
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    assert.equal(await evaluate("document.querySelector('#settings-skills-resizer').getClientRects().length"),0);
    assert.equal(await evaluate("(()=>{const a=document.querySelector('.skills-browser').getBoundingClientRect();const b=document.querySelector('.skills-preview').getBoundingClientRect();return b.top>=a.bottom})()"),true);
    results.push({view:'mobile-skills',screenshot:await screenshot('mobile-skills')});
    await evaluate("document.querySelector('#skill-preview-scroll').scrollIntoView({block:'start'})");
    results.push({view:'mobile-preview',screenshot:await screenshot('mobile-preview')});
    await evaluate("location.hash='#settings/mcp?agent=codex'");
    await until(()=>evaluate("!document.querySelector('#mcp-detail').hidden"));
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    assert.equal(await evaluate("document.querySelector('#settings-mcp-resizer').getClientRects().length"),0);
    assert.equal(await evaluate("(()=>{const a=document.querySelector('#settings-mcp-panel .skills-browser').getBoundingClientRect();const b=document.querySelector('#settings-mcp-panel .skills-preview').getBoundingClientRect();return b.top>=a.bottom})()"),true);
    results.push({view:'mobile-mcp',screenshot:await screenshot('mobile-mcp')});
    await evaluate("location.hash='#workbench'");
    await until(()=>evaluate("document.querySelector('#settings-view').hidden"));
    assert.equal(await evaluate("document.querySelector('#message-input').value"),''); // reload restores server history, not unsent text
    assert.equal(await evaluate('state.context.sessionId'),session.id);
    assert.equal(fs.readFileSync(original,'utf8'),before);
    assert.equal(fs.readFileSync(mcpFile,'utf8'),mcpOriginal);
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
