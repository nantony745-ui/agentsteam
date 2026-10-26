'use strict';

const EmployeeMemoryUI = (() => {
  const $ = name => document.getElementById(`employee-memory-${name}`);
  let current = null;
  class StaleView extends Error {}
  const node = (tag, text) => { const element = document.createElement(tag); element.textContent = text; return element; };
  const active = view => current === view && /^#employees(?:\/|$)/.test(location.hash);

  async function api(view, path, method = 'GET', body) {
    if (!active(view)) throw new StaleView();
    const response = await fetch(view.base + path, {method, ...(body ? {
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    } : {})});
    const data = await response.json();
    if (!active(view)) throw new StaleView();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '记忆请求失败');
    return data;
  }

  async function act(button, callback) {
    const view = current;
    if (!view || !active(view)) return;
    button.disabled = true;
    $('error').textContent = '';
    try { await callback(view); }
    catch (error) { if (active(view) && !(error instanceof StaleView)) $('error').textContent = error.message; }
    finally {
      if (active(view)) button.disabled = button === $('prev') ? view.offset === 0 :
        button === $('next') ? view.offset + 50 >= view.total : false;
    }
  }

  function renderFacts(view, data) {
    const items = data.items || [];
    const total = data.total ?? items.length;
    const offset = view.offset;
    view.total = total;
    $('facts').replaceChildren();
    $('count').textContent = `${total} 条记忆`;
    $('page').textContent = `第 ${Math.floor(offset / 50) + 1} 页`;
    $('prev').disabled = offset === 0;
    $('next').disabled = offset + 50 >= total;
    if (!items.length) {
      const empty = node('div'); empty.className = 'employee-memory-empty';
      empty.append(node('span', '✦'), node('strong', '记忆库还是空的'), node('p', '开启个人记忆后，完成的工作会在这里逐渐积累。'));
      $('facts').append(empty);
      return;
    }
    for (const [index, item] of items.entries()) {
      const card = node('article'); card.className = 'employee-memory-item';
      const top = node('div'); top.className = 'employee-memory-item-top';
      top.append(node('span', `记忆 ${String(offset + index + 1).padStart(2, '0')}`));
      if (item.created_at) {
        const time = node('time', new Date(item.created_at).toLocaleString());
        time.dateTime = item.created_at; top.append(time);
      }
      card.append(top, node('p', item.text || item.content || '无正文'));
      $('facts').append(card);
    }
  }

  async function facts(view) {
    const stamp = ++view.facts;
    const offset = view.offset;
    const data = await api(view, `/facts?kind=all&offset=${offset}`);
    if (stamp === view.facts) renderFacts(view, data);
  }

  async function refresh(view) {
    const stamp = ++view.refresh;
    const settings = await api(view, '');
    if (stamp !== view.refresh) return;
    view.settings = settings;
    $('enabled').checked = settings.enabled;
    $('enabled').disabled = false;
    $('status').textContent = !settings.enabled ? '已关闭' : !settings.global_enabled ? '全局记忆未启用' :
      settings.error ? '服务不可用' : settings.runtime === 'ready' ? '运行中' : '连接中';
    $('status').dataset.state = !settings.enabled ? 'off' : settings.global_enabled && settings.runtime === 'ready' && !settings.error ? 'ready' : 'waiting';
    $('threshold').textContent = `每 ${settings.auto_retain_turns} 轮自动沉淀`;
    if (!settings.global_enabled) {
      view.total = 0;
      view.offset = 0;
      $('facts').replaceChildren(node('p', '启用全局长期记忆后可查看此队员的记忆。'));
      $('count').textContent = '已保存的记忆';
      $('prev').disabled = $('next').disabled = true;
      return;
    }
    await facts(view);
  }

  function select(employeeId) {
    current = null;
    $('section').hidden = !employeeId;
    for (const name of ['error', 'status', 'threshold', 'facts']) $(name).replaceChildren();
    $('count').textContent = '已保存的记忆';
    $('page').textContent = '第 1 页';
    $('enabled').checked = false;
    $('enabled').disabled = true;
    $('prev').disabled = $('next').disabled = true;
    if (!employeeId) return;
    current = {base: `/api/employees/${employeeId}/memory`, offset: 0, total: 0, facts: 0, refresh: 0};
    act($('refresh'), refresh);
  }

  $('enabled').addEventListener('change', () => {
    const enabled = $('enabled').checked;
    act($('enabled'), async view => {
      ++view.refresh;
      try { await api(view, '', 'PUT', {enabled}); }
      catch (error) { if (active(view)) $('enabled').checked = view.settings.enabled; throw error; }
      await refresh(view);
    });
  });
  $('refresh').addEventListener('click', () => act($('refresh'), refresh));
  for (const name of ['prev', 'next']) $(name).addEventListener('click', () => act($(name), async view => {
    const previous = view.offset;
    view.offset += name === 'next' ? 50 : -50;
    try { await facts(view); }
    catch (error) { view.offset = previous; throw error; }
  }));
  window.addEventListener('hashchange', () => { if (current && !active(current)) current = null; });
  return {select};
})();
