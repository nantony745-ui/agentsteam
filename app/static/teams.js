'use strict';

const TeamsUI = (() => {
  const q = selector => document.querySelector(selector);
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    element.className = className;
    if (text != null) element.textContent = text;
    return element;
  };
  let teams = [], employees = [], current = null, revision = 0, saving = false;
  const request = (url, method = 'GET', body) => api(url, {method,
    ...(body ? {headers: {'Content-Type':'application/json'}, body:JSON.stringify(body)} : {})});
  const report = error => {q('#teams-error').textContent = error.message;};
  const action = callback => event => {event?.preventDefault(); Promise.resolve().then(callback).catch(report);};

  function renderList() {
    const list = q('#team-list'); list.replaceChildren();
    const search = q('#teams-search').value.trim().toLocaleLowerCase();
    for (const team of teams) {
      if (search && !team.name.toLocaleLowerCase().includes(search)) continue;
      const button = node('button', 'team-list-item'); button.type = 'button';
      button.setAttribute('aria-current', String(current?.id === team.id));
      button.append(node('strong', '', team.name), node('span', '', `${team.member_ids.length} 位成员`));
      button.addEventListener('click', () => select(team)); list.append(button);
    }
    if (!list.children.length) list.append(node('p', 'settings-placeholder', teams.length ? '没有匹配的团队。' : '还没有团队，点击新增开始。'));
  }

  function select(team) {
    revision++;
    current = structuredClone(team || {name:'', member_ids:[], leader_id:''});
    q('#team-detail').hidden = false; q('#teams-empty').hidden = true;
    q('#team-name').value = current.name; q('#team-feedback').textContent = '';
    q('#team-capability').replaceChildren(node('p', 'settings-placeholder', '选择成员查看职责与能力。'));
    q('#team-delete').hidden = !current.id;
    renderList(); renderMembers();
  }

  async function preview(employee) {
    const token = ++revision;
    q('#team-capability').replaceChildren(node('p', 'settings-placeholder', '正在读取能力…'));
    const result = await request(`/api/employees/${employee.employee_id}/capabilities`);
    if (token !== revision) return;
    const panel = q('#team-capability'); panel.replaceChildren(node('h3', '', employee.name));
    panel.append(node('p', '', employee.role || '未设置角色'));
    for (const key of ['instructions','responsibilities','principles','growth']) {
      if (employee[key]) panel.append(node('p', 'team-instructions', employee[key]));
    }
    panel.append(node('h4', '', 'Skills'));
    for (const skill of result.skills) panel.append(node('p', '', `${skill.name} · ${skill.description || skill.error || ''}`));
    panel.append(node('h4', '', 'MCP'));
    for (const server of result.mcp) panel.append(node('p', '', `${server.name} · ${server.enabled === false ? '已禁用' : server.enabled ? '配置启用' : '发现配置'}`));
    panel.append(node('p', 'settings-note', result.note || '请先配置员工 Harness。'));
  }

  function renderMembers() {
    const candidates = q('#team-candidates'), selected = q('#team-selected');
    candidates.replaceChildren(); selected.replaceChildren();
    const search = q('#team-search').value.trim().toLocaleLowerCase();
    for (const employee of employees) {
      if (search && !`${employee.name} ${employee.role}`.toLocaleLowerCase().includes(search)) continue;
      const row = node('div', 'team-member-row');
      const info = node('button', 'team-member-info'); info.type = 'button';
      if (employee.harness) info.append(EmployeeSelect.icon(employee.harness));
      const text = node('span', ''); text.append(node('strong', '', employee.name), node('small', '', employee.harness ? employee.role || '未设置角色' : '请先配置 Harness'));
      info.append(text); info.addEventListener('click', action(() => preview(employee)));
      const chosen = current.member_ids.includes(employee.employee_id);
      const add = node('button', 'team-member-add', chosen ? '✓' : '＋'); add.type = 'button';
      add.disabled = chosen || !employee.harness;
      add.title = !employee.harness ? '请先在队员管理中配置 Harness' : `添加 ${employee.name}`;
      add.setAttribute('aria-label', add.title);
      add.addEventListener('click', () => {
        current.member_ids.push(employee.employee_id);
        current.leader_id ||= employee.employee_id; renderMembers();
      });
      row.append(info, add); candidates.append(row);
    }
    current.member_ids.forEach((id, index) => {
      const employee = employees.find(item => item.employee_id === id);
      const row = node('div', 'team-member-row');
      const name = node('button', 'team-member-info', employee?.name || '队员已被删除'); name.type = 'button';
      name.disabled = !employee;
      name.addEventListener('click', action(() => employee && preview(employee)));
      const leader = node('button', 'team-leader', id === current.leader_id ? '♛ 队长' : '设为队长'); leader.type = 'button';
      leader.disabled = !employee;
      leader.setAttribute('aria-pressed', String(id === current.leader_id));
      leader.addEventListener('click', () => {current.leader_id = id; renderMembers();});
      const up = node('button', 'team-order', '↑'); up.type = 'button'; up.disabled = index === 0; up.setAttribute('aria-label', '上移成员');
      up.addEventListener('click', () => {
        [current.member_ids[index - 1], current.member_ids[index]] = [id, current.member_ids[index - 1]]; renderMembers();
      });
      const remove = node('button', 'team-remove', '×'); remove.type = 'button'; remove.setAttribute('aria-label', `移除 ${employee?.name || '成员'}`);
      remove.addEventListener('click', () => {
        current.member_ids = current.member_ids.filter(item => item !== id);
        if (current.leader_id === id) current.leader_id = current.member_ids[0] || ''; renderMembers();
      });
      row.append(name, leader, up, remove); selected.append(row);
    });
    q('#team-selected-count').textContent = `${current.member_ids.length} 位成员`;
    if (!current.member_ids.length) selected.append(node('p', 'settings-placeholder', '从左侧添加员工，并指定一位队长。'));
  }

  async function show() {
    const token = ++revision;
    q('#teams-error').textContent = '';
    const values = await Promise.all([request('/api/teams'), request('/api/employees')]);
    if (token !== revision) return;
    [teams, employees] = values; renderList();
    select(teams.find(item => item.id === current?.id) || teams[0]);
  }

  async function save() {
    if (saving) return;
    if (!q('#team-name').value.trim() || !current.member_ids.length) throw new Error('请填写团队名称并选择成员');
    saving = true; q('#team-form').inert = true;
    try {
      const body = {name:q('#team-name').value.trim(), member_ids:current.member_ids, leader_id:current.leader_id,
        ...(current.id ? {version:current.version} : {})};
      const saved = await request(current.id ? `/api/teams/${current.id}` : '/api/teams', current.id ? 'PUT' : 'POST', body);
      teams = await request('/api/teams'); select(saved); q('#team-feedback').textContent = '已保存，用于之后新建的项目';
    } finally {saving = false; q('#team-form').inert = false;}
  }

  async function projectOptions() {
    const select = q('#project-team');
    [teams, employees] = await Promise.all([request('/api/teams'),request('/api/employees')]);
    select.replaceChildren(new Option('不使用团队', ''));
    for (const team of teams) select.append(new Option(`${team.name} · ${team.member_ids.length} 人`, team.id));
    projectSummary();
  }

  function projectSummary() {
    const team = teams.find(item => item.id === q('#project-team').value);
    q('#project-team-context').hidden = !team;
    q('#project-team-summary').textContent = team ? `${team.member_ids.map(id => `${employees.find(item => item.employee_id === id)?.name || '队员已被删除'}${id === team.leader_id ? '（队长）' : ''}`).join('、')}。创建后保留当前团队组合。` : '';
  }

  function init() {
    q('#team-add').addEventListener('click', () => select());
    q('#teams-search').addEventListener('input', renderList);
    q('#team-search').addEventListener('input', () => current && renderMembers());
    q('#team-form').addEventListener('submit', action(save));
    q('#team-delete').addEventListener('click', () => q('#team-delete-dialog').showModal());
    q('#team-delete-cancel').addEventListener('click', () => q('#team-delete-dialog').close());
    q('#team-delete-confirm').addEventListener('click', action(async () => {
      await request(`/api/teams/${current.id}`, 'DELETE'); q('#team-delete-dialog').close(); current = null; await show();
    }));
    q('#project-team').addEventListener('change', projectSummary);
  }
  return {init, show, projectOptions};
})();
