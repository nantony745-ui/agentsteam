const fs = require('node:fs');
const path = require('node:path');

function addSkillsElements(env) {
  const view = env.get('#settings-view');
  const ids = ['settings-harness-panel','settings-skills-panel','settings-harness-nav','settings-skills-nav',
    'skills-harness','skills-search',
    'skills-count','skills-error','skills-list','skills-locations','skills-location-list','skills-refresh',
    'skill-title','skill-preview-scroll','skill-placeholder','skill-detail','skill-name','skill-path',
    'skill-metadata-section','skill-metadata','skill-markdown',
    'settings-mcp-panel','settings-mcp-nav','mcp-harness','mcp-search','mcp-count','mcp-error','mcp-list',
    'mcp-locations','mcp-location-list','mcp-refresh','mcp-title','mcp-preview-scroll','mcp-placeholder',
    'mcp-detail','mcp-name','mcp-path','mcp-config','mcp-note','mcp-transport','mcp-enabled','mcp-entry-error'];
  for (const id of ids) {
    const node = env.document.createElement('div');
    node.id = id;
    view.append(node);
  }
  for(const name of ['harness','skills','mcp']) {
    const container = env.document.createElement('div');
    container.className='settings-split'; container.clientWidth=1000;
    env.get(`#settings-${name}-panel`).append(container);
    const handle=env.document.createElement('div'); handle.id=`settings-${name}-resizer`; handle.className='settings-resizer';
    container.append(handle);
  }
  for(const file of ['harness-preview.js','skills.js','mcp.js','settings-layout.js']) env.run(fs.readFileSync(path.join(__dirname,'../app/static',file),'utf8'));
}
module.exports = { addSkillsElements };
