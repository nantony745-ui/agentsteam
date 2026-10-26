'use strict';

function createHarnessPreview({resource, detailPrefix, listKey, label, itemLabel, selectionParam, remoteDetail, renderDetail, clearDetail, description}) {
  const query = selector => document.querySelector(selector);
  const listNode = name => query(`#${resource}-${name}`);
  const detailNode = name => query(`#${detailPrefix}-${name}`);
  let records = [];
  let agent = 'codex';
  let selected = null;
  let entries = [];
  let note = '';
  let visible = false;
  let loadedKey = null;
  let listVersion = 0;
  let detailVersion = 0;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  async function request(url) {
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `读取 ${label} 失败，请重试`);
    return data;
  }

  function link(itemId) {
    const params = new URLSearchParams({ agent });
    if (itemId) params.set(selectionParam, itemId);
    return `#settings/${resource}?${params}`;
  }
  function navigate() { window.location.hash = link(null); }

  function placeholder(text) {
    detailNode('detail').hidden = true;
    detailNode('placeholder').hidden = false;
    detailNode('placeholder').textContent = text;
    detailNode('title').textContent = selected ? entries.find(item => item.id === selected)?.name || `${itemLabel} 预览` : `选择一个 ${itemLabel}`;
    clearDetail();
  }

  function renderFilters() {
    const harness = listNode('harness');
    harness.replaceChildren();
    for (const record of records) {
      const option = element('option', '', record.name);
      option.value = record.id;
      harness.append(option);
    }
    harness.value = agent;
  }

  function renderList() {
    const term = listNode('search').value.trim().toLocaleLowerCase();
    const filtered = entries.filter(item => [item.name, item.slug, item.description, item.path, item.transport].some(value => (value || '').toLocaleLowerCase().includes(term)));
    listNode('count').textContent = term ? `${filtered.length} / ${entries.length}` : String(entries.length);
    const list = listNode('list');
    list.replaceChildren();
    for (const item of filtered) {
      const card = element('a', 'skill-list-item');
      card.href = link(item.id);
      card.dataset.skillId = item.id;
      card.setAttribute('aria-current', item.id === selected ? 'true' : 'false');
      const heading = element('div', 'skill-list-item-heading');
      heading.append(element('strong', '', item.name));
      card.append(heading);
      const summary = description(item);
      if (summary) card.append(element('p', 'skill-list-description', summary));
      card.append(element('p', 'skill-list-path', item.path));
      if (item.error) card.append(element('p', 'form-error', item.error));
      list.append(card);
    }
    if (!filtered.length) list.append(element('p', 'skills-empty', term ? `没有匹配的 ${label}` : `未发现全局 ${label}`));
  }

  function renderLocations(locations) {
    const list = listNode('location-list');
    list.replaceChildren();
    for (const location of locations) {
      const row = element('p', '', `${location.path} · ${location.error || (location.exists ? `${resource === 'mcp' ? '文件' : '目录'}存在` : `${resource === 'mcp' ? '文件' : '目录'}不存在`)}`);
      if (location.error) row.className = 'form-error';
      list.append(row);
    }
  }

  async function preview(id) {
    selected = id;
    const version = ++detailVersion;
    const key = agent;
    renderList();
    placeholder(id ? `正在读取 ${itemLabel}…` : note || `从左侧选择 ${itemLabel}，查看内容与来源。`);
    if (!id) return;
    try {
      const data = remoteDetail ? await request(`/api/settings/harnesses/${agent}/${resource}/${encodeURIComponent(id)}`) : entries.find(item => item.id === id);
      if (!visible || version !== detailVersion || key !== agent) return;
      renderDetail(data);
      detailNode('placeholder').hidden = true;
      detailNode('detail').hidden = false;
      detailNode('preview-scroll').scrollTop = 0;
    } catch (error) {
      if (visible && version === detailVersion && key === agent) placeholder(`无法预览：${error.message}。可刷新后重试。`);
    }
  }

  async function load(requested) {
    const version = ++listVersion;
    ++detailVersion;
    const key = agent;
    selected = null;
    entries = [];
    note = '';
    if (listNode('note')) listNode('note').textContent = '';
    loadedKey = null;
    listNode('error').textContent = '';
    listNode('refresh').disabled = false;
    listNode('list').setAttribute('aria-busy', 'false');
    renderLocations([]);
    renderList();
    placeholder(`正在发现全局 ${label}…`);
    listNode('refresh').disabled = true;
    listNode('list').setAttribute('aria-busy', 'true');
    try {
      const data = await request(`/api/settings/harnesses/${agent}/${resource}`);
      if (!visible || version !== listVersion || key !== agent) return;
      entries = data[listKey];
      note = data.note || '';
      const noteNode = listNode('note');
      if (noteNode) noteNode.textContent = note;
      loadedKey = key;
      renderLocations(data.locations);
      if (resource === 'mcp') listNode('error').textContent = data.locations.filter(item => item.error).map(item => `${item.path}：${item.error}`).join('；');
      await preview(entries.some(item => item.id === requested) ? requested : entries[0]?.id || null);
    } catch (error) {
      if (visible && version === listVersion && key === agent) {
        listNode('error').textContent = error.message;
        placeholder(`未能读取 ${label}，请刷新重试。`);
      }
    } finally {
      if (version === listVersion) {
        listNode('refresh').disabled = false;
        listNode('list').setAttribute('aria-busy', 'false');
      }
    }
  }

  async function show(items) {
    visible = true;
    records = items;
    const params = new URLSearchParams(window.location.hash.split('?')[1] || '');
    agent = records.some(item => item.id === params.get('agent')) ? params.get('agent') : records[0]?.id || 'codex';
    if (loadedKey !== agent) {
      ++listVersion;
      ++detailVersion;
      selected = null;
      entries = [];
      renderList();
      renderLocations([]);
      placeholder(`正在读取 ${label}…`);
    }
    renderFilters();
    if (loadedKey === agent) {
      const id = params.get(selectionParam);
      if (id && id !== selected) await preview(id);
      return;
    }
    await load(params.get(selectionParam));
  }

  function hide() {
    visible = false;
    ++listVersion;
    ++detailVersion;
    loadedKey = null;
    listNode('refresh').disabled = false;
    listNode('list').setAttribute('aria-busy', 'false');
  }

  function init(retryInitialization) {
    listNode('harness').addEventListener('change', event => { agent = event.target.value; navigate(); });
    listNode('search').addEventListener('input', renderList);
    listNode('refresh').addEventListener('click', () => records.length ? load(selected) : retryInitialization());
  }

  return { init, show, hide };
}
