const test = require('node:test');
const assert = require('node:assert/strict');
const { environment } = require('./frontend_dom.cjs');
const { addSkillsElements } = require('./skills_dom.cjs');

const KEY = 'agentsteam.settings.width.v1';
function setup(saved, viewport = 1440) {
  const env = environment(viewport);
  const view = env.document.createElement('div'); view.id = 'settings-view'; env.document.body.append(view);
  addSkillsElements(env);
  env.get('#settings-skills-panel').hidden = true;
  env.get('#settings-mcp-panel').hidden = true;
  if (saved !== undefined) env.storage.set(KEY, saved);
  env.run('SettingsLayout.init()');
  return env;
}
const width = env => env.get('#settings-view').style['--settings-list-width'];
const emitWindow = (env, name) => { for (const handler of env.window.listeners[name] || []) handler(); };
function drag(env, handle, distance, end = 'pointerup') {
  handle.emit('pointerdown', { clientX: 300 });
  handle.emit('pointermove', { clientX: 300 + distance });
  handle.emit(end);
}

test('两页默认 280px，任一分隔线拖动后共享宽度并在刷新后恢复', () => {
  const env = setup();
  assert.equal(width(env), '280px');
  drag(env, env.get('#settings-harness-resizer'), 96);
  assert.equal(width(env), '376px');
  env.get('#settings-harness-panel').hidden = true;
  env.get('#settings-skills-panel').hidden = false;
  env.run('SettingsLayout.render()');
  assert.equal(env.get('#settings-skills-resizer').getAttribute('aria-valuenow'), '376');
  drag(env, env.get('#settings-skills-resizer'), -32);
  assert.equal(width(env), '344px');
  assert.equal(env.get('#settings-harness-resizer').getAttribute('aria-valuenow'), '344');
  assert.equal(width(setup(env.storage.get(KEY))), '344px');
  env.get('#settings-skills-panel').hidden = true;
  env.get('#settings-mcp-panel').hidden = false;
  env.run('SettingsLayout.render()');
  assert.equal(env.get('#settings-mcp-resizer').getAttribute('aria-valuenow'),'344');
  drag(env,env.get('#settings-mcp-resizer'),32);
  assert.equal(env.get('#settings-skills-resizer').getAttribute('aria-valuenow'),'376');
});

test('按可用内容宽度限制拖动，方向键调整，窄屏隐藏分隔线且保留偏好', () => {
  const env = setup(); const handle = env.get('#settings-harness-resizer');
  drag(env, handle, 2000); assert.equal(width(env), '520px');
  const container = env.document.querySelector('.settings-split'); container.clientWidth = 720;
  env.resize(1000); assert.equal(width(env), '388px');
  env.resize(390); assert.equal(handle.hidden, true);
  drag(env, handle, -500); handle.emit('keydown', {key:'ArrowLeft'});
  assert.equal(env.storage.get(KEY), '520');
  container.clientWidth = 1000; env.resize(1440);
  assert.equal(handle.hidden, false); assert.equal(width(env), '520px');
  handle.emit('keydown', {key:'ArrowLeft'}); assert.equal(width(env), '504px');
  drag(env, handle, -2000); assert.equal(width(env), '220px');
  handle.emit('keydown', {key:'ArrowRight'}); assert.equal(width(env), '236px');
});

test('拖动取消、失焦、丢失捕获和切页恢复原宽度且不持久化中间值', () => {
  for (const reason of ['pointercancel', 'lostpointercapture', 'blur', 'hashchange', 'resize']) {
    const env = setup('320'); const handle = env.get('#settings-harness-resizer');
    handle.emit('pointerdown', {clientX:300}); handle.emit('pointermove', {clientX:400});
    assert.equal(width(env), '420px');
    if (reason.startsWith('pointer') || reason === 'lostpointercapture') handle.emit(reason);
    else emitWindow(env, reason);
    assert.equal(width(env), '320px', reason);
    assert.equal(env.storage.get(KEY), '320');
    assert.equal(env.document.body.classList.contains('resizing'), false);
  }
});

test('损坏的宽度偏好采用默认值，超限值限制在可用范围', () => {
  for (const saved of ['broken', 'null', '"360"', '{}']) assert.equal(width(setup(saved)), '280px');
  assert.equal(width(setup('9999')), '520px');
  assert.equal(width(setup('-1')), '220px');
});

function setupEmployees(saved) {
  const env = setup('300');
  const view = env.document.createElement('section');
  view.id = 'employees-view'; view.clientWidth = 1000;
  env.document.body.append(view);
  const handle = env.document.createElement('div');
  handle.id = 'employee-resizer'; view.append(handle);
  if (saved !== undefined) env.storage.set('agentsteam.employees.width.v1', saved);
  env.run('EmployeeLayout.init()');
  return env;
}
const employeeWidth = env => env.get('#employees-view').style['--settings-list-width'];

test('队员分栏独立拖动、键盘调整并恢复偏好，不改设置页宽度', () => {
  const env = setupEmployees(); const handle = env.get('#employee-resizer');
  assert.equal(employeeWidth(env), '240px');
  drag(env, handle, 96);
  handle.emit('keydown', {key:'ArrowRight'});
  assert.equal(employeeWidth(env), '352px');
  assert.equal(width(env), '300px');
  assert.equal(employeeWidth(setupEmployees(env.storage.get('agentsteam.employees.width.v1'))), '352px');
  drag(env, handle, 2000); assert.equal(employeeWidth(env), '440px');
  env.get('#employees-view').clientWidth = 600;
  env.run('EmployeeLayout.render()'); assert.equal(employeeWidth(env), '268px');
  drag(env, handle, -2000); assert.equal(employeeWidth(env), '180px');
});

test('队员分栏取消拖动恢复原值，手机保留桌面偏好', () => {
  const env = setupEmployees('320'); const handle = env.get('#employee-resizer');
  drag(env, handle, 64, 'pointercancel');
  assert.equal(employeeWidth(env), '320px');
  assert.equal(env.storage.get('agentsteam.employees.width.v1'), '320');
  env.resize(390); assert.equal(handle.hidden, true);
  drag(env, handle, -500);
  assert.equal(env.storage.get('agentsteam.employees.width.v1'), '320');
  env.resize(1440); assert.equal(handle.hidden, false);
  assert.equal(employeeWidth(env), '320px');
});
