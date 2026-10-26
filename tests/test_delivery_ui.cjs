const assert = require('node:assert/strict');
const { test } = require('node:test');
const { environment } = require('./frontend_dom.cjs');

function setup() {
  const env = environment();
  env.run(`
    const taskFixture = {id: 'task-a', status: 'review', branch: 'harness/task-a', base_sha: 'a'.repeat(40), worktree: 'D:/worktrees/task-a'};
    const diffFixture = {head_sha: 'b'.repeat(40), patch: 'deleted file mode 100644\\n+<script>unsafe()</script>\\nBinary files differ',
      status: ' D old.txt', untracked: ['中文 space.txt', 'binary.bin']};
    const contextA = {projectId: 'project', sessionId: 'worker-a', generation: 0, task: taskFixture};
    state.context = contextA;
    api = async path => path.includes('/sessions/') ? {session: {cwd: taskFixture.worktree}, task: taskFixture} : diffFixture;
  `);
  return env;
}

test('任务变更显示完整 SHA、删除、二进制及新增文件，正文按纯文本渲染', async () => {
  const env = setup();
  await env.run('refreshTaskChanges(contextA)');
  assert.equal(env.get('#task-changes').hidden, false);
  assert.equal(env.get('#task-diff-content').hidden, false);
  assert.match(env.get('#task-diff-meta').textContent, /待验收/);
  assert.match(env.get('#task-diff-meta').textContent, new RegExp('a'.repeat(40)));
  assert.match(env.get('#task-diff-meta').textContent, new RegExp('b'.repeat(40)));
  assert.match(env.get('#task-diff-patch').textContent, /deleted file mode/);
  assert.match(env.get('#task-diff-patch').textContent, /Binary files/);
  assert.match(env.get('#task-diff-patch').textContent, /<script>/);
  assert.equal(env.get('#task-diff-patch').innerHTML, '');
  assert.deepEqual(env.get('#task-untracked').children.map(button => button.textContent), ['中文 space.txt', 'binary.bin']);
  assert.equal(env.get('#session-cwd').textContent, 'D:/worktrees/task-a');
});

test('共享目录和普通指定目录只显示说明，不请求 Diff 或残留预览', async () => {
  const env = setup();
  await env.run('refreshTaskChanges(contextA)');
  await env.run(`(async () => {
    for (const working_dir of [null, 'D:/ordinary']) {
      api = async path => {
        if (!path.includes('/sessions/')) throw new Error('不应请求 Diff');
        return {session: {cwd: working_dir || 'D:/parent'}, task: {...taskFixture, worktree: null, working_dir}};
      };
      $('#task-file-preview').textContent = '旧文件';
      await refreshTaskChanges(contextA);
      if (!$('#task-diff-content').hidden) throw new Error('Diff 未隐藏');
      if ($('#task-file-preview').textContent !== '') throw new Error('残留预览');
      const expected = working_dir ? '未绑定独立 worktree' : '与父会话共享';
      if (!$('#task-diff-message').textContent.includes(expected)) throw new Error('目录说明错误');
    }
  })()`);
  assert.match(env.get('#task-diff-meta').textContent, /D:\/ordinary/);
  assert.equal(env.get('#task-diff-patch').textContent, '');
  assert.equal(env.get('#task-untracked').children.length, 0);
});

test('detached HEAD 显示明确分支说明，共享切到已绑定后恢复 Diff', async () => {
  const env = setup();
  await env.run(`api = async () => ({session: {cwd: 'D:/parent'}, task: {...taskFixture, worktree: null}});
    refreshTaskChanges(contextA)`);
  assert.equal(env.get('#task-diff-content').hidden, true);
  await env.run(`api = async path => path.includes('/sessions/')
    ? {session: {}, task: {...taskFixture, branch: null}} : diffFixture;
    refreshTaskChanges(contextA)`);
  assert.match(env.get('#task-diff-meta').textContent, /detached HEAD/);
  assert.equal(env.get('#task-diff-content').hidden, false);
});

test('点击新增文件编码完整路径并显示文本、空文件、二进制与截断原因', async () => {
  const env = setup();
  await env.run('refreshTaskChanges(contextA)');
  await env.run(`(async () => {
    let request;
    api = async path => {request = path; return {kind: 'text', content: '<img src=x>', truncated: true, reason: '超过 1 MiB，已截断'};};
    await loadTaskPreview(contextA, taskFixture.id, '中文 space.txt', contextA.diffVersion);
    if (!request.includes(new URLSearchParams({path: '中文 space.txt'}).toString())) throw new Error('路径未完整编码');
  })()`);
  assert.match(env.get('#task-preview-message').textContent, /已截断/);
  assert.equal(env.get('#task-file-preview').textContent, '<img src=x>');
  assert.equal(env.get('#task-file-preview').innerHTML, '');
  await env.run(`api = async () => ({kind: 'binary', content: null, reason: '内容包含 NUL 字节', truncated: false});
    loadTaskPreview(contextA, taskFixture.id, 'binary.bin', contextA.diffVersion)`);
  assert.match(env.get('#task-preview-message').textContent, /二进制文件/);
  assert.equal(env.get('#task-file-preview').textContent, '');
  await env.run(`api = async () => ({kind: 'text', content: '', reason: null, truncated: false});
    loadTaskPreview(contextA, taskFixture.id, 'empty.txt', contextA.diffVersion)`);
  assert.match(env.get('#task-preview-message').textContent, /空文件/);
});

