// 真实 Chromium、HTTP/SSE 和临时 SQLite；不调用真实模型。
const {spawn, execFileSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'agentsteam-employees-'));
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
    ['tests/fixtures/employees_browser_app.py',directory,String(serverPort)],
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
    const dragDivider = async (selector, distance) => {
      const point = await evaluate(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+80}})()`);
      await send('Input.dispatchMouseEvent',{type:'mouseMoved',...point});
      await send('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});
      await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:point.x+distance,y:point.y,button:'left',buttons:1});
      await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:point.x+distance,y:point.y,button:'left',clickCount:1});
    };
    await send('Runtime.enable');
    await send('Page.enable');
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
    await send('Page.navigate',{url:base+'/'});

    await until(()=>evaluate("typeof EmployeesUI!=='undefined' && state.projects.length===1"));
    await evaluate("document.querySelector('#open-employees').click()");
    await until(()=>evaluate("!document.querySelector('#employees-view').hidden"));
    assert.equal(await evaluate("!document.querySelector('.app-shell').hidden && document.querySelector('#sidebar').getBoundingClientRect().width>0"),true);
    assert.equal(await evaluate("document.querySelector('#employees-empty').hidden"),false);
    await evaluate("document.querySelector('#employee-add').click()");
    await until(()=>evaluate("!document.querySelector('#employee-detail').hidden"));
    assert.equal(await evaluate("document.querySelectorAll('#employee-form textarea').length"),1);
    await evaluate("document.querySelector('#employee-harness-trigger').click()");
    await until(()=>evaluate("document.querySelector('#employee-harness-popup').matches(':popover-open')"));
    assert.equal(await evaluate("document.querySelectorAll('#employee-harness-popup img').length"),4);
    await evaluate("[...document.querySelectorAll('#employee-harness-options button')].find(button=>button.textContent.includes('Codex')).click()");
    assert.match(await evaluate("document.querySelector('#employee-harness-trigger img').src"),/codex.svg$/);
    await evaluate("document.querySelector('#employee-name').value='浏览器队员';document.querySelector('#employee-instructions').value='负责开发，先验证再交付，持续总结经验。';document.querySelector('#employee-form').requestSubmit()");
    await until(()=>evaluate("document.querySelector('#employee-feedback').textContent.includes('已保存')"));
    assert.equal(await evaluate("document.querySelector('#employee-save').disabled"),true);
    await evaluate("document.querySelector('#employee-role').value='研发工程师';document.querySelector('#employee-role').dispatchEvent(new Event('input',{bubbles:true}))");
    assert.equal(await evaluate("document.querySelector('#employee-save').disabled"),false);
    await evaluate("document.querySelector('#employee-discard').click()");
    assert.equal(await evaluate("document.querySelector('#employee-role').value"),'');
    let employees=await (await fetch(base+'/api/employees')).json();
    assert.equal(employees.length,1);
    const employee=employees[0];
    assert.equal(employee.instructions,'负责开发，先验证再交付，持续总结经验。');
    await evaluate("document.querySelector('#employee-probe').click()");
    await until(()=>evaluate("document.querySelectorAll('#employee-config-fields select').length>0"));
    await evaluate("document.querySelector('#employee-config-fields select').value='fast';document.querySelector('#employee-config-fields select').dispatchEvent(new Event('change'));document.querySelector('#employee-name').value='放弃的姓名';document.querySelector('#employee-discard').click()");
    await until(()=>evaluate("document.querySelector('#employee-name').value==='浏览器队员'"));
    await fetch(base+'/fixture/skill/'+employee.employee_id,{method:'POST'});
    await evaluate("document.querySelector('#employee-skills-refresh').click()");
    await until(()=>evaluate("document.querySelectorAll('#employee-skills-list button').length===2"));
    await evaluate("[...document.querySelectorAll('#employee-skills-list button')].find(button=>button.textContent.includes('agentsteam-skill-stewardship')).click()");
    await until(()=>evaluate("document.querySelector('#employee-skill-content').textContent.includes('Experience capture')"));
    await evaluate("[...document.querySelectorAll('#employee-skills-list button')].find(button=>button.textContent.includes('personal-unique')).click()");
    await until(()=>evaluate("document.querySelector('#employee-skill-content').textContent.includes('first')"));
    assert.equal(await evaluate("(()=>{const list=document.querySelector('#employee-skills-list');const sample=list.querySelector('button');for(let i=0;i<10;i++){const copy=sample.cloneNode(true);copy.dataset.scrollFixture='true';list.append(copy)}const limited=list.clientHeight<=590 && list.scrollHeight>list.clientHeight;list.scrollTop=100;const scrolls=list.scrollTop>0;list.querySelectorAll('[data-scroll-fixture]').forEach(node=>node.remove());return limited && scrolls})()"),true);
    await evaluate("document.querySelector('#employee-probe').click()");
    await until(()=>evaluate("document.querySelectorAll('#employee-config-fields select').length>0 && !document.querySelector('#employee-probe').disabled"));
    const results=[{view:'desktop',screenshot:await screenshot('employees-desktop')}];
    await send('Emulation.setDeviceMetricsOverride',{width:1910,height:1000,deviceScaleFactor:1,mobile:false});
    await until(()=>evaluate("document.querySelector('#employee-detail').clientWidth>1200"));
    assert.equal(await evaluate("(()=>{const node=document.querySelector('.employee-detail-scroll'),pane=node.getBoundingClientRect(),detail=document.querySelector('#employee-detail').getBoundingClientRect();return pane.left+node.clientWidth-detail.right<=30 && detail.left-pane.left<=30})()"),true);
    await dragDivider('#employee-resizer',96);
    await until(()=>evaluate("document.querySelector('.employees-browser').getBoundingClientRect().width===336"));
    assert.equal(await evaluate("localStorage.getItem('agentsteam.employees.width.v1')"),'336');
    await evaluate("document.querySelector('#employee-resizer').focus()");
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'ArrowLeft',code:'ArrowLeft',windowsVirtualKeyCode:37});
    await send('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowLeft',code:'ArrowLeft',windowsVirtualKeyCode:37});
    assert.equal(await evaluate("document.querySelector('#employee-resizer').getAttribute('aria-valuenow')"),'320');
    results.push({view:'wide-resized',screenshot:await screenshot('employees-wide')});
    await send('Page.reload');
    await until(()=>evaluate("typeof EmployeesUI!=='undefined' && document.querySelector('#employee-name').value==='浏览器队员'"));
    assert.equal(await evaluate("document.querySelector('#employee-resizer').getAttribute('aria-valuenow')"),'320');
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
    await evaluate("document.querySelector('#employee-probe').click()");
    await until(()=>evaluate("Boolean(document.querySelector('#employee-config-model-trigger')) && !document.querySelector('#employee-probe').disabled"));
    await evaluate("document.querySelector('#employee-config-model-trigger').click()");
    await until(()=>evaluate("document.querySelector('#employee-config-model-popup').matches(':popover-open')"));
    assert.equal(await evaluate("document.querySelector('#employee-config-model-popup input')===document.activeElement"),true);
    assert.equal(await evaluate("document.querySelectorAll('#employee-config-model-options button').length"),63);
    await evaluate("document.querySelector('#employee-config-model-popup input').value='6.1 Sol';document.querySelector('#employee-config-model-popup input').dispatchEvent(new Event('input',{bubbles:true}))");
    assert.equal(await evaluate("document.querySelectorAll('#employee-config-model-options button').length"),1);
    await evaluate("document.querySelector('#employee-config-model-popup input').value='missing-model';document.querySelector('#employee-config-model-popup input').dispatchEvent(new Event('input',{bubbles:true}))");
    assert.equal(await evaluate("document.querySelector('#employee-config-model-options').textContent"),'没有匹配的选项');
    await evaluate("document.querySelector('#employee-config-model-popup input').value='fast';document.querySelector('#employee-config-model-popup input').dispatchEvent(new Event('input',{bubbles:true}))");
    assert.equal(await evaluate("document.querySelectorAll('#employee-config-model-options button').length"),1);
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
    await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
    await until(()=>evaluate("document.querySelector('#employee-config-model').value==='fast' && !document.querySelector('#employee-probe').disabled"));
    assert.equal(await evaluate("document.querySelector('#employee-config-model-popup').matches(':popover-open')"),false);
    await evaluate("document.querySelector('#employee-config-model-trigger').click()");
    await until(()=>evaluate("document.querySelector('#employee-config-model-trigger').getAttribute('aria-expanded')==='true'"));
    results.push({view:'model-picker',screenshot:await screenshot('employees-model-picker')});
    assert.equal(await evaluate("document.querySelector('#employee-config-model-options button[aria-selected=true]').textContent.includes('fast')"),true);
    assert.equal(await evaluate("(()=>{const box=document.querySelector('#employee-config-model-popup').getBoundingClientRect();return box.left>=0 && box.right<=innerWidth && box.top>=0 && box.bottom<=innerHeight})()"),true);
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'End',code:'End',windowsVirtualKeyCode:35});
    assert.equal(await evaluate("document.querySelector('#employee-config-model-options').scrollTop>0"),true);
    assert.equal(await evaluate("document.querySelector('#employee-config-model-options button[data-active=true]').textContent.includes('Model 59')"),true);
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Home',code:'Home',windowsVirtualKeyCode:36});
    assert.equal(await evaluate("document.querySelector('#employee-config-model-options button[data-active=true]').textContent"),'继承全局配置');
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await until(()=>evaluate("!document.querySelector('#employee-config-model-popup').matches(':popover-open')"));
    await evaluate("document.querySelector('#employee-discard').click()");
    await send('Emulation.setDeviceMetricsOverride',{width:1024,height:900,deviceScaleFactor:1,mobile:false});
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    assert.equal(await evaluate("document.querySelector('#employee-name').getBoundingClientRect().right<=innerWidth"),true);
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await until(()=>evaluate("document.querySelector('#employee-resizer').hidden"));
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    results.push({view:'mobile',screenshot:await screenshot('employees-mobile')});
    await evaluate("document.querySelector('#employee-harness-trigger').scrollIntoView({block:'center'});document.querySelector('#employee-harness-trigger').click()");
    await until(()=>evaluate("document.querySelector('#employee-harness-popup').matches(':popover-open')"));
    assert.equal(await evaluate("document.querySelector('#employee-harness-popup').getBoundingClientRect().right<=innerWidth"),true);
    await evaluate("document.querySelector('#employee-harness-trigger').click()");
    await evaluate("document.querySelector('.topbar [data-panel-toggle=left]').click()");
    assert.equal(await evaluate("document.querySelector('#employees-view').inert"),true);
    await evaluate("document.querySelector('#open-employees').click()");
    assert.equal(await evaluate("document.querySelector('#employees-view').inert"),false);
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
    await evaluate(`document.querySelector('#employee-project').value=${JSON.stringify(fixture.project_id)};document.querySelector('#employee-session').click()`);
    await until(()=>evaluate("Boolean(state.draft?.employeeId) && !document.querySelector('.app-shell').hidden"));
    assert.equal(await evaluate("document.querySelector('#harness').hidden"),true);
    await evaluate("document.querySelector('#message-input').value='浏览器原始任务';document.querySelector('#message-form').requestSubmit()");
    await until(()=>evaluate("Boolean(state.context?.sessionId)"));
    await until(()=>evaluate("[...state.runRecords.values()].some(item=>item.status==='running')"));
    await fetch(base+'/fixture/finish',{method:'POST'});
    await until(()=>evaluate("[...state.runRecords.values()].some(item=>item.status==='completed')"));
    await send('Emulation.setDeviceMetricsOverride',{width:1910,height:1000,deviceScaleFactor:1,mobile:false});
    await until(()=>evaluate("document.querySelector('.run').getBoundingClientRect().width>1400"));
    assert.equal(await evaluate("(()=>{const pane=document.querySelector('.conversation').getBoundingClientRect(),run=document.querySelector('.run').getBoundingClientRect(),composer=document.querySelector('.composer').getBoundingClientRect();return pane.right-run.right<=30 && run.left-pane.left<=30 && Math.abs(composer.width-run.width)<2 && Math.abs(composer.left-run.left)<2})()"),true);
    results.push({view:'conversation-wide',screenshot:await screenshot('conversation-wide')});
    await dragDivider('#left-resizer',64);
    assert.equal(await evaluate("document.querySelector('#left-resizer').getAttribute('aria-valuenow')"),'364');
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
    results.push({view:'conversation-mobile',screenshot:await screenshot('conversation-mobile')});
    await send('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
    assert.match(await evaluate("document.querySelector('#session-harness').textContent"),/浏览器队员.*Codex/);
    await evaluate("document.querySelector('#message-input').value='取消本轮';document.querySelector('#message-form').requestSubmit()");
    await until(()=>evaluate("[...state.runRecords.values()].some(item=>item.status==='running')"));
    await evaluate("document.querySelector('#send-button').click()");
    await until(()=>evaluate("[...state.runRecords.values()].some(item=>item.status==='canceled')"));
    await send('Page.reload');
    await until(()=>evaluate("typeof state!=='undefined' && Boolean(state.context?.ready) && document.querySelector('#session-harness').textContent.includes('浏览器队员')"));
    await evaluate("document.querySelector('#open-employees').click()");
    await until(()=>evaluate("!document.querySelector('#employees-view').hidden"));
    await until(()=>evaluate("document.querySelector('#employee-name').value==='浏览器队员'"));
    await evaluate("document.querySelector('#employee-name').value='保存的新姓名';document.querySelector('#employee-form').requestSubmit()");
    await until(()=>evaluate("document.querySelector('#employee-feedback').textContent.includes('已保存')"));
    await evaluate("document.querySelector('#employees-back').click()");
    await until(()=>evaluate("!document.querySelector('.app-shell').hidden"));
    assert.match(await evaluate("document.querySelector('#session-harness').textContent"),/浏览器队员/);
    await evaluate("document.querySelector('#create-session-button').click()");
    assert.equal(await evaluate("document.querySelector('#employee-choice').value"),'');
    assert.equal(await evaluate("document.querySelector('#harness').hidden"),false);
    await evaluate("document.querySelector('#open-employees').click()");
    await until(()=>evaluate("!document.querySelector('#employees-view').hidden"));
    await evaluate("document.querySelector('#open-settings').click()");
    await until(()=>evaluate("!document.querySelector('#settings-view').hidden"));
    assert.equal(await evaluate("document.querySelector('.app-shell').hidden"),true);
    await evaluate("document.querySelector('#settings-return').click()");
    await until(()=>evaluate("!document.querySelector('.app-shell').hidden"));
    assert.equal(await evaluate("document.querySelector('#employees-view').hidden"),true);
    await evaluate("document.querySelector('#open-employees').click()");
    await until(()=>evaluate("!document.querySelector('#employees-view').hidden"));
    await evaluate("document.querySelector('#create-session-button').click()");
    await until(()=>evaluate("document.querySelector('#employees-view').hidden && Boolean(state.draft)"));
    assert.equal(await evaluate("document.querySelector('#message-input')===document.activeElement"),true);
    const legacy = await (await fetch(base+'/api/employees',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'旧版队员',responsibilities:'负责研发',principles:'先做验证',growth:'总结经验'})})).json();
    await evaluate("document.querySelector('#open-employees').click()");
    await until(()=>evaluate("[...document.querySelectorAll('#employee-list button')].some(button=>button.textContent.includes('旧版队员'))"));
    await evaluate("[...document.querySelectorAll('#employee-list button')].find(button=>button.textContent.includes('旧版队员')).click()");
    assert.match(await evaluate("document.querySelector('#employee-instructions').value"),/专业职责：\n负责研发\n\n工作准则：\n先做验证\n\n成长课题：\n总结经验/);
    await evaluate("document.querySelector('#employee-role').value='研发';document.querySelector('#employee-role').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#employee-form').requestSubmit()");
    await until(()=>evaluate("document.querySelector('#employee-feedback').textContent.includes('已保存')"));
    const unchanged = await (await fetch(base+'/api/employees/'+legacy.employee_id)).json();
    assert.equal(unchanged.principles,'先做验证');
    assert.equal(unchanged.instructions,'');
    await evaluate("document.querySelector('#employee-instructions').value='统一工作指引';document.querySelector('#employee-instructions').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#employee-form').requestSubmit()");
    await until(()=>evaluate("document.querySelector('#employee-feedback').textContent.includes('已保存')"));
    const merged = await (await fetch(base+'/api/employees/'+legacy.employee_id)).json();
    assert.equal(merged.instructions,'统一工作指引');
    assert.equal(merged.responsibilities+merged.principles+merged.growth,'');
    const memoryImages = await require('./employee_memory_browser.cjs')({base, evaluate, send, until, screenshot,
      first: {employee_id: employee.employee_id, name: '保存的新姓名'}, second: merged});
    results.push({view: 'personal-memory', screenshots: memoryImages});
    await evaluate("document.querySelector('#employee-delete').click()");
    assert.equal(await evaluate("document.querySelector('#employee-delete-dialog').open"),true);
    assert.match(await evaluate("document.querySelector('#employee-delete-description').textContent"),/个人技能、历史会话和本地文件全部保留/);
    await evaluate("document.querySelector('#employee-delete-cancel').click()");
    assert.equal((await fetch(base+'/api/employees/'+legacy.employee_id)).status,200);
    const {employee_id: legacyId, ...legacyUpdate} = merged;
    await fetch(base+'/api/employees/'+legacyId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({...legacyUpdate,name:'更新后的队员'})});
    await evaluate("document.querySelector('#employee-delete').click();document.querySelector('#employee-delete-form').requestSubmit()");
    await until(()=>evaluate("document.querySelector('#employee-delete-error').textContent.includes('已更新')"));
    assert.equal(await evaluate("document.querySelector('#employee-delete-dialog').open"),true);
    await evaluate("document.querySelector('#employee-delete-cancel').click();document.querySelector('#employees-back').click()");
    await until(()=>evaluate("document.querySelector('#employees-view').hidden"));
    await evaluate("document.querySelector('#open-employees').click()");
    await until(()=>evaluate("document.querySelector('#employee-name').value==='更新后的队员'"));
    await evaluate("window.deleteCalls=0;window.originalFetch=fetch;window.fetch=(url,options)=>{if(options?.method==='DELETE')window.deleteCalls++;return window.originalFetch(url,options)};document.querySelector('#employee-delete').click();document.querySelector('#employee-delete-form').requestSubmit();document.querySelector('#employee-delete-form').requestSubmit()");
    await until(()=>evaluate("!document.querySelector('#employee-delete-dialog').open && document.querySelector('#employee-count').textContent==='1'"));
    assert.equal(await evaluate('window.deleteCalls'),1);
    await until(()=>evaluate("document.querySelector('#employee-name').value==='保存的新姓名'"));
    assert.equal(await evaluate(`!![...document.querySelector('#employee-choice').options].find(option=>option.value===${JSON.stringify(legacyId)})`),false);
    const employeeDirectory=path.join(directory,'employees',employee.employee_id);
    const files=fs.readdirSync(employeeDirectory,{recursive:true}).filter(name=>fs.statSync(path.join(employeeDirectory,name)).isFile());
    const originalFiles=new Map(files.map(name=>[name,fs.readFileSync(path.join(employeeDirectory,name))]));
    await evaluate(`document.querySelector('#employee-choice').value=${JSON.stringify(employee.employee_id)};document.querySelector('#employee-choice').dispatchEvent(new Event('change'));document.querySelector('#employee-delete').click()`);
    results.push({view:'delete-confirmation',screenshot:await screenshot('employees-delete')});
    await evaluate("document.querySelector('#employee-delete-form').requestSubmit()");
    await until(()=>evaluate("document.querySelector('#employee-count').textContent==='0' && !document.querySelector('#employees-empty').hidden"));
    assert.equal(await evaluate("document.querySelector('#employee-detail').hidden"),true);
    assert.equal(await evaluate("state.draft.employeeId"),null);
    assert.equal(await evaluate("document.querySelector('#employee-choice').options.length"),1);
    for(const [name,contents] of originalFiles) {
      assert.equal(fs.existsSync(path.join(employeeDirectory,name)),true);
      if(name!=='profile.json') assert.deepEqual(fs.readFileSync(path.join(employeeDirectory,name)),contents);
    }
    const storedProfile=JSON.parse(fs.readFileSync(path.join(employeeDirectory,'profile.json'),'utf8'));
    assert.equal(storedProfile.deleted,true);
    const historical=(await(await fetch(base+`/api/projects/${fixture.project_id}/sessions`)).json()).find(item=>item.employee_id===employee.employee_id);
    assert.ok(historical);
    assert.equal((await fetch(base+`/api/sessions/${historical.id}/history`)).status,200);
    await send('Page.reload');
    await until(()=>evaluate("typeof EmployeesUI!=='undefined' && document.querySelector('#employee-count').textContent==='0' && !document.querySelector('#employees-view').hidden"));
    assert.deepEqual(browserErrors,[]);
    console.log(JSON.stringify({result:'passed',directory,results,browserErrors},null,2));
  } finally {
    socket?.close();chrome?.kill();server.kill();
    if(errors.length) console.error(errors.join(''));
  }
}
run().then(()=>process.exit(0),error=>{console.error(error);process.exit(1);});
