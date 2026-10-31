'use strict';

const EmployeesUI = (() => {
  const q = selector => document.querySelector(selector);
  let records = [], current = null, draftConfig = {}, busy = false, revision = 0;
  let previewRevision = 0, skillsRevision = 0, navigation = 0, listRevision = 0;
  let deleteTarget = null;
  let harnessPicker, configPickers = [], guidanceSource = {};
  const fields = ['name', 'role', 'instructions', 'harness'];
  const blank = () => ({name:'', role:'', instructions:'', harness:null, config_values:{}});
  const guidance = record => [record.instructions, ...[['专业职责', 'responsibilities'], ['工作准则', 'principles'], ['成长课题', 'growth']]
    .filter(([, key]) => record[key]?.trim()).map(([name, key]) => `${name}：\n${record[key]}`)].filter(Boolean).join('\n\n');
  function clearConfig(message) {
    for (const picker of configPickers) picker.destroy();
    configPickers = [];
    note(q('#employee-config-fields'), message);
  }
  const request = async (url, method = 'GET', body) => {
    const response = await fetch(url, {method, ...(body ? {headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {})});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '请求字段不合法');
    return result;
  };
  const report = error => { q('#employees-error').textContent = error.message; };
  const action = callback => async event => {
    event?.preventDefault();
    q('#employees-error').textContent = '';
    try { await callback(); } catch (error) { report(error); }
  };
  const harnessName = value => ({codex:'Codex', opencode:'OpenCode', codebuddy:'CodeBuddy', deepseek:'DeepSeek'}[value] || '待配置');
  function note(container, message) {
    const text = document.createElement('p');
    text.className = 'employee-note'; text.textContent = message;
    container.replaceChildren(text);
  }
  function formValue() {
    const body = Object.fromEntries(fields.map(field => [field, q(`#employee-${field}`).value]));
    body.harness ||= null;
    if (body.instructions === current.instructions) Object.assign(body, guidanceSource);
    else Object.assign(body, {responsibilities:'', principles:'', growth:''});
    body.config_values = {...draftConfig};
    return body;
  }
  function updateSaveState() {
    if (!current) return;
    const body = formValue();
    const dirty = fields.some(field => field === 'instructions' ? q('#employee-instructions').value !== current.instructions : body[field] !== current[field]) ||
      Object.keys({...current.config_values, ...draftConfig}).some(key => current.config_values[key] !== draftConfig[key]);
    const pending = dirty || !current.employee_id;
    q('#employee-save-state').textContent = pending ? '有未保存修改' : '当前信息已保存';
    q('#employee-save-state').dataset.dirty = String(pending);
    q('#employee-save').disabled = busy || !pending;
    q('#employee-discard').disabled = !dirty;
    q('#employee-delete').disabled = busy;
  }
  function renderList() {
    q('#employee-count').textContent = records.length;
    q('#employee-list').replaceChildren();
    if (!records.length) note(q('#employee-list'), '尚无队员，点击“新增”创建。');
    for (const record of records) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'employee-list-item';
      const name = document.createElement('strong'); name.textContent = record.name;
      const meta = document.createElement('span'); meta.className = 'employee-list-meta';
      const role = document.createElement('span'); role.textContent = record.role || '未设置角色';
      const harness = document.createElement('span'); harness.className = 'employee-list-harness';
      if (record.harness) harness.append(EmployeeSelect.icon(record.harness));
      harness.append(harnessName(record.harness));
      meta.append(role, harness); button.append(name, meta);
      button.setAttribute('aria-current', String(current?.employee_id === record.employee_id));
      button.addEventListener('click', action(() => select(record)));
      q('#employee-list').append(button);
    }
  }
  function select(record) {
    revision++;
    previewRevision++;
    current = structuredClone(record);
    if (typeof EmployeeMemoryUI !== 'undefined') EmployeeMemoryUI.select(current.employee_id);
    guidanceSource = Object.fromEntries(['instructions', 'responsibilities', 'principles', 'growth'].map(key => [key, record[key] || '']));
    current.instructions = guidance(record);
    draftConfig = {...current.config_values};
    for (const field of fields) q(`#employee-${field}`).value = current[field] || '';
    harnessPicker.close(); harnessPicker.sync();
    clearConfig(current.harness ? '检测配置后，可设置模型与权限。' : '选择 Harness 后配置模型与权限。');
    q('#employee-feedback').textContent = '';
    q('#employee-detail').hidden = false;
    q('#employee-delete').hidden = !current.employee_id;
    q('#employees-empty').hidden = true;
    q('#employee-heading-name').textContent = current.name || '新增队员';
    q('#employee-heading-role').textContent = current.role || '设置资料与运行配置';
    q('#employee-heading-harness').textContent = harnessName(current.harness);
    q('#employee-probe-status').textContent = '';
    q('#employee-skills-section').hidden = !current.employee_id;
    q('#employee-session-section').hidden = !current.employee_id;
    q('#employee-session').disabled = !current.harness;
    q('#employee-skill-content').replaceChildren();
    q('#employee-skill-title').textContent = '';
    updateSaveState();
    renderList();
    if (current.employee_id) return refreshSkills();
  }
  function renderChoice() {
    let select = q('#employee-choice');
    if (!select) {
      select = document.createElement('select');
      select.id = 'employee-choice'; select.setAttribute('aria-label', '新会话队员');
      q('#agent-choice').prepend(select);
      select.addEventListener('change', () => {
        if (!state.draft || state.draft.creating) return;
        const record = records.find(item => item.employee_id === select.value);
        state.draft.employeeId = record?.employee_id || null;
        state.draft.harness = record ? record.harness : q('#harness').value;
        renderChoice();
        refreshDraftModel();
      });
    }
    select.replaceChildren(new Option('直接选择 Harness', ''));
    for (const record of records) {
      const option = new Option(`${record.name}${record.harness ? '' : '（待配置）'}`, record.employee_id);
      option.disabled = !record.harness;
      select.append(option);
    }
    select.value = state.draft?.employeeId || '';
    select.disabled = Boolean(state.draft?.creating);
    q('#harness').hidden = Boolean(state.draft?.employeeId);
  }
  async function refreshSkills() {
    const owner = current.employee_id, token = revision;
    const scanToken = ++skillsRevision;
    previewRevision++;
    const result = await request(`/api/employees/${owner}/skills`);
    if (token !== revision || scanToken !== skillsRevision) return;
    q('#employee-skills-directory').textContent = result.directory;
    q('#employee-skills-list').replaceChildren();
    q('#employee-skill-content').replaceChildren();
    q('#employee-skill-title').textContent = '';
    if (!result.skills.length) note(q('#employee-skills-list'), '尚无个人技能。将技能包放入上方目录后刷新。');
    else {
      q('#employee-skill-title').textContent = '技能预览';
      note(q('#employee-skill-content'), '选择左侧技能，预览 Markdown 正文。');
    }
    for (const item of result.skills) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'employee-skill-item';
      const name = document.createElement('strong'); name.textContent = item.name;
      const description = document.createElement('span'); description.textContent = item.error || item.description;
      button.append(name, description);
      button.setAttribute('aria-current', 'false');
      button.disabled = Boolean(item.error);
      button.addEventListener('click', action(async () => {
        const previewToken = ++previewRevision;
        const detail = await request(`/api/employees/${owner}/skills/${item.id}`);
        if (token !== revision || previewToken !== previewRevision) return;
        for (const sibling of q('#employee-skills-list').children) sibling.setAttribute('aria-current', String(sibling === button));
        q('#employee-skill-title').textContent = item.name;
        renderMarkdown(q('#employee-skill-content'), detail.content);
      }));
      q('#employee-skills-list').append(button);
    }
  }
  async function load() {
    const token = ++listRevision;
    const result = await request('/api/employees');
    if (token !== listRevision) return;
    records = result;
    renderList(); renderChoice();
  }
  async function navigate() {
    const token = ++navigation, detailRevision = revision;
    const visible = /^#employees(?:\/|$)/.test(location.hash);
    q('#employees-view').hidden = !visible;
    q('.app-shell').classList.toggle('employee-mode', visible);
    q('#open-employees').setAttribute('aria-current', visible ? 'page' : 'false');
    if (!visible) return;
    q('.app-shell').hidden = false; q('#settings-view').hidden = true;
    EmployeeLayout.render();
    try {
      await load();
      const projects = await request('/api/projects');
      if (token !== navigation) return;
      q('#employee-project').replaceChildren(new Option('选择项目', ''));
      for (const project of projects) q('#employee-project').append(new Option(project.name, project.id));
      q('#employee-project').value = state.projectId || '';
      if (detailRevision !== revision) return;
      const selected = records.find(item => item.employee_id === current?.employee_id) || records[0];
      if (selected) await select(selected);
    } catch (error) { report(error); }
  }
  async function probe() {
    if (busy) return;
    const harness = q('#employee-harness').value;
    if (!harness) throw new Error('请先选择 Harness');
    const token = revision;
    busy = true; q('#employee-probe').disabled = true; updateSaveState();
    q('#employee-probe-status').textContent = '正在检测…';
    try {
      const result = await request(`/api/settings/harnesses/${harness}/probe`, 'POST', {
        project_id:q('#employee-project').value || null, config_values:draftConfig, force:false,
      });
      if (token !== revision || harness !== q('#employee-harness').value) return;
      clearConfig(''); q('#employee-config-fields').replaceChildren();
      for (const field of result.fields) {
        const row = document.createElement('div'); row.className = 'employee-config-field';
        const label = document.createElement('label'); label.textContent = field.name;
        const select = document.createElement('select'); select.disabled = !field.supported;
        select.id = `employee-config-${field.id}`;
        label.htmlFor = select.id + '-trigger';
        select.append(new Option(field.supported ? '继承全局配置' : 'Harness 未提供', ''));
        for (const option of field.options) select.append(new Option(option.name, option.value));
        select.value = draftConfig[field.id] || '';
        select.addEventListener('change', () => {
          if (select.value) draftConfig[field.id] = select.value; else delete draftConfig[field.id];
          updateSaveState();
          if (field.id === 'model') action(probe)();
        });
        row.append(label, select); q('#employee-config-fields').append(row);
        configPickers.push(EmployeeSelect.mount(select, {label:field.name, searchable:field.id === 'model'}));
      }
      q('#employee-probe-status').textContent = '配置已检测';
      q('#employee-feedback').textContent = result.invalid.map(item => item.reason).join('；');
    } catch (error) {
      if (token === revision) q('#employee-probe-status').textContent = '检测失败';
      throw error;
    } finally { busy = false; q('#employee-probe').disabled = false; updateSaveState(); }
  }
  async function save() {
    if (busy) throw new Error('正在检测配置，请等待检测结束再保存');
    const body = formValue();
    if (current.employee_id) body.version = current.version;
    const token = revision;
    busy = true; q('#employee-save').disabled = true;
    q('#employee-form').inert = true;
    try {
      const record = await request(current.employee_id ? `/api/employees/${current.employee_id}` : '/api/employees', current.employee_id ? 'PUT' : 'POST', body);
      await load();
      if (token === revision) {
        await select(record);
        q('#employee-feedback').textContent = '已保存，修改后新会话生效';
      }
    } finally { busy = false; q('#employee-form').inert = false; updateSaveState(); }
  }
  async function startSession() {
    if (!current.harness) throw new Error('请先配置并保存 Harness');
    const project = q('#employee-project').value;
    if (!project) throw new Error('请选择项目');
    leave();
    selectProject(project);
    if (typeof TeamWorkspace !== 'undefined' && state.projects.find(item => item.id === project)?.team) {
      await TeamWorkspace.openMember(current.employee_id);
      return;
    }
    state.draft.employeeId = current.employee_id; state.draft.harness = current.harness;
    refreshDraftModel();
    renderChoice(); q('#message-input').focus();
  }
  function confirmDelete() {
    if (busy) return;
    deleteTarget = {employee_id:current.employee_id, name:current.name, version:current.version};
    q('#employee-delete-description').textContent = `确定删除队员“${deleteTarget.name}”？删除后不再出现在可选队员中，后续团队和项目无法引用。个人技能、历史会话和本地文件全部保留。`;
    q('#employee-delete-error').textContent = '';
    q('#employee-delete-dialog').showModal();
  }
  async function deleteEmployee(event) {
    event.preventDefault();
    if (busy) return;
    busy = true;
    q('#employee-delete-confirm').disabled = q('#employee-delete-cancel').disabled = true;
    q('#employee-delete-error').textContent = '';
    updateSaveState();
    try {
      await request(`/api/employees/${deleteTarget.employee_id}?version=${deleteTarget.version}`, 'DELETE');
      q('#employee-delete-dialog').close();
      revision++; previewRevision++; skillsRevision++; listRevision++; navigation++;
      records = records.filter(record => record.employee_id !== deleteTarget.employee_id);
      if (state.draft?.employeeId === deleteTarget.employee_id) {
        state.draft.employeeId = null; state.draft.harness = q('#harness').value;
        refreshDraftModel();
      }
      renderChoice(); renderList();
      if (records.length) await select(records[0]);
      else {
        if (typeof EmployeeMemoryUI !== 'undefined') EmployeeMemoryUI.select(null);
        current = null; harnessPicker.close(); clearConfig('');
        q('#employee-detail').hidden = true; q('#employees-empty').hidden = false;
        q('#employee-add').focus();
      }
      if (typeof TeamWorkspace !== 'undefined') await TeamWorkspace.refresh();
    } catch (error) {
      if (q('#employee-delete-dialog').open) q('#employee-delete-error').textContent = error.message;
      else report(error);
    } finally {
      busy = false;
      q('#employee-delete-confirm').disabled = q('#employee-delete-cancel').disabled = false;
      updateSaveState();
    }
  }
  function leave() {
    location.hash = '';
    navigate();
  }
  function init() {
    EmployeeLayout.init();
    harnessPicker = EmployeeSelect.mount(q('#employee-harness'), {label:'Agent Harness', icons:true});
    q('label[for="employee-harness"]').htmlFor = 'employee-harness-trigger';
    q('#open-employees').addEventListener('click', () => {
      location.hash = '#employees';
      if (q('#sidebar').dataset.drawer === 'true') q('#sidebar [data-panel-toggle="left"]').click();
    });
    q('#sidebar').addEventListener('click', event => {
      if (/^#employees(?:\/|$)/.test(location.hash) && event.target.closest('.project-button, .session-button, #create-session-button')) leave();
    }, true);
    q('#employees-back').addEventListener('click', leave);
    q('#employee-add').addEventListener('click', action(() => select(blank())));
    q('#employee-delete').addEventListener('click', confirmDelete);
    q('#employee-delete-form').addEventListener('submit', deleteEmployee);
    q('#employee-delete-cancel').addEventListener('click', () => q('#employee-delete-dialog').close());
    q('#employee-delete-dialog').addEventListener('cancel', event => {if (busy) event.preventDefault();});
    q('#employee-form').addEventListener('submit', action(save));
    q('#employee-discard').addEventListener('click', action(() => select(records.find(item => item.employee_id === current?.employee_id) || blank())));
    q('#employee-form').addEventListener('input', () => {q('#employee-feedback').textContent = ''; updateSaveState();});
    q('#employee-harness').addEventListener('change', () => {
      revision++; draftConfig = {};
      clearConfig('检测配置后，可设置模型与权限。');
      q('#employee-probe-status').textContent = '';
      updateSaveState();
    });
    q('#employee-probe').addEventListener('click', action(probe));
    q('#employee-skills-refresh').addEventListener('click', action(refreshSkills));
    q('#employee-session').addEventListener('click', action(startSession));
    window.addEventListener('hashchange', navigate);
    load().catch(report); navigate();
  }
  return {init, renderChoice};
})();

EmployeesUI.init();
