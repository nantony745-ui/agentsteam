# Codex 原生 Plan 模式

普通 Codex 会话、员工会话与团队窗格的输入栏提供“跟随配置 / 默认模式 / Plan · 先规划”。模式随消息入库，排队及重复提交保持同一选择；后端确认原生 `collaboration_mode` 生效后才发送消息。全局设置与员工配置也会按 ACP 返回能力展示“工作模式”。权限配置 `mode` 独立保留。

Plan 中的 `request_user_input` 通过 ACP `elicitation/create` 进入问题卡，支持选项、文字和补充说明，回答通过 HTTP 回到原生等待中的请求，不作为新聊天消息发送。工具自动批准不会代答问题。问题与解决事件事务入库，刷新可恢复，重复答复返回冲突；取消、回合结束、进程退出和服务关闭使等待过期。服务重启时旧问题过期，不恢复原生连接，沿用现有的新建会话恢复方式。

原生 `plan_update` 按 planId 替换 Markdown 方案，`plan_removed` 移除执行入口。方案正文进入 Run 结果。正常完成后可点“按此计划执行”，明确提交默认模式和所选计划正文；也可继续在 Plan 模式输入修订要求。此按钮不是自动批准，也不更改员工身份或工具权限。

实现主要位于 `app/agents/{acp_client,harness_config,elicitation}.py`、`app/services/orchestrator.py`、`app/db/storage.py`、`app/api/routes.py`，UI 共用 `app/static/app.js` 的表单与方案卡，团队窗格在 `team-conversation.js` 接入。无新增依赖或数据库表。

参照本机 `@agentclientprotocol/codex-acp@2.0.1/dist/index.js` 的 `CodexPlanStream`、`buildUserInputRequest`、`convertUserInputResponse` 与 `collaboration_mode` 配置；参照 Codeg `src-tauri/src/acp/connection.rs` 的 elicitation 能力声明和问答桥接、`src/components/chat/message-input.tsx` 的模式选择、`src/components/chat/conversation-shell.tsx` 的问答卡与计划决策交互。采用现有 Python ACP SDK 的 unstable protocol、SQLite、HTTP/SSE 与原生 JavaScript，未修改或启动 Codeg。

验证入口：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_codex_plan.py tests/test_run_events.py -q
node --test tests/test_*.cjs
node tests/verify_codex_plan_browser.cjs
```

新增后端测试使用真实 stdio JSON-RPC 与本地协议替身，覆盖能力协商、模式配置、问题校验、重复提交、取消、计划结果、员工身份与权限隔离、重启过期。Chromium 验收覆盖单会话、团队窗格、刷新恢复、回答后执行和 390px 布局。不调用真实付费模型；实际 Codex 模型行为未做在线端到端验证。

2026-10-28 验证结果：相关 Python 模块 176 项通过，SSE 模块单独 21 项通过，前端全量 167 项通过；Chromium 无脚本异常，截图确认问题卡和计划卡可见，窄屏无横向溢出。Python 编译、JavaScript 语法和 diff 检查通过。

宽范围回归限制：从仓库根直接执行 pytest 会同时收集 `comeup_online/tests` 副本并产生同名模块冲突；限定 `tests/` 的一次全量运行出现失败且长时间未结束，已停止，未宣称全量 Python 通过。此后上述直接相关模块和 SSE 模块均独立通过，宽范围失败的根因未确认。


2026-10-28 发布副本同步复验：在 `comeup_online` 按 README 设置 `NO_PROXY=127.0.0.1,localhost` 后，全量 Python 首轮为 558 项通过、1 项失败；唯一失败是旧消息序列化断言未包含新增的 `collaboration_mode`。更新该断言并新增 5 项模式取值用例后，两份目录的消息模型与 Plan 协议测试各 55 项通过。修正后未重复整套 Python 回归。发布副本前端 167 项与 Chromium 验收通过，无浏览器异常，编译、语法、diff 检查通过。

浏览器验收默认使用当前仓库 `.venv/Scripts/python.exe`；复用已有环境时可通过 `AGENTSTEAM_TEST_PYTHON` 指定解释器路径。发布副本保留配置模板，实际 `agents.toml` 不纳入 Git。

2026-10-28 提问卡片更新：共享问题卡改为浅绿色标题、整行可选卡片、推荐徽标、逐题导航与已回答摘要。必填问题完成后才允许提交；Codex user_note 按 questionId 放入所属题目。参照 Codeg `src/components/chat/ask-question-card.tsx` 的 `AskQuestionCard` 和 `src/lib/ask-question.ts` 的 `splitRecommended`，使用现有原生 JavaScript/CSS 实现，保留 HTTP 回答协议及原始选项值，无新增依赖。

本轮开发目录验证：前端 168 项、Plan 协议 4 项通过；Chromium 验收覆盖刷新恢复、键盘方向键选择、普通会话及团队窗格回答后执行、桌面和 390px 窄屏，无脚本异常。截图已检查。未调用真实付费模型。