test('未准备和预览错误可见，刷新成功后恢复', async () => {
  const env = setup();
  await env.run(`api = async path => {
    if (path.includes('/sessions/')) return {session: {}, task: taskFixture};
    throw new Error('尚无隔离交付目录');
  }; refreshTaskChanges(contextA)`);
  assert.match(env.get('#task-diff-message').textContent, /尚无隔离交付目录/);
  await env.run(`api = async path => path.includes('/sessions/') ? {session: {}, task: taskFixture} : diffFixture;
    refreshTaskChanges(contextA)`);
  assert.equal(env.get('#task-diff-message').textContent, '已读取当前变更');
  await env.run(`api = async () => {throw new Error('文件不存在');};
    loadTaskPreview(contextA, taskFixture.id, 'gone.txt', contextA.diffVersion)`);
  assert.match(env.get('#task-preview-message').textContent, /预览失败：文件不存在/);
});

test('切换 Worker 后旧 Diff 成功或失败响应不覆盖新视图，同会话旧刷新也不覆盖新刷新', async () => {
  const env = setup();
  await env.run(`(async () => {
    let resolveOld;
    api = async () => new Promise(resolve => {resolveOld = resolve;});
    const old = refreshTaskChanges(contextA);
    state.context = {...contextA};
    $('#task-diff-message').textContent = '新视图';
    resolveOld({session: {}, task: taskFixture});
    await old;
    if ($('#task-diff-message').textContent !== '新视图') throw new Error('旧成功响应覆盖新视图');
    state.context = contextA;
    let rejectOld;
    api = async () => new Promise((resolve, reject) => {rejectOld = reject;});
    const failed = refreshTaskChanges(contextA);
    state.context = {...contextA};
    $('#task-diff-message').textContent = '新视图';
    rejectOld(new Error('旧失败'));
    await failed;
    if ($('#task-diff-message').textContent !== '新视图') throw new Error('旧失败覆盖新视图');
    state.context = contextA;
    api = async () => new Promise(resolve => {resolveOld = resolve;});
    const earlier = refreshTaskChanges(contextA);
    api = async path => path.includes('/sessions/') ? {session: {}, task: taskFixture} : diffFixture;
    await refreshTaskChanges(contextA);
    resolveOld({session: {}, task: null});
    await earlier;
    if ($('#task-changes').hidden) throw new Error('旧刷新隐藏新内容');
  })()`);
});

test('预览切换、Diff 刷新和会话切换均使旧预览失效', async () => {
  const env = setup();
  await env.run('refreshTaskChanges(contextA)');
  await env.run(`(async () => {
    let resolveOld;
    api = async () => new Promise(resolve => {resolveOld = resolve;});
    const earlier = loadTaskPreview(contextA, taskFixture.id, 'old.txt', contextA.diffVersion);
    api = async () => ({kind: 'text', content: 'new', truncated: false});
    await loadTaskPreview(contextA, taskFixture.id, 'new.txt', contextA.diffVersion);
    resolveOld({kind: 'text', content: 'old'});
    await earlier;
    if ($('#task-file-preview').textContent !== 'new') throw new Error('旧文件覆盖新文件');
    api = async () => new Promise(resolve => {resolveOld = resolve;});
    const beforeRefresh = loadTaskPreview(contextA, taskFixture.id, 'old.txt', contextA.diffVersion);
    contextA.diffVersion += 1;
    $('#task-file-preview').textContent = 'refreshed';
    resolveOld({kind: 'text', content: 'old'});
    await beforeRefresh;
    if ($('#task-file-preview').textContent !== 'refreshed') throw new Error('刷新未废弃旧预览');
    const beforeSwitch = loadTaskPreview(contextA, taskFixture.id, 'old.txt', contextA.diffVersion);
    state.context = {...contextA};
    $('#task-file-preview').textContent = 'switched';
    resolveOld({kind: 'text', content: 'old'});
    await beforeSwitch;
    if ($('#task-file-preview').textContent !== 'switched') throw new Error('切换未废弃旧预览');
  })()`);
});

test('目录就绪与回合完成触发刷新，切回主会话清理任务面板', async () => {
  const env = setup();
  env.run(`let refreshCount = 0;
    refreshTaskChanges = context => {if (context !== contextA) throw new Error('错误上下文'); refreshCount++;};
    scheduleSidebarRefresh = () => {};
    renderEvent({kind: 'task.worktree_ready', task_id: taskFixture.id, payload: {}}, contextA);
    refreshRun = () => {}; refreshSessionNativeId = () => {}; refreshRunFileChanges = () => {};
    updateRunHeader = () => {}; renderRunItem = () => {};
    applyRunStatusEvent({run_id: 'run', session_id: 'worker-a', payload: {status: 'completed'}}, {}, contextA);
    if (refreshCount !== 2) throw new Error('刷新触发缺失');
    resetConversation();
  `);
  assert.equal(env.get('#task-changes').hidden, true);
  assert.equal(env.get('#task-diff-meta').textContent, '');
  assert.equal(env.get('#task-file-preview').textContent, '');
});

test('手动刷新绑定当前会话，重放相同目录就绪事件只刷新一次', () => {
  const env = setup();
  env.run(`bindEvents(); let calls = 0;
    refreshTaskChanges = context => {if (context !== contextA) throw new Error('错误会话'); calls++;};
    scheduleSidebarRefresh = () => {};
    $('#task-diff-refresh').click();
    contextA.lastEventId = 0;
    const event = {id: 2, kind: 'task.worktree_ready', project_id: 'project', session_id: 'worker-a',
      task_id: taskFixture.id, payload: {}};
    applyEvent(event, contextA);
    applyEvent(event, contextA);
    if (calls !== 2 || contextA.lastEventId !== 2) throw new Error('重复事件重复刷新');
    state.context = {...contextA};
    applyEvent({...event, id: 3}, contextA);
    if (calls !== 2) throw new Error('旧会话事件触发刷新');
  `);
});
