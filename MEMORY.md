# 可选长期记忆

长期记忆在现有 FastAPI 进程内使用 Hindsight 引擎。SQLite 保存设置、来源、投递状态和历史上下文；PostgreSQL + pgvector 保存事实、向量和归纳。基础工作台可以在不开启长期记忆时运行。

## 前置条件

```powershell
uv sync --locked --extra memory
```

Hindsight 已通过锁文件固定到公开上游提交。首次安装需要获取该 Git 依赖及其 Python 依赖。

另外准备：

- 安装 pgvector 的 PostgreSQL，事先创建专用数据库 `agentsteam_memory`。应用会向专用数据库执行上游迁移，不接受任意业务数据库名称。
- OpenAI Chat/Responses 兼容生成模型。
- OpenAI 兼容 Embedding 模型，支持 1–2000 维；超过上限时应用尝试向服务请求 1024 维，不在本地裁剪向量。
- Cohere 或硅基流动兼容 Rerank 服务。

当前本地数据库辅助脚本面向 PostgreSQL 13 与 pgvector 0.8.1；不据此保证其他 PostgreSQL/上游组合已验证。

## 配置与启用

在“设置 → 长期记忆”填写数据库连接串及三类模型的地址、密钥与模型名。可以检测模型并分项测试连接，配置完成后启用。共享池与队员个人记忆默认关闭；队员还需在个人页面开启。

连接串示例：

```text
postgresql://memory_user:REPLACE_WITH_PASSWORD@127.0.0.1:5432/agentsteam_memory
```

配置与密钥保存到本机 SQLite，接口不返回明文密钥；留空密码框表示保留已配置值。应将数据库视为凭据文件管理。连接测试和启用后的模型操作会访问所配置服务，可能产生服务费用。

启动带记忆依赖的应用：

```powershell
.\start.cmd memory
```

使用外部 PostgreSQL 时，通过设置页填写连接即可，不需要项目内数据库管理脚本。

## 可选的项目内 PostgreSQL

仓库不包含 PostgreSQL 程序、扩展构建产物或数据库。辅助脚本可以从已有 PostgreSQL 13 程序和匹配的已编译 pgvector 复制组件，再初始化项目专用数据库：

```powershell
# 将示例路径替换为已有程序及匹配构建产物的位置。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/memory-postgres.ps1 -Action Install -PostgresRoot 'C:/database-tools/PostgreSQL/13' -VectorBuild 'C:/database-tools/pgvector-0.8.1'
```

脚本将程序放到 `.runtime/postgresql/`、数据放到 `data/memory-postgres/`，随机凭据保存在 `.runtime/memory-postgres.json`，默认监听 `127.0.0.1:15432`，不注册系统服务。源目录需要提供脚本要求的许可证文件。

已安装后可管理启停：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/memory-postgres.ps1 -Action Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/memory-postgres.ps1 -Action Start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/memory-postgres.ps1 -Action Stop
```

`start.cmd memory` 检测到项目内数据库配置时会先启动该数据库。关闭应用不自动停库；停止或备份数据库前应先停止应用的记忆处理。已有记忆设置优先，不会自动覆盖为项目内数据库。

## 使用和限制

- 普通会话使用共享池；队员使用独立记忆库，不混用彼此身份。
- 成功完成的回合按设定轮数沉淀，默认 3 轮；不足一批的记录持久化保留。队员跨会话累计，普通会话分别累计。
- 失败、取消、停用期间的回合及旧聊天不会自动导入；手动保存和 MCP 保存立即登记投递。
- 每轮前自动 Recall 最多等待 5 秒，最多注入 12000 字符；检索失败继续会话并提示。Reflect 结果默认不再次保存。
- MCP 提供 `memory_retain`、`memory_recall`、`memory_reflect`。来源保留身份、版本与依据，Agent 报告不自动当作已核实事实。
- 已有来源后不能直接切换数据库、Embedding 模型或维度。修订和删除采用版本核对，处理过程保留审计记录。
- 个人开关关闭后阻止后续读写，但无法撤回已经注入的上下文或取消上游已经接收的全部处理。

实现入口为 `app/memory/`；源码结构见 [后端说明](app/memory/README.md)。完整回归需要安装 memory 额外依赖；自动测试不代表真实云模型、数据库组合均已验证。
