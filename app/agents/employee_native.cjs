'use strict';

// 只转换经版本和入口校验的模块；安装目录中的文件保持不变。
const fs = require('node:fs');
const { registerHooks } = require('node:module');
const { fileURLToPath } = require('node:url');
const path = require('node:path');
const spec = JSON.parse(fs.readFileSync(process.env.AGENTSTEAM_EMPLOYEE_SPEC, 'utf8'));

function replaceOnce(source, before, after) {
  if (source.split(before).length !== 2) throw new Error('队员适配入口不匹配，拒绝启动');
  return source.replace(before, after);
}

function transform(source, harness) {
  if (harness === 'codex') {
    source = replaceOnce(source,
      'async createSessionConfig(projectPath, additionalDirectories, mcpServers) {',
      `async createSessionConfig(projectPath, additionalDirectories, mcpServers) {
        const employeeEffective = await this.codexClient.configRead({includeLayers: true, cwd: projectPath});
        const employeePrevious = Object.prototype.hasOwnProperty.call(this.config, 'developer_instructions')
          ? this.config.developer_instructions : employeeEffective?.config?.developer_instructions;
        const employeeDeveloper = [employeePrevious, ${JSON.stringify(spec.identity)}].filter(Boolean).join('\\n\\n');`);
    source = replaceOnce(source,
      '...forceGitRootTurnDiffPaths(mergeGatewayConfig(this.config, this.gatewayConfig)),',
      '...forceGitRootTurnDiffPaths(mergeGatewayConfig(this.config, this.gatewayConfig)), developer_instructions: employeeDeveloper,');
    source = replaceOnce(source,
      'const skillExtraRoots = additionalRoots.map((root) => path7.join(root, ".agents", "skills"));',
      `const skillExtraRoots = [...new Set([...additionalRoots.map((root) => path7.join(root, ".agents", "skills")), ${JSON.stringify(spec.skills_path)}])];`);
  } else if (harness === 'deepseek') {
    source = replaceOnce(source, 'await ctx.plugin(SystemPrompt, { personaPrefix: PERSONA });',
      `await ctx.plugin(SystemPrompt, { personaPrefix: PERSONA });
       ctx.systemPrompt.variable('agentsteam_employee_identity', () => ${JSON.stringify(spec.identity)});
       ctx.systemPrompt.section({name: 'agentsteam:employee', order: 10300, text: '{{agentsteam_employee_identity}}'});`);
    source = replaceOnce(source, 'await ctx.plugin(SkillFilesystem, {});',
      `await ctx.plugin(SkillFilesystem, {customSkillDirs: [${JSON.stringify(spec.skills_path)}]});`);
  } else throw new Error('未知队员适配器');
  return source;
}

registerHooks({load(url, context, nextLoad) {
  const result = nextLoad(url, context);
  if (url.startsWith('file:') && path.resolve(fileURLToPath(url)) === path.resolve(spec.target)) {
    return {...result, source: transform(Buffer.isBuffer(result.source) ? result.source.toString('utf8') : result.source, spec.harness)};
  }
  return result;
}});

module.exports = { transform };
