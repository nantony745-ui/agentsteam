# 技能编写

## 类别与命名

技能使下一次同类任务能直接采用有效流程。类别应描述持续存在的工作，例如 `api-troubleshooting`；一次报错、PR 编号、临时代号或日期留在任务记录。

使用 `<skill-name>/SKILL.md`，以 YAML frontmatter 开头：

```yaml
---
name: api-troubleshooting
description: 排查接口请求失败时，定位协议、认证和响应处理问题。
---
```

`name` 与目录一致，只用小写字母、数字和单连字符，最长 64 字符。`description` 用简短的一句话说明能力与触发场景；存在 YAML 特殊字符时正确引用。保持自动发现，使用当前 Harness 支持的字段与工具名称。

## 步骤优先

按照实际执行顺序写流程，给出具体调用、判断条件与完成标准。适用时依次包含用途、触发场景、前提、运行方式、步骤、陷阱和验证；没有实际内容的栏目直接省略。

共同需要的条件与判断放在 `SKILL.md`，经验和陷阱紧贴受影响的步骤。一个步骤应能回答“执行什么、看到什么结果算完成、下一步怎样选”。命令、端点、函数签名和配置键采用来源或验证中的准确写法，不补造参数。

陷阱采用**动作 + 一句机制原因**，使规则脱离原始事件仍有用。例如：

> 扩展 helper 的参数前，先搜索测试目录中该符号的替身实现；手写 mock 可能复制了旧签名，只运行部分测试会漏掉这些调用。

保存这里的检索动作、原因与验证范围；事件的编号、时间和失败转录保留在任务记录。同一经验再次出现时，澄清或加强原规则；纠错直接修改误导的句子。

## 专题支持文件

| 位置 | 用途与写法 |
| --- | --- |
| `SKILL.md` | 该类任务共同需要的流程、判断和验证入口，通常简单技能约 100 行、复杂技能约 200 行，按实际内容取舍 |
| `references/<topic>.md` | 分支中的决策表、复现方法、工具差异或技术笔记；优先扩展已有专题 |
| `scripts/<name>.<ext>` | 可重复运行的操作、验证或探测；写成可调用文件，并完成适用执行验证 |
| `templates/<name>.<ext>` 或 `assets/` | 确实需要复制修改的起始材料或输出资源 |

每个新增支持文件在主文件中有相对路径和明确的“何时读取／运行”。文件按主题组织，避免用每次会话新增一个引用文件的方式堆积经历。主文件本身提供可执行流程；支持文件补充深度。

只缓存环境无法直接告诉下一次执行者的经验。代码和配置负责当前项目事实，工具 schema 负责参数定义，技能负责操作顺序、选择依据及非显然陷阱。大型来源的处理见[显式学习](learn.md)。

## 完成标准

- 下次遇到触发场景，名称和描述能引导读取正确技能。
- 流程包含适用条件、动作、分支和能判断的完成标准。
- 同一规则只有一个权威位置；更新原句而非追加互相冲突的补丁说明。
- 引用与磁盘文件对应，命令及脚本的验证范围明确。
- 写入与结果报告使用用户的语言。

## 来源与适配

| 来源 | 采用与调整 |
| --- | --- |
| Hermes `agent/background_review.py::_SKILL_REVIEW_PROMPT` | 采用技术经验与技能缺漏信号，以及“已用技能 → 已有类别 → 支持文件 → 新类别”顺序；执行位置改为当前 loop，更新以有效信号为条件 |
| Hermes 同文件 `_LESSON_LAYER_BLOCK` | 采用步骤优先、动作与机制原因、同一经验一条规则、原地纠错、按主题组织引用 |
| Hermes 同文件 `_DO_NOT_CAPTURE_BLOCK` | 采用对临时环境状态、瞬时错误和未解决失败的证据判断，保存已验证修复与可复用方法 |
| Hermes `agent/learn_prompt.py::build_learn_prompt`、`_AUTHORING_STANDARDS`、`_KNOWLEDGE_SKILL_STANDARDS`、`_SOURCE_HYGIENE` | 采用来源盘点、要求保留、逐专题处理、索引核对、准确调用和来源隔离；字段与工具按本项目环境适配 |
| RovAI `skills/memory-stewardship/SKILL.md` 及 `references/{authority-and-safety,read-write-workflow,content-and-keys,scopes}.md` | 采用当前协作证据 → 独立候选 → 读取与比较 → 停止／修订／新增 → 实际结果的捕获方式；存储目标改为当前员工个人技能维护源 |

固定来源：Hermes `f14699a1379838862d3020b97785796aa555d36d`（https://github.com/NousResearch/hermes-agent）；RovAI `362be12f7ae8e53913a29a3745069c9696f96c25`（https://github.com/murray17/rovai-ai）。这里的要求由提示词表达，实际写入与权限检查由当前 Harness 的工具执行。

<!-- Upstream license notices for adapted prompt text.
MIT License

Copyright (c) 2025 Nous Research
Copyright (c) 2026 rovai-ai contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
-->
