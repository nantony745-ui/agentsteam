'use strict';

const HarnessSettingsUI = (() => {
  const query = selector => document.querySelector(selector);
  const labels = { unverified: '未检测', probing: '检测中…', ready: '检测成功', failed: '检测失败' };
  const records = new Map();
  const drafts = new Map();
  const snapshots = new Map();
  const requests = new Map();
  const busy = new Set();
  const messages = new Map();
  let projectId;
  let selected = null;
  let visible = false;
  let loaded = false;
  let navigation = 0;
  let debounce = null;
  let saving = false;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  async function request(url, method, body) {
    const response = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '设置请求失败，请重试');
    return data;
  }

  function signature(id) {
    return JSON.stringify([projectId(), Object.entries(drafts.get(id) || {}).sort()]);
  }

  function icon(id) {
    const node = element('img', 'harness-icon');
    node.src = `/icons/agents/${id}.svg`;
    node.alt = '';
    return node;
  }

  function badge(record) {
    const status = record.enabled ? busy.has(record.id) ? 'probing' : record.status.state : 'disabled';
    const text = status === 'disabled' ? '未启用' : status === 'unverified' && !record.installed ? '未安装' : labels[status];
    const node = element('span', 'harness-badge', text);
    node.dataset.status = status;
    return node;
  }

  function renderList() {
    const list = query('#harness-list');
    list.replaceChildren();
    for (const record of records.values()) {
      const row = element('article', 'harness-row');
      row.dataset.selected = String(record.id === selected);
      const identity = element('a', 'harness-identity');
      identity.href = `#settings/${record.id}`;
      if (record.id === selected) identity.setAttribute('aria-current', 'page');
      const name = element('span', 'harness-name');
      name.append(element('strong', '', record.name), badge(record));
      identity.append(icon(record.id), name);
      const detect = element('button', 'harness-detect', '↻');
      detect.type = 'button';
      detect.title = `检测 ${record.name} 连接`;
      detect.setAttribute('aria-label', detect.title);
      detect.setAttribute('aria-busy', String(busy.has(record.id)));
      detect.disabled = busy.has(record.id) || !record.enabled;
      detect.addEventListener('click', () => probe(record.id, true));
      row.append(identity, detect);
      if (record.status.error) row.append(element('p', 'harness-row-error', record.status.error));
      list.append(row);
    }
  }

  function snapshotFor(id) {
    const entry = snapshots.get(id);
    return entry?.signature === signature(id) ? entry.snapshot : null;
  }

  function renderField(field, draft, invalid) {
    const row = element('div', 'harness-field');
    const info = element('div', 'harness-field-info');
    const label = element('label', '', field.name);
    label.setAttribute('for', `setting-${field.id}`);
    info.append(label);
    if (field.description) info.append(element('p', '', field.description));
    if (field.limitation) info.append(element('p', '', field.limitation));
    const defaultOption = field.options.find(option => option.value === field.default_value);
    if (field.supported) info.append(element('p', 'harness-default', `默认：${defaultOption?.name || field.default_value || '由 Harness 决定'}`));
    const control = element('div', 'harness-field-control');
    const select = element('select');
    select.id = `setting-${field.id}`;
    select.disabled = !field.supported || busy.has(selected) || saving;
    const inherit = element('option', '', field.supported ? '沿用默认' : '暂不支持');
    inherit.value = '';
    select.append(inherit);
    const groups = new Map();
    for (const item of field.options) {
      let parent = select;
      if (item.group) {
        if (!groups.has(item.group)) {
          const group = element('optgroup');
          group.label = item.group;
          groups.set(item.group, group);
          select.append(group);
        }
        parent = groups.get(item.group);
      }
      const option = element('option', '', item.name);
      option.value = item.value;
      parent.append(option);
    }
    if (draft[field.id] && !field.options.some(option => option.value === draft[field.id])) {
      const stale = element('option', '', `${draft[field.id]}（需重新选择）`);
      stale.value = draft[field.id];
      select.append(stale);
    }
    select.value = draft[field.id] || '';
    select.addEventListener('change', () => changeField(field.id, select.value));
    control.append(select);
    const problem = invalid.find(item => item.id === field.id);
    if (problem) control.append(element('p', 'form-error', problem.reason));
    const chosen = field.options.find(item => item.value === draft[field.id]);
    if (chosen?.description) control.append(element('p', 'harness-option-description', chosen.description));
    row.append(info, control);
    return row;
  }

  function renderDetail() {
    const record = records.get(selected);
    if (!record) return;
    const snapshot = snapshotFor(selected);
    const draft = drafts.get(selected);
    query('#settings-title').textContent = record.name;
    query('#settings-agent-icon').replaceChildren(icon(record.id));
    query('#settings-status').replaceChildren(badge(record));
    query('#settings-cwd').textContent = snapshot ? `检测目录：${snapshot.cwd}` : '';
    const fields = query('#harness-fields');
    fields.replaceChildren();
    if (snapshot) {
      for (const field of snapshot.fields) fields.append(renderField(field, draft, snapshot.invalid));
    } else {
      fields.append(element('p', 'settings-placeholder', busy.has(selected) ? '正在读取模型与配置能力…' : record.status.error || '尚未获取配置，请重新检测。'));
    }
    query('#settings-save').disabled = !snapshot || snapshot.invalid.length > 0 || busy.has(selected) || saving;
    const dirty = JSON.stringify(Object.entries(draft).sort()) !== JSON.stringify(Object.entries(record.settings.config_values).sort());
    query('#settings-feedback').textContent = messages.get(selected) || (dirty ? '有未保存的修改' : '配置由主会话与 Worker 共用');
  }

  function render() {
    if (!visible) return;
    query('#harness-detail').hidden = !selected;
    renderList();
    if (selected) renderDetail();
  }

  async function probe(id, force) {
    const version = (requests.get(id) || 0) + 1;
    requests.set(id, version);
    const stamp = signature(id);
    busy.add(id);
    messages.delete(id);
    render();
    try {
      const snapshot = await request(`/api/settings/harnesses/${id}/probe`, 'POST', {
        project_id: projectId(), config_values: { ...drafts.get(id) }, force,
      });
      if (requests.get(id) !== version || signature(id) !== stamp) return;
      snapshots.set(id, { signature: stamp, snapshot, time: Date.now() });
      records.get(id).status = { state: 'ready' };
    } catch (error) {
      if (requests.get(id) !== version || signature(id) !== stamp) return;
      snapshots.delete(id);
      records.get(id).status = { state: 'failed', error: error.message };
    } finally {
      if (requests.get(id) === version) busy.delete(id);
      render();
    }
  }

  function changeField(id, value) {
    const harness = selected;
    const previous = snapshotFor(harness);
    const draft = drafts.get(harness);
    if (value) draft[id] = value;
    else delete draft[id];
    messages.delete(harness);
    if (id === 'model') {
      requests.set(harness, (requests.get(harness) || 0) + 1);
      busy.delete(harness);
      clearTimeout(debounce);
      debounce = setTimeout(() => probe(harness, false), 250);
    } else if (previous) {
      previous.invalid = previous.invalid.filter(item => item.id !== id);
      snapshots.set(harness, { signature: signature(harness), snapshot: previous, time: Date.now() });
    }
    render();
  }

  async function save(event) {
    event.preventDefault();
    const id = selected;
    const snapshot = snapshotFor(id);
    if (saving || !snapshot || snapshot.invalid.length || busy.has(id)) return;
    saving = true;
    render();
    try {
      const settings = await request(`/api/settings/harnesses/${id}`, 'PUT', {
        project_id: projectId(), config_values: { ...drafts.get(id) },
      });
      records.get(id).settings = settings;
      messages.set(id, '已保存，将从下一轮开始应用');
    } catch (error) {
      messages.set(id, error.message);
    } finally {
      saving = false;
      render();
    }
  }

  async function navigate() {
    const version = ++navigation;
    const wasVisible = visible;
    visible = /^#settings(?:\/|$)/.test(window.location.hash);
    query('.app-shell').hidden = visible;
    query('#settings-view').hidden = !visible;
    const page = visible ? window.location.hash.split('?')[0] : '';
    const memoryPage = page === '#settings/memory';
    const teamsPage = page === '#settings/teams';
    if (typeof TeamsUI !== 'undefined') {
      query('#settings-teams-panel').hidden = !teamsPage;
      query('#settings-teams-nav').setAttribute('aria-current', teamsPage ? 'page' : 'false');
    }
    if (typeof MemoryUI !== 'undefined') {
      query('#settings-memory-panel').hidden = !memoryPage;
      query('#settings-memory-nav').setAttribute('aria-current', memoryPage ? 'page' : 'false');
      if (!memoryPage) MemoryUI.hide();
    }
    const previewPage = page === '#settings/skills' ? 'skills' : page === '#settings/mcp' ? 'mcp' : null;
    for (const name of ['harness', 'skills', 'mcp']) {
      const active = !memoryPage && !teamsPage && name === (previewPage || 'harness');
      query(`#settings-${name}-panel`).hidden = !active;
      query(`#settings-${name}-nav`).setAttribute('aria-current', active ? 'page' : 'false');
    }
    SettingsLayout.render();
    if (previewPage !== 'skills') HarnessSkillsUI.hide();
    if (previewPage !== 'mcp') HarnessMcpUI.hide();
    if (teamsPage && typeof TeamsUI !== 'undefined') {
      try { await TeamsUI.show(); } catch (error) { query('#teams-error').textContent = error.message; }
      return;
    }
    if (memoryPage && typeof MemoryUI !== 'undefined') {
      try { await MemoryUI.show(); } catch (error) { query('#memory-error').textContent = error.message; }
      return;
    }
    if (!visible) {
      if (wasVisible && !/^#employees(?:\/|$)/.test(window.location.hash)) query('#open-settings').focus({ preventScroll: true });
      return;
    }
    query('#settings-error').textContent = '';
    if (!loaded) {
      try {
        const items = await request('/api/settings/harnesses', 'GET');
        for (const item of items) {
          records.set(item.id, item);
          if (!drafts.has(item.id)) drafts.set(item.id, { ...item.settings.config_values });
        }
        loaded = true;
      } catch (error) {
        if (navigation === version) query(previewPage ? `#${previewPage}-error` : '#settings-error').textContent = error.message;
        return;
      }
    }
    if (navigation !== version) return;
    if (previewPage) {
      await (previewPage === 'skills' ? HarnessSkillsUI : HarnessMcpUI).show([...records.values()]);
      return;
    }
    const id = window.location.hash.split('/')[1];
    selected = records.has(id) ? id : selected || records.keys().next().value;
    render();
    query('#settings-title').setAttribute('tabindex', '-1');
    query('#settings-title').focus({ preventScroll: true });
    const cached = snapshots.get(selected);
    if (selected && records.get(selected).enabled && (!snapshotFor(selected) || Date.now() - cached.time >= 30000)) {
      await probe(selected, false);
    }
  }

  function init(currentProject) {
    projectId = currentProject;
    SettingsLayout.init();
    HarnessSkillsUI.init(navigate);
    HarnessMcpUI.init(navigate);
    if (typeof MemoryUI !== 'undefined') MemoryUI.init();
    query('#open-settings').addEventListener('click', () => { window.location.hash = '#settings'; });
    query('#harness-settings-form').addEventListener('submit', save);
    window.addEventListener('hashchange', navigate);
    navigate();
  }

  return { init };
})();
