'use strict';

const ProjectGitUI = (() => {
  const q = selector => document.querySelector(selector);
  let context = null;
  let version = 0;
  let data = null;
  let busy = false;

  function endpoint(target, initialize) {
    const query = target.sessionId ? `?${new URLSearchParams({session_id: target.sessionId})}` : '';
    return `/api/projects/${encodeURIComponent(target.projectId)}/git${initialize ? '/init' : ''}${query}`;
  }

  function render(error) {
    const button = q('#project-git-toggle');
    q('#project-git').hidden = !context;
    if (!context) return;
    const label = error ? 'Git 检查失败' : !data ? '检查 Git…' : !data.is_repo ? '未初始化 Git'
      : data.detached ? `游离 HEAD · ${data.short_sha}` : data.branch || 'Git 仓库';
    q('#project-git-label').textContent = label;
    button.title = `${context.path}\n${label}`;
    q('#project-git-message').textContent = error || (busy ? '正在读取…' : !data?.is_repo
      ? '此目录可直接使用，也可初始化 Git 以跟踪文件变更。'
      : !data.has_commits ? '仓库已初始化，尚无提交。' : data.changes.length ? `${data.changes.length} 项文件变更` : '工作区干净');
    q('#project-git-path').textContent = context.path;
    q('#project-git-root').textContent = data?.root || '—';
    q('#project-git-relative').textContent = data?.relative_path === '.' ? '仓库根目录' : data?.relative_path || '—';
    q('#project-git-head').textContent = data?.is_repo ? `${data.branch || '游离 HEAD'}${data.short_sha ? ` · ${data.short_sha}` : ''}` : '—';
    q('#project-git-init').hidden = !data || data.is_repo || Boolean(error);
    q('#project-git-init').disabled = busy;
    q('#project-git-refresh').disabled = busy;
    const changes = q('#project-git-changes');
    changes.replaceChildren();
    for (const change of data?.changes || []) {
      const row = document.createElement('li');
      const status = document.createElement('code');
      status.textContent = change.status;
      const file = document.createElement('span');
      file.textContent = change.original_path ? `${change.original_path} → ${change.path}` : change.path;
      row.append(status, file);
      changes.append(row);
    }
  }

  async function refresh(initialize = false) {
    if (!context || (initialize && busy)) return;
    const target = context;
    const current = ++version;
    busy = true;
    render('');
    try {
      const response = await fetch(endpoint(target, initialize), initialize ? {method: 'POST'} : {});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '无法读取 Git 状态');
      if (version !== current) return;
      data = result;
      busy = false;
      render('');
    } catch (error) {
      if (version !== current) return;
      busy = false;
      render(error.message);
    }
  }

  function close() {
    q('#project-git-panel').hidePopover();
    q('#project-git-panel').hidden = true;
    q('#project-git-toggle').setAttribute('aria-expanded', 'false');
  }

  function setContext(target) {
    const next = target.projectId && target.path ? target : null;
    if (JSON.stringify(next) === JSON.stringify(context)) return;
    version++;
    context = next;
    data = null;
    busy = false;
    close();
    render('');
    if (context) return refresh();
  }

  function bind() {
    q('#project-git-toggle').addEventListener('click', () => {
      const panel = q('#project-git-panel');
      if (!panel.hidden) { close(); return; }
      const anchor = q('#project-git-toggle').getBoundingClientRect();
      const width = Math.min(480, window.innerWidth - 32);
      panel.style.left = `${Math.max(16, Math.min(anchor.left, window.innerWidth - width - 16))}px`;
      panel.style.bottom = `${window.innerHeight - anchor.top + 8}px`;
      panel.style.maxHeight = `${Math.min(420, anchor.top - 16)}px`;
      panel.hidden = false;
      panel.showPopover();
      q('#project-git-toggle').setAttribute('aria-expanded', 'true');
      refresh();
    });
    q('#project-git-close').addEventListener('click', close);
    q('#project-git-refresh').addEventListener('click', () => refresh());
    q('#project-git-init').addEventListener('click', () => refresh(true));
    window.addEventListener('resize', close);
    q('#message-form').addEventListener('scroll', close);
    document.addEventListener('click', event => {
      if (!q('#project-git').contains(event.target)) close();
    });
    q('#project-git').addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        close();
        q('#project-git-toggle').focus();
      }
    });
  }

  return {bind, setContext, refresh};
})();
