'use strict';

const HarnessSkillsUI = createHarnessPreview({
  resource: 'skills', detailPrefix: 'skill', listKey: 'skills', label: 'Skills', itemLabel: 'Skill',
  selectionParam: 'skill', remoteDetail: true,
  description: item => item.description,
  clearDetail() {
    document.querySelector('#skill-markdown').replaceChildren();
    for (const id of ['metadata', 'name', 'path']) document.querySelector(`#skill-${id}`).textContent = '';
  },
  renderDetail(data) {
    const query = name => document.querySelector(`#skill-${name}`);
    query('title').textContent = data.skill.name;
    query('name').textContent = data.skill.name;
    query('path').textContent = `技能文件：${data.skill.path}`;
    query('metadata').textContent = data.metadata;
    query('metadata-section').hidden = !data.metadata;
    renderMarkdown(query('markdown'), data.body);
  },
});
