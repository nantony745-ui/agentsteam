# Skill authoring

## Class and naming

A skill lets the next task of the same class use an effective procedure directly. Name an enduring class of work, such as `api-troubleshooting`; keep individual errors, PR numbers, temporary codenames, and dates in task records.

Use `<skill-name>/SKILL.md` starting with YAML frontmatter:

```yaml
---
name: api-troubleshooting
description: Diagnose protocol, authentication, and response handling when API requests fail.
---
```

Match `name` to the directory, using lowercase letters, digits, and single hyphens, up to 64 characters. Write a short, one-sentence `description` stating capability and triggers; quote YAML special characters correctly. Keep automatic discovery enabled and use fields and tool names supported by the current Harness.

## Procedure first

Write steps in execution order, with concrete calls, decision conditions, and completion criteria. Where applicable, include purpose, triggers, prerequisites, invocation, steps, pitfalls, and verification in that order; omit sections without actual content.

Keep shared conditions and decisions in `SKILL.md`, with lessons and pitfalls beside the affected step. Each step should answer what to do, which result establishes completion, and how to choose the next action. Use exact commands, endpoints, signatures, and configuration keys from sources or verification rather than inventing parameters.

Write a pitfall as **an action + one clause explaining the mechanism** so it stands without the original incident. For example:

> Before widening a helper signature, search the test tree for substitutes implementing that symbol; hand-written mocks may copy the old signature, and running only some tests can miss these calls.

Preserve the search action, reason, and verification scope; keep incident identifiers, times, and failure transcripts in task records. When the same lesson recurs, clarify or strengthen the existing rule; fix the misleading sentence directly.

## Topical support files

| Location | Purpose and shape |
| --- | --- |
| `SKILL.md` | Shared procedure, decisions, and verification entry points for the class; roughly 100 lines for a simple skill or 200 for a complex one, adjusted to actual content |
| `references/<topic>.md` | A branch's decision table, reproduction recipe, tool differences, or technical notes; extend an existing topic first |
| `scripts/<name>.<ext>` | Repeatable operations, verification, or probes; provide callable files and appropriate execution checks |
| `templates/<name>.<ext>` or `assets/` | Starting material actually needed for copying and modification, or output resources |

Give every new supporting file a relative pointer and a clear loading or execution condition in the main file. Organize by topic instead of adding a new reference for each session. The main file provides an executable procedure; supporting files provide depth.

Cache only experience the environment cannot directly teach the next executor. Code and configuration own current project facts, tool schemas own parameter definitions, and skills own operation order, selection criteria, and non-obvious pitfalls. For large sources, see [Explicit learning](learn.md).

## Completion criteria

- Names and descriptions route the next matching task to the right skill.
- The procedure includes conditions, actions, branches, and observable completion criteria.
- Each rule has one authoritative home; revise its original wording instead of appending contradictory corrections.
- References match files on disk, with command and script verification scopes stated.
- Persisted content and outcome reports use the user's language.

## Sources and adaptation

| Source | Adopted and adapted |
| --- | --- |
| Hermes `agent/background_review.py::_SKILL_REVIEW_PROMPT` | Technical-learning and skill-gap signals, and the used skill → existing class → support file → new class order; execution moves into the current loop and updates depend on valid signals |
| `_LESSON_LAYER_BLOCK` in the same Hermes file | Procedure first, action plus mechanism, one rule per lesson, corrections in place, and topical references |
| `_DO_NOT_CAPTURE_BLOCK` in the same Hermes file | Evidence-based handling of temporary setup state, transient errors, and unresolved failures; preserve verified fixes and reusable methods |
| Hermes `agent/learn_prompt.py::build_learn_prompt`, `_AUTHORING_STANDARDS`, `_KNOWLEDGE_SKILL_STANDARDS`, `_SOURCE_HYGIENE` | Source inventory, full requirements, incremental topic processing, index reconciliation, exact invocations, and source hygiene; adapt fields and tools to this environment |
| RovAI `skills/memory-stewardship/SKILL.md` and `references/{authority-and-safety,read-write-workflow,content-and-keys,scopes}.md` | Capture from current collaboration evidence → independent candidate → read and compare → stop/revise/add → actual outcome; target the current employee's personal skill maintenance source |

Pinned sources: Hermes `f14699a1379838862d3020b97785796aa555d36d` (https://github.com/NousResearch/hermes-agent); RovAI `362be12f7ae8e53913a29a3745069c9696f96c25` (https://github.com/murray17/rovai-ai). These requirements are expressed in prompts; actual writes and permission checks are performed by the current Harness tools.

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
