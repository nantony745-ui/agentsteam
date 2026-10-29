# 前后端资源开销审查

日期：2026-10-29。结论：优先处理团队窗的资源增长、记忆服务的长时间持锁、创建队员会话时的同步文件操作；其次减少全量刷新与记忆重复存储。无需为这些问题改成多进程或增加依赖。

本轮只做审查和合成样本验证，没有修改业务代码、业务数据库或参考仓库。P1 表示建议优先处理、满足触发条件时影响明显；P2 表示常规优化；P3 表示先测量再决定。优先级不是已测得的资源占比。

## 已核实的主要问题

| 优先级 | 范围与证据 | 触发条件及资源影响 | 最小处理方向 |
| --- | --- | --- | --- |
| P1 | 团队历史：[team-conversation.js:120](app/static/team-conversation.js#L120)、[finish:229](app/static/team-conversation.js#L229) | 历史不断加载或会话持续运行，Run、DOM、seen 集合、工具及交互记录持续保留；完成后 segments 仍保存正文源字符串。合成样本已复现增长。 | 复用主会话已有的历史窗口、占位恢复与终态源文本释放；淘汰时同步清理所属工具、交互、事件 ID。 |
| P1，需高流量触发 | 团队事件：[team-workspace.js:103](app/static/team-workspace.js#L103)、[receive:135](app/static/team-conversation.js#L135) | 每条事件建立 delivery，pane 通过 Promise 链串行消费；无待处理数量上限。补拉或渲染慢于到达速度时，积压会增长。代码确认，未做队列峰值压测。 | 参照主会话 livePending 上限机制：超过预算断流，从最后确认游标补拉；保留各窗独立消费和顺序确认。 |
| P1，记忆启用时 | 记忆锁：[service.py:122](app/memory/service.py#L122)、[tick:180](app/memory/service.py#L180) | Recall/Reflect、后台投递、修订和配置共用一把锁，锁跨越上游 I/O；Reflect 查询本身超时为 120 秒。引擎替身已证实查询期间修订无法进入；自动 Recall 的 5 秒预算也包含排队时间。 | 缩小生命周期锁范围，为普通引擎操作设有界并发；必须保证重配置/关闭不销毁正在使用的引擎，并保留同一来源的版本顺序。 |
| P1，技能较大时 | 队员快照：[employees.py:181](app/services/employees.py#L181)、[_snapshot:209](app/services/employees.py#L209)；调用：[routes.py:97](app/api/routes.py#L97)、[teams.py:188](app/services/teams.py#L188)、[orchestrator.py:401](app/services/orchestrator.py#L401) | async 路径同步发现目录、递归读文件、计算哈希和写快照；收集时把技能文件全部读入 bytes 字典。已存在相同摘要快照仍需先读取、计算摘要。大技能包会占内存并阻塞同进程事件循环，具体延迟未测。 | 先把同步准备步骤移至线程，保留 admission 协调及取消后的清理语义；若内存测量确认有压力，再评估流式哈希/复制。线程本身不降低文件字节总量。 |
| P2 | 全量侧栏：[app.js:243](app/static/app.js#L243)、[轮询:266](app/static/app.js#L266)、[routes.py:113](app/api/routes.py#L113)、[storage.py:804](app/db/storage.py#L804)、[binding:256](app/services/employees.py#L256) | 每个可见页面每 5 秒查询全部会话摘要、传输全量结果并重绘；队员摘要还逐条同步读 binding 文件。每组只展示 5 条没有减少后端读取与网络数据量。 | 优先使用变化通知加去重刷新；跨页面同步保留恢复查询。规模确实需要时，再加增量接口/分页；不能直接限制列表而破坏树关系与删除识别。 |
| P2 | 记忆重复存储：[flush_capture_batch:102](app/memory/storage.py#L102) | 已提交自动沉淀只更新缓冲行 source_id，正文和 metadata 仍留在缓冲表，操作表再保存一份。3 轮合成样本确认两表各保留 30,144 字符正文，尚未计 metadata 的副本。 | 来源与操作在同事务写入成功后清理已提交缓冲；继续保留未满批次内容和既有审计记录。先验证重复完成、回滚与重启。减少逻辑数据不等于 SQLite 文件立即缩小。 |
| P2 | 团队计时刷新：[team-conversation.js:56](app/static/team-conversation.js#L56)、[update:312](app/static/team-conversation.js#L312) | 每窗每秒遍历全部 Run，每个 Run 又扫描问题集合，并更新计划操作与会话选项；隐藏 pane 仍执行。与历史无界保留叠加，扫描量随 Run/问题数量增长。 | 活动回合才更新计时；一次构造待回答 Run 集合；隐藏时暂停无关刷新，恢复时补刷新。 |
| P2，长回答时 | Markdown：[app.js:119](app/static/app.js#L119)、[team-conversation.js:221](app/static/team-conversation.js#L221) | 已有 100ms/RAF 合批，但每次仍重解析累计全文并替换内容。长回答持续多次刷新会反复处理历史前缀；没有测量浏览器长任务或实际总耗时。 | 先测长正文流式渲染；可随正文长度降低中间刷新频率，终态完整渲染。没有依据直接引入新 Markdown/虚拟列表依赖。 |
| P2，长会话且自动沉淀开启时 | 反馈提取：[memory/storage.py:83](app/memory/storage.py#L83)、[完成事务:135](app/db/storage.py#L135)、[索引:44](app/db/schema.sql#L44) | 完成 Run 的写事务中查同会话 feedback.pulled 的时间范围。现有查询计划仅使用 events_session(session_id)，没有按 kind/时间缩小索引范围；可能扫描该会话大量非反馈事件。 | 用大样本验证后再选择部分索引或调整关联；兼顾时间过滤与 id 排序，不能仅凭索引名称判断收益。 |

## 次优先级候选

| 优先级 | 证据 | 建议与边界 |
| --- | --- | --- |
| P3 | [MCP bridge:30](app/mcp/bridge.py#L30) | 每次工具调用创建并关闭 AsyncClient。桥进程内可复用客户端与连接池，保留动态读取超时、trust_env=False 和退出清理；本机环回收益尚未测。 |
| P3 | [validate_worktree:77](app/delivery/git_delivery.py#L77)、[diff_task:108](app/delivery/git_delivery.py#L108) | 一次预览先执行 6 次 Git 验证；Diff 路径合计执行 10 次 Git 命令。可合并元数据读取，不能省掉仓库、分支和基线有效性检查。实际耗时待测。 |
| P3 | [wait_for_tasks:47](app/api/internal.py#L47) | 任务长等待每 250ms 重新鉴权和查询快照，多个等待者叠加读库。可参考现有 Storage.changed 唤醒，保留截止时间、凭据失效和断线处理；与浏览器 SSE 已有唤醒机制区分。 |
| P3 | [Teams.roster:139](app/services/teams.py#L139) | 为团队会话补标题时读取整个项目的会话摘要。可只投影已选会话标题；改动前验证团队范围和返回一致性。 |
| P3 | [团队 tabs:125](app/static/team-workspace.js#L125)、[签名:141](app/static/team-workspace.js#L141) | 每条 SSE 事件都会计算完整 roster 的 JSON 签名，文本增量通常不改变标签。只在相关状态变化时计算，或使用修订号；已有签名机制避免了未变化时的 DOM 重建。 |

## 不应直接清理的资源

- ACP 健康连接跨 Run 复用，运行时和原生进程可以随未删除会话数量累积。当前连接关闭后不能透明恢复同一原生身份，不能直接加空闲淘汰来换内存；需要先明确恢复与关闭交互。未测量各 Harness 的 RSS。
- 员工 runtime 快照按内容摘要复用；软删除员工明确要求保留资料、技能、快照和绑定，不能直接做目录垃圾清理。
- 删除会话时，memory_contexts、memory_run_scopes、memory_capture_turns 没有同步清理；未提交队员缓冲可能后续跨会话凑批。是否清理要先明确会话删除与长期记忆保留语义，不能当作普通缓存删除。定位：storage.py:432、memory/schema.sql:15、memory/storage.py:102。
- 文件系统快照：data/lab.db 为 224,563,200 字节（约 214.2 MiB）。只查看了文件长度，没有读取业务消息、执行 VACUUM 或分析真实表占比，不能据此认定数据库异常膨胀或估算可回收空间。

## 已有优化

- SQLite 已有 WAL、写事务锁、独立查询连接与读锁、事件/运行/工具等索引；不是每条查询都新建连接。
- RunJournal 已有 100ms 合批、相邻文本合并、每 Run 8MiB/2,048 项和全局 64MiB/16,384 项待写预算。
- 事件大字段分段压缩、部分终端输出去重；详情分页且按需读取。
- 主会话已有最近 20 轮首屏、约 60 轮历史窗口、正文源字符串释放和 256 个待消费事件的保护；团队窗尚未对齐这些限制。
- SSE 等待数据库变化唤醒，含超时心跳；没有发现所有 SSE 客户端固定每 250ms 查询数据库的问题。
- Harness 检测有 30 秒缓存和串行探测锁；文件变更缓存最多 8 项，单项超过 2MiB 不缓存；预览最多 1MiB，ACP stderr 缓冲限制为 64KiB。
- 静态资源已配置 GZipMiddleware，minimum_size=1024、compresslevel=3；没有重复建议启用 gzip。

## 验证与覆盖

| 验证 | 结果 | 解释范围 |
| --- | --- | --- |
| 团队窗合成 100/200 个已完成 Run，每轮 1,024 字符正文 | DOM Run 为 100/200，保留正文源字符为 102,400/204,800 | 证明对象和 DOM 持续保留；不是浏览器 heap/RSS 测量。使用现有 frontend_dom 测试环境。 |
| 记忆引擎替身阻塞 Reflect，同时提交来源修订 | 查询占锁时修订无法进入；释放后两者完成 | 证明服务层串行关系，没有调用云端模型。 |
| 内存 SQLite，调用生产 flush_capture_batch 提交 3 轮 | 已标记提交的缓冲仍有 3 行；缓冲和操作表正文各为 30,144 字符 | 证明正文副本保留，没有估算真实业务磁盘回收比例。 |
| 原始 schema 上 EXPLAIN QUERY PLAN | SEARCH events USING INDEX events_session (session_id=?) | 仅确认反馈查询的索引过滤范围，未量化大库耗时。 |
| Python 定向回归 | 44 passed，14.89 秒 | test_memory_ingestion.py、test_memory_policy.py、test_local_history.py、test_session_summary.py。 |
| 前端定向回归 | 48 passed | test_team_conversation.cjs、test_history_ui.cjs、test_sidebar.cjs、test_team_workspace.cjs。 |

原始合成结果保存在 [profile_output/performance-audit-2026-10-29.json](profile_output/performance-audit-2026-10-29.json)。探针通过命令行 stdin 临时执行，没有向应用插入 instrumentation。

已对前后端源码做文件枚举和性能关键词检索，重点追踪各条发现的完整调用关系。覆盖 app/static 自有 JS/CSS/HTML、app/api、services、agents、delivery、mcp、domain、db、memory 与启动/基准脚本；不是每个文件逐行审计。没有做真实业务并发压测、浏览器 heap/火焰图、PostgreSQL 查询分析或各 Harness 内存采样，因此不承诺具体提速比例。

数据路径为：浏览器 HTTP → FastAPI 服务 → SQLite/文件/ACP 子进程；ACP 增量 → RunJournal 合批 → SQLite 事件 → SSE → 浏览器串行消费/渲染。记忆路径另经 SQLite outbox → 同进程 Hindsight → PostgreSQL/模型服务；本轮测量用替身截断了外部调用。

Codeg 只读参照：src-tauri/src/workspace_state/mod.rs 的 recent_capacity、can_replay_from 使用有界回放队列和游标恢复；src-tauri/src/terminal/manager.rs 的 SCROLLBACK_MAX_CHARS、Scrollback::append 使用缓冲上限。团队窗优化首先复用本仓库主会话机制，再保留上述“内存有界、历史可恢复”的规则，不迁移 Rust/Tauri 或改变 HTTP/SSE 架构。未启动 Codeg。

本轮新增文件只有本报告及 profile_output 下的合成结果 JSON；未修改业务代码，未新增依赖。性能范围内未核实到需要单列的安全告警；本轮不属于完整安全审计。

## 优化实施记录

以下为后续授权实施的记录，前文保留审查时的状态。

- 记忆重复存储：批次登记成功后同事务删除缓冲；启动迁移清理旧版已提交缓冲，幂等检查继续使用稳定来源 ID。复用本项目 MemoryStore/outbox 的来源与事务规则，Codeg 无对应长期记忆后端。记忆与个人记忆回归 29 项通过，覆盖旧缓冲、重启防重、未满批次与回滚。
- 反馈提取：增加仅覆盖 feedback.pulled 的 session/time 部分索引，保留按事件 ID 返回的语义。复用本库现有部分索引做法；适配 Python SQLite 查询，没有新增服务。2 项定向测试通过，覆盖 2,000 条无关事件、时间过滤与非时间顺序的反馈；另以 100,000 条无关事件验证索引过滤范围。
