const assert = require('node:assert/strict');

module.exports = async ({base, evaluate, send, until, screenshot, first, second}) => {
  const click = id => evaluate(`document.getElementById(${JSON.stringify(id)}).click()`);
  const value = id => evaluate(`document.getElementById(${JSON.stringify(id)}).textContent`);
  const select = async name => {
    await evaluate(`[...document.querySelectorAll('#employee-list button')].find(button=>button.textContent.includes(${JSON.stringify(name)})).click()`);
    await until(() => evaluate(`document.querySelector('#employee-name').value===${JSON.stringify(name)} && !document.querySelector('#employee-memory-enabled').disabled`));
  };
  await until(() => evaluate("!document.querySelector('#employee-memory-enabled').disabled"));
  assert.equal(await evaluate("document.querySelector('#employee-memory-enabled').checked"), false);
  assert.equal(await evaluate("document.querySelector('#employee-memory-section').previousElementSibling.id"), 'employee-skills-section');
  assert.equal(await evaluate("document.querySelector('#employee-memory-kind')"), null);
  for (const title of ['检索与反思', '保存记忆', '来源与版本', '处理进度']) {
    assert.equal((await value('employee-memory-section')).includes(title), false);
  }
  await click('employee-memory-enabled');
  await until(async () => (await value('employee-memory-status')).includes('全局记忆未启用'));
  await fetch(base + '/fixture/memory', {method: 'POST'});
  await click('employee-memory-refresh');
  await until(async () => (await value('employee-memory-status')).includes('运行中'));
  await until(async () => (await value('employee-memory-facts')).includes('记忆库还是空的'));

  const ownPath = `/api/employees/${second.employee_id}/memory`;
  await fetch(base + ownPath + '/retain', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({request_id: crypto.randomUUID(), content: '浏览器个人来源'})});
  await click('employee-memory-refresh');
  await until(async () => (await value('employee-memory-facts')).includes(second.employee_id));
  assert.equal(await value('employee-memory-count'), '1 条记忆');
  assert.equal(await evaluate("document.querySelectorAll('.employee-memory-item').length"), 1);
  assert.equal(await evaluate("document.querySelector('#employee-memory-next').disabled"), true);
  await click('employee-memory-enabled');
  await until(async () => (await value('employee-memory-status')).includes('已关闭'));
  assert.ok((await value('employee-memory-facts')).includes(second.employee_id));

  await evaluate(`window.memoryFetch=fetch;window.memoryHeld=[];window.memoryHold=true;
    window.fetch=async (url,options)=>{const response=await window.memoryFetch(url,options);
      if(window.memoryHold && String(url).includes(${JSON.stringify(ownPath + '/facts')}))
        return new Promise(resolve=>window.memoryHeld.push(()=>resolve(response)));
      return response;};document.querySelector('#employee-memory-refresh').click()`);
  await until(() => evaluate('window.memoryHeld.length===1'));
  await select(first.name);
  assert.ok((await value('employee-memory-facts')).includes('记忆库还是空的'));
  await evaluate('window.memoryHold=false;window.memoryHeld.splice(0).forEach(resolve=>resolve());window.fetch=window.memoryFetch');
  assert.equal((await value('employee-memory-facts')).includes(second.employee_id), false);
  await select(second.name);
  await until(async () => (await value('employee-memory-facts')).includes(second.employee_id));

  const images = [];
  for (const width of [1440, 390]) {
    await send('Emulation.setDeviceMetricsOverride', {width, height: 900, deviceScaleFactor: 1, mobile: width < 500});
    await evaluate("document.querySelector('#employee-memory-section').scrollIntoView({block:'start'})");
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'), true);
    images.push(await screenshot(`personal-memory-${width}`));
  }
  await send('Emulation.setDeviceMetricsOverride', {width: 1440, height: 900, deviceScaleFactor: 1, mobile: false});
  await evaluate("location.hash='#settings/memory'");
  await until(() => evaluate("Boolean(document.querySelector('#memory-auto_retain_turns'))"));
  assert.equal(await evaluate("document.querySelector('#memory-auto_retain_turns').value"), '3');
  await evaluate("document.querySelector('#memory-auto_retain_turns').value='4';document.querySelector('#memory-config button[type=submit]').click()");
  await until(async () => (await (await fetch(base + '/api/settings/memory')).json()).auto_retain_turns === 4);
  await click('open-employees');
  await until(() => evaluate("!document.querySelector('#employees-view').hidden"));
  await select(second.name);
  await until(async () => (await value('employee-memory-threshold')).includes('4'));
  assert.equal(await value('employee-memory-error'), '');
  return images;
};
