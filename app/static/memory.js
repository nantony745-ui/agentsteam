'use strict';

const MemoryUI = (() => {
  const $ = id => document.getElementById(id);
  const labels = { queued: '待提交', accepted: '已接收', written: '事实已写入', consolidating: '归纳处理中', complete: '归纳完成', failed: '失败', deleted: '已删除' };
  let config;
  let timer;
  let visible = false;
  let selected;
  let offset = 0;
  let generation = 0;

  function node(tag, text) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function renderResults(target, data, onSource = id => act(target, () => openSource(id))) {
    target.replaceChildren();
    if (data.text) target.append(node('p', data.text));
    const items = data.items || data.results || [];
    for (const item of items) {
      const card = node('article'); card.className = 'memory-result';
      card.append(node('p', item.text || item.content || '无正文'));
      if (item.context) card.append(node('p', item.context));
      if (item.created_at) card.append(node('time', new Date(item.created_at).toLocaleString()));
      const source = item.document_id || item.metadata?.source_id;
      if (source) {
        const button = node('button', '查看来源'); button.type = 'button'; button.title = source;
        button.addEventListener('click', () => onSource(source)); card.append(button);
      }
      const evidence = node('details'); evidence.append(node('summary', '来源与检索依据'), node('pre', JSON.stringify(item, null, 2)));
      card.append(evidence); target.append(card);
    }
    if (!items.length && !data.text) target.append(node('p', '没有找到相关记忆。'));
    if (data.text) {
      const evidence = node('details'); evidence.append(node('summary', '回答依据'), node('pre', JSON.stringify(data, null, 2))); target.append(evidence);
    }
  }

  async function api(path, method = 'GET', body) {
    const response = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '记忆请求失败，请检查输入');
    return data;
  }

  async function act(button, action) {
    button.disabled = true;
    $('memory-error').textContent = '';
    try { await action(); } catch (error) { $('memory-error').textContent = error.message; }
    finally { button.disabled = false; }
  }

  function field(parent, id, label, type, value) {
    const row = node('label', label);
    row.htmlFor = id;
    const input = node('input');
    input.id = id; input.type = type; input.autocomplete = type === 'password' ? 'new-password' : 'off';
    if (type === 'checkbox') input.checked = value;
    else input.value = value || '';
    row.append(input); parent.append(row);
    return input;
  }

  function choice(parent, id, label, values, value) {
    const row = node('label', label), select = node('select');
    row.htmlFor = id; select.id = id;
    for (const [key, text] of values) { const option = node('option', text); option.value = key; select.append(option); }
    select.value = value; row.append(select); parent.append(row);
    return select;
  }

  function modelValue(key) {
    const value = $(`memory-${key}-model`).value;
    return value === '__manual__' ? $(`memory-${key}-custom-model`).value : value;
  }

  function modelConnection(key) {
    return { provider: $(`memory-${key}-provider`).value, base_url: $(`memory-${key}-url`).value,
      model: modelValue(key), api_key: $(`memory-${key}-key`).value || null };
  }

  function feedback(status, state, text) {
    status.className = `memory-connection-status ${state}`;
    status.textContent = text;
  }

  function modelPicker(group, key) {
    const controls = node('div'); controls.className = 'memory-model-control';
    const select = choice(controls, `memory-${key}-model`, '模型', [], '');
    const detect = node('button', '检测模型'); detect.type = 'button'; detect.id = `memory-${key}-detect`;
    controls.append(detect); group.append(controls);
    const custom = field(group, `memory-${key}-custom-model`, '手动模型名称', 'text', '');
    custom.parentElement.hidden = true;
    const status = node('p', '填写 API 地址和密钥后检测，也可手动填写模型。');
    status.id = `memory-${key}-models-status`; status.setAttribute('role', 'status'); group.append(status);
    select.setAttribute('aria-describedby', status.id);
    function options(models, value) {
      select.replaceChildren();
      const entries = [['', '请选择模型'], ...models.map(id => [id, id])];
      if (value && !models.includes(value)) entries.push([value, `${value}（当前配置）`]);
      entries.push(['__manual__', '手动填写…']);
      for (const [id, text] of entries) { const option = node('option', text); option.value = id; select.append(option); }
      select.value = value; custom.parentElement.hidden = true;
    }
    options([], config[key].model);
    select.addEventListener('change', () => {
      custom.parentElement.hidden = select.value !== '__manual__';
      if (!custom.parentElement.hidden) custom.focus();
    });
    const connection = () => ({ provider: $(`memory-${key}-provider`).value,
      base_url: $(`memory-${key}-url`).value, api_key: $(`memory-${key}-key`).value || null });
    for (const suffix of ['provider', 'url', 'key']) $(`memory-${key}-${suffix}`).addEventListener('input', () => {
      options([], modelValue(key)); feedback(status, '', '连接已修改，请重新检测模型。');
    });
    detect.addEventListener('click', async () => {
      const body = connection(), signature = JSON.stringify(body);
      detect.disabled = true; feedback(status, 'pending', '正在检测模型…');
      try {
        const result = await api(`/api/settings/memory/models/${key}`, 'POST', body);
        if (!select.isConnected || signature !== JSON.stringify(connection())) return;
        options(result.models, modelValue(key));
        feedback(status, 'success', `检测成功：共 ${result.models.length} 个模型。选择后测试对应功能的连接。`);
      } catch (error) {
        if (select.isConnected && signature === JSON.stringify(connection())) {
          feedback(status, 'failure', `检测失败：${error.message}`);
        }
      } finally { detect.disabled = false; }
    });
  }

  function renderConfig() {
    const form = $('memory-config'); form.replaceChildren();
    const switches = node('div'); switches.className = 'memory-switches';
    for (const [key, text] of [['enabled', '启用长期记忆'], ['auto_retain', '自动沉淀已完成回合'], ['auto_recall', '执行前自动检索']]) field(switches, `memory-${key}`, text, 'checkbox', config[key]);
    const turns = field(switches, 'memory-auto_retain_turns', '自动沉淀轮数', 'number', config.auto_retain_turns ?? 3);
    turns.min = '1'; turns.step = '1'; turns.required = true;
    switches.append(node('p', '队员跨会话累计，普通会话单独累计；修改后在下一次符合条件的回合完成时组批。'));
    form.append(switches);
    const database = node('div'); database.className = 'memory-database';
    field(database, 'memory-database', `数据库连接串${config.database_configured ? '（已配置，留空保留）' : '（专用库 agentsteam_memory）'}`, 'password', '');
    probeButton(database, 'database'); form.append(database);
    for (const [key, title] of [['llm', '生成模型'], ['embedding', 'Embedding'], ['rerank', 'Rerank']]) {
      const group = node('fieldset'); group.className = 'memory-model-group'; group.append(node('legend', title));
      const values = key === 'llm' ? [['openai-responses', 'OpenAI Responses 兼容'], ['openai', 'OpenAI Chat 兼容']] : key === 'embedding' ? [['openai', 'OpenAI 兼容']] : [['siliconflow', '硅基流动'], ['cohere', 'Cohere 兼容']];
      choice(group, `memory-${key}-provider`, '提供商', values, config[key].provider);
      field(group, `memory-${key}-url`, 'API 地址（基础地址或完整接口地址）', 'url', config[key].base_url);
      field(group, `memory-${key}-key`, `API Key${config[key].key_configured ? '（已配置，留空保留）' : ''}`, 'password', '');
      form.append(group); modelPicker(group, key);
      if (key === 'embedding') {
        const dimensions = field(group, 'memory-dimensions', '向量维度（自动检测，写入后锁定）', 'number', config[key].model ? config.embedding_dimensions : '');
        dimensions.readOnly = true; dimensions.min = '1'; dimensions.max = '2000';
        dimensions.placeholder = '选择模型后自动检测';
        const manual = field(group, 'memory-dimensions-manual', '连接失败后手动设置维度（1–2000）', 'checkbox', false);
        manual.parentElement.hidden = true;
        manual.addEventListener('change', () => { dimensions.readOnly = !manual.checked; });
      }
      if (key === 'llm') choice(group, 'memory-reasoning', '思考强度', [['low', '低'], ['medium', '中'], ['high', '高']], config.reasoning_effort);
      probeButton(group, key);
    }
    const save = node('button', '保存配置'); save.type = 'submit'; save.className = 'settings-primary memory-save'; form.append(save);
    $('memory-runtime').textContent = `引擎：${{ready:'可用',starting:'正在连接',disabled:'未启用',unavailable:'不可用'}[config.runtime]}${config.error ? ' · ' + config.error : ''}`;
  }

  function payload() {
    const data = { enabled: $('memory-enabled').checked, auto_retain: $('memory-auto_retain').checked, auto_recall: $('memory-auto_recall').checked, auto_retain_turns: Number($('memory-auto_retain_turns').value),
      database_url: $('memory-database').value || null, embedding_dimensions: $('memory-dimensions').value ? Number($('memory-dimensions').value) : null, reasoning_effort: $('memory-reasoning').value };
    for (const key of ['llm', 'embedding', 'rerank']) data[key] = modelConnection(key);
    return data;
  }

  function probeButton(parent, component) {
    const button = node('button', '测试连接'); button.type = 'button'; button.id = `memory-${component}-probe`;
    const status = node('p'); status.id = `memory-${component}-probe-status`; status.setAttribute('role', 'status');
    button.setAttribute('aria-describedby', status.id); parent.append(button, status);
    let revision = 0;
    function invalidate(event) {
      revision++; feedback(status, '', '配置已修改，请重新测试连接。');
      if (event.target === $('memory-dimensions-manual')) return;
      if (component === 'embedding' && event.target !== $('memory-dimensions')) {
        $('memory-dimensions').value = ''; $('memory-dimensions').readOnly = true;
        $('memory-dimensions-manual').checked = false; $('memory-dimensions-manual').parentElement.hidden = true;
      }
    }
    parent.addEventListener('input', invalidate);
    async function test() {
      const stamp = revision;
      button.disabled = true; feedback(status, 'pending', '正在测试连接…'); status.setAttribute('aria-busy', 'true');
      try {
        const body = component === 'database' ? { database_url: $('memory-database').value || null } : { connection: modelConnection(component) };
        if (component === 'embedding') {
          const current = body.connection, saved = config.embedding;
          const base = url => url.trim().replace(/\/+$/, '').replace(/\/(embeddings)$/, '');
          const unchanged = current.model === saved.model && base(current.base_url) === base(saved.base_url) && current.provider === saved.provider;
          body.embedding_dimensions = $('memory-dimensions-manual').checked ? Number($('memory-dimensions').value) : unchanged ? config.embedding_dimensions : null;
        }
        const result = await api(`/api/settings/memory/probe/${component}`, 'POST', body);
        if (!status.isConnected || stamp !== revision) return;
        if (component === 'embedding') {
          $('memory-dimensions').value = result.dimensions;
          $('memory-dimensions').readOnly = true; $('memory-dimensions-manual').checked = false;
          $('memory-dimensions-manual').parentElement.hidden = true;
        }
        feedback(status, 'success', result.message || `连接成功：${JSON.stringify(result)}`);
      } catch (error) {
        if (!status.isConnected || stamp !== revision) return;
        feedback(status, 'failure', `测试失败：${error.message}`);
        if (component === 'embedding') $('memory-dimensions-manual').parentElement.hidden = false;
      } finally { button.disabled = false; status.removeAttribute('aria-busy'); }
    }
    button.addEventListener('click', test);
    if (component === 'embedding') parent.addEventListener('change', event => {
      if ((event.target.id === 'memory-embedding-model' && event.target.value !== '__manual__') ||
          event.target.id === 'memory-embedding-custom-model') {
        if (modelValue('embedding')) test();
      }
    });
  }

  async function refresh() {
    const stamp = generation;
    const [sources, operations, status] = await Promise.all([api('/api/memory/sources'), api('/api/memory/operations'), api('/api/settings/memory')]);
    if (!visible || stamp !== generation) return;
    $('memory-runtime').textContent = status.error || (status.runtime === 'ready' ? '共用记忆池可用' : status.enabled ? '记忆引擎连接中或不可用' : '长期记忆未启用');
    const list = $('memory-sources'); list.replaceChildren();
    for (const source of sources) {
      const button = node('button', `${source.id.startsWith('run:') ? '会话沉淀' : '主动保存'} · v${source.version} · ${labels[source.status] || source.status}`);
      button.type = 'button'; button.title = source.id;
      button.addEventListener('click', () => act(button, () => openSource(source.id)));
      list.append(button);
    }
    if (!sources.length) list.append(node('p', `还没有记忆来源。可手动保存，或启用自动沉淀后在同一会话完成 ${status.auto_retain_turns ?? 3} 轮对话；不足一批的内容会保留等待。`));
    const jobs = $('memory-operations'); jobs.replaceChildren();
    for (const operation of operations) {
      const row = node('div', `${operation.source_id} · v${operation.version} · ${labels[operation.status] || operation.status}${operation.error ? ' · ' + operation.error : ''}`);
      if (operation.status === 'failed') {
        const retry = node('button', '重试'); retry.type = 'button';
        retry.addEventListener('click', () => act(retry, async () => { await api(`/api/memory/operations/${operation.id}/retry`, 'POST'); await refresh(); })); row.append(retry);
      }
      jobs.append(row);
    }
    clearTimeout(timer);
    if (visible && operations.some(op => ['queued','accepted','written','consolidating'].includes(op.status))) timer = setTimeout(() => refresh().catch(error => { $('memory-error').textContent = error.message; }), 3000);
  }

  async function openSource(id) {
    const data = await api(`/api/memory/sources/${encodeURIComponent(id)}`);
    selected = data; $('memory-source-detail').hidden = false;
    $('memory-source-title').textContent = `${data.id} · v${data.version}`;
    $('memory-source-content').value = data.revisions.at(-1).content;
    $('memory-source-content').disabled = Boolean(data.deleted);
    $('memory-revise').disabled = $('memory-delete').disabled = Boolean(data.deleted);
    $('memory-source-history').textContent = JSON.stringify(data.revisions, null, 2);
    const link = $('memory-session'); link.hidden = !data.session_id;
    link.onclick = async () => {
      try {
        const session = await api(`/api/sessions/${data.session_id}`);
        window.location.hash = '#workbench';
        await openSidebarSession(session);
      } catch (error) { $('memory-error').textContent = '来源会话已删除或不可用'; }
    };
  }

  async function show() {
    visible = true; const stamp = ++generation;
    const loaded = await api('/api/settings/memory');
    if (!visible || stamp !== generation) return;
    config = loaded; renderConfig(); await refresh();
  }

  function hide() { visible = false; generation++; clearTimeout(timer); }

  function init() {
    $('memory-config').addEventListener('submit', event => { event.preventDefault(); act(event.submitter, async () => {
      config = await api('/api/settings/memory', 'PUT', payload()); renderConfig(); $('memory-runtime').textContent = '配置已保存';
    }); });
    $('memory-retain').addEventListener('click', event => act(event.currentTarget, async () => {
      await api('/api/memory/retain', 'POST', { request_id: crypto.randomUUID(), content: $('memory-input').value });
      $('memory-input').value = ''; await refresh();
    }));
    for (const kind of ['recall', 'reflect']) $(`memory-${kind}`).addEventListener('click', event => act(event.currentTarget, async () => {
      const result = await api(`/api/memory/${kind}`, 'POST', { query: $('memory-query').value });
      renderResults($('memory-results'), result);
    }));
    $('memory-refresh').addEventListener('click', event => act(event.currentTarget, refresh));
    $('memory-revise').addEventListener('click', event => act(event.currentTarget, async () => {
      await api(`/api/memory/sources/${encodeURIComponent(selected.id)}`, 'PUT', { version: selected.version, content: $('memory-source-content').value });
      await openSource(selected.id); await refresh();
    }));
    $('memory-delete').addEventListener('click', event => act(event.currentTarget, async () => {
      if (!window.confirm('删除此来源及其关联记忆？旧版本内容仍保留在来源审计记录中。')) return;
      await api(`/api/memory/sources/${encodeURIComponent(selected.id)}`, 'DELETE', { version: selected.version });
      await openSource(selected.id); await refresh();
    }));
    async function facts() {
      const result = await api(`/api/memory/facts?kind=${$('memory-kind').value}&offset=${offset}`);
      renderResults($('memory-facts'), result);
      $('memory-page').textContent = `第 ${offset / 50 + 1} 页`;
    }
    $('memory-facts-load').addEventListener('click', event => act(event.currentTarget, async () => { offset = 0; await facts(); }));
    $('memory-next').addEventListener('click', event => act(event.currentTarget, async () => { offset += 50; await facts(); }));
    $('memory-prev').addEventListener('click', event => act(event.currentTarget, async () => { offset = Math.max(0, offset - 50); await facts(); }));
  }

  return { init, show, hide, renderResults, labels };
})();
