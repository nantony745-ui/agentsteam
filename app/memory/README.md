# 长期记忆后端

本目录通过 Hindsight Python 引擎，在已有 FastAPI 进程内提供共享池与队员个人记忆。安装、配置与使用见 [长期记忆说明](../../MEMORY.md)。

| 模块 | 职责 |
| --- | --- |
| `api.py`、`models.py` | HTTP/MCP 接口、请求字段和配置校验 |
| `config.py` | 服务端配置、凭据掩码、模型目录与连接测试 |
| `service.py` | 业务调度、持久化投递、自动 Recall 注入 |
| `storage.py`、`schema.sql`、`migrations.py` | SQLite 来源版本、身份、状态与迁移 |
| `ingestion.py`、`prompts.py` | 陈述者、来源、依据、分段与长期价值约束 |
| `engine/hindsight.py` | 上游初始化、WorkerPoller、Retain/Recall/Reflect 适配 |

自动保存：成功回合 → SQLite 暂存 → 达到设置轮数后登记来源与操作 → Hindsight 接收 → PostgreSQL 队列处理 → 状态同步。失败、取消或停用期间的回合不计入；手动保存和 MCP 保存立即登记投递。

自动读取：新回合 → 限时 Recall → 保存实际注入快照 → Agent 执行。检索失败不阻断会话，Reflect 默认不再次保存。普通会话使用共享库，队员以稳定 ID 使用各自记忆库。

SQLite 保存设置、来源版本、投递状态及历史注入；PostgreSQL + pgvector 保存事实、向量、归纳和上游作业。配置与密钥属于本机数据，不随源码发布。

Hindsight 由项目依赖与锁文件固定至提交 `0ee8b17612dd08b66bcfe216dfb7b03eeeb3fff1`。运行时使用上游引擎提示词与本项目 `prompts.py` 策略，不把开发助手的技能目录作为应用依赖。
