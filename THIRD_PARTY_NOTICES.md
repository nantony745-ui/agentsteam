# 第三方组件说明

下列随源码分发的资源保留其原始许可与版权声明；项目的 MIT 许可不替代这些声明。

| 资源 | 来源 | 许可文件 |
| --- | --- | --- |
| Agent 图标 | [LobeHub Icons](https://github.com/lobehub/lobe-icons) | [MIT](app/static/icons/agents/LICENSE.lobe-icons.txt) |
| Pi 图标 | [Pi](https://github.com/badlogic/pi-mono) | [MIT](app/static/icons/agents/LICENSE.pi.txt) |
| Markdown 渲染库 | [markdown-it](https://github.com/markdown-it/markdown-it) | [许可原文](app/static/vendor/markdown-it.LICENSE) |

Python 依赖由 `pyproject.toml` 和 `uv.lock` 声明，依赖包适用各自许可证。可选长期记忆使用 [Hindsight](https://github.com/vectorize-io/hindsight)，锁定至提交 `0ee8b17612dd08b66bcfe216dfb7b03eeeb3fff1`，不在仓库中打包其安装目录。

Agent CLI、PostgreSQL 和 pgvector 需要使用者另行准备；仓库包含配置与管理脚本，不分发这些程序或数据库。使用数据库复制脚本时，原程序的许可证文件会随程序保留。

实现过程中参考了 Codeg 的 ACP 会话、委派、权限闭环与技能管理流程，使用 Python、FastAPI、SQLite 和 HTTP/SSE 适配。仓库不包含参考项目的本机源码目录或安装包。
