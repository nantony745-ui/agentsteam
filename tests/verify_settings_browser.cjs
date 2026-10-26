// 使用本机已有 Chromium 和原生 CDP 验证真实排版；ACP 边界使用测试替身。
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsteam-settings-ui-'));
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
    await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
    await send('Page.navigate', { url: `http://127.0.0.1:${serverPort}/` });
    await until(() => evaluate("typeof state !== 'undefined' && state.sidebarPoll !== null"));
    await evaluate("document.querySelector('#message-input').value='浏览器草稿'; const filler=document.createElement('div'); filler.style.height='3000px'; document.querySelector('.sidebar-scroll').append(filler)");
    const entryPosition = await evaluate("document.querySelector('#open-settings').getBoundingClientRect().bottom");
    await evaluate("document.querySelector('.sidebar-scroll').scrollTop=2000");
    assert.equal(await evaluate("document.querySelector('#open-settings').getBoundingClientRect().bottom"), entryPosition);
    assert.ok(entryPosition > 850 && entryPosition <= 900, '齿轮应固定在侧栏底部');
    await evaluate("document.querySelector('#open-settings').click()");
    await until(() => evaluate("document.querySelectorAll('.harness-row').length===4"));
    await until(() => evaluate("Boolean(document.querySelector('#setting-model')) && !document.querySelector('#settings-save').disabled"));
    const columns = await evaluate("(()=>{const list=document.querySelector('#harness-list').getBoundingClientRect();const detail=document.querySelector('#harness-detail').getBoundingClientRect();return {sideBySide:list.right<=detail.left,top:list.top,bottom:list.bottom,right:detail.right,height:detail.height}})()");
    const headingBottom = await evaluate("document.querySelector('#settings-harness-panel .settings-page-heading').getBoundingClientRect().bottom");
    assert.ok(columns.sideBySide && columns.top >= headingBottom && columns.top <= headingBottom + 12 && columns.bottom >= 880 && columns.right >= 1420 && columns.height >= 750, `标题下两栏应铺满右侧可用空间：${JSON.stringify({columns,headingBottom})}`);
    assert.equal(await evaluate("document.querySelector('#sdk-heading').textContent"), 'Agent SDK管理');
    await evaluate("document.querySelectorAll('.harness-detect')[1].click()");
    await until(() => evaluate("document.querySelectorAll('.harness-row')[1].querySelector('.harness-badge').dataset.status==='ready'"));
    assert.equal(await evaluate("document.querySelector('#settings-title').textContent"), 'Codex');
    const results = [];
    const screenshot = async name => {
      const data = await send('Page.captureScreenshot', { format: 'png' });
      const file = path.join(directory, name + '.png');
      fs.writeFileSync(file, Buffer.from(data.data, 'base64'));
      return file;
    };
    results.push({ view: 'desktop-list', screenshot: await screenshot('desktop-list') });
    await evaluate("location.hash='#settings/codex'");
    await until(() => evaluate("Boolean(document.querySelector('#setting-model')) && !document.querySelector('#settings-save').disabled"));
    await evaluate("const input=document.querySelector('#setting-model'); input.value='fast'; input.dispatchEvent(new Event('change')); ");
    await until(() => evaluate("document.querySelector('#setting-model')?.value==='fast' && !document.querySelector('#settings-save').disabled"));
    await evaluate("document.querySelector('#harness-settings-form').requestSubmit()");
    await until(() => evaluate("document.querySelector('#settings-feedback').textContent.includes('已保存')"));
    assert.equal(await evaluate("(async () => { const records = await (await fetch('/api/settings/harnesses')).json(); return records[0].settings.config_values.model; })()"), 'fast');
    results.push({ view: 'desktop-detail', screenshot: await screenshot('desktop-detail') });
    await send('Page.reload');
    await until(() => evaluate("document.querySelector('#setting-model')?.value==='fast' && !document.querySelector('#settings-save').disabled"));
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    const positions = await evaluate("[...document.querySelectorAll('.harness-field')].map(row=>{const label=row.querySelector('label').getBoundingClientRect();const select=row.querySelector('select').getBoundingClientRect();return select.top>label.bottom})");
    assert.ok(positions.every(Boolean), '手机配置行应上下排列');
    results.push({ view: 'mobile-detail', screenshot: await screenshot('mobile-detail') });
    await evaluate("location.hash='#settings'");
    await until(() => evaluate("!document.querySelector('#harness-list').hidden"));
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    assert.equal(await evaluate("document.querySelector('#harness-detail').hidden"), false);
    results.push({ view: 'mobile-list', screenshot: await screenshot('mobile-list') });
    await evaluate("location.hash='#workbench'");
    await until(() => evaluate("document.querySelector('#settings-view').hidden"));
    assert.equal(await evaluate("document.querySelector('.app-shell').hidden"), false);
    await evaluate("const warning=document.querySelector('#session-config-warning'); warning.textContent='权限未生效：当前会话保留实际旧值'; warning.hidden=false");
    assert.equal(await evaluate("document.querySelector('#session-config-warning').getBoundingClientRect().bottom <= document.querySelector('.timeline-container').getBoundingClientRect().top"), true);
    assert.ok(await evaluate("document.querySelector('.timeline-container').getBoundingClientRect().height") > 0);
    results.push({ view: 'mobile-warning', screenshot: await screenshot('mobile-warning') });
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
