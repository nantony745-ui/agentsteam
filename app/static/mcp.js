'use strict';

const mcpEnabledLabel = item => item.enabled === true ? '配置启用' : item.enabled === false ? '配置停用' : '沿用默认';

const HarnessMcpUI = createHarnessPreview({
  resource: 'mcp', detailPrefix: 'mcp', listKey: 'servers', label: 'MCP', itemLabel: 'MCP',
  selectionParam: 'server', remoteDetail: false,
  description: item => `${item.transport} · ${mcpEnabledLabel(item)}`,
  clearDetail() {
    for (const id of ['config', 'name', 'path', 'transport', 'enabled', 'entry-error']) document.querySelector(`#mcp-${id}`).textContent = '';
  },
  renderDetail(item) {
    if (!item) throw new Error('MCP 不存在，请刷新列表');
    const query = name => document.querySelector(`#mcp-${name}`);
    query('title').textContent = item.name;
    query('name').textContent = item.name;
    query('path').textContent = `配置文件：${item.path}`;
    query('transport').textContent = item.transport;
    query('enabled').textContent = mcpEnabledLabel(item);
    query('entry-error').textContent = item.error || '';
    query('config').textContent = JSON.stringify(item.config, null, 2);
  },
});
