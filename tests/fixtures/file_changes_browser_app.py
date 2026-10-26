"""浏览器验收专用：临时仓库、可控 ACP 通知，无真实 Agent。"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import uvicorn

from app.agents.acp_client import normalize_update
from app.domain.models import MessageCreate, ProjectCreate, RunStatus, SessionCreate
from app.main import create_app


directory = Path(sys.argv[1])
repo = directory / "repo"
app = create_app(directory / "browser.db")
static_mount = app.router.routes.pop()
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        storage = app.state.storage
        project = await storage.create_project(ProjectCreate(path=str(repo), name="Diff 文件卡片验收"))
        session = await storage.create_session(SessionCreate(project_id=project["id"], harness="codex"))
        run, _ = await storage.create_run(session["id"], MessageCreate(message="修改文档并展示变更", request_id=str(uuid4())))
        await storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
        blocks = []
        for name, old, new in [("plan/中文 space.md", "# 原始标题\n", "# 更新标题\n新增正文\n"),
                               ("new.txt", None, "新文件\n"),
                               ("plan/review/very-long-title-restoration-evidence-coverage-report.md", "old\n", "new\n")]:
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new, encoding="utf-8", newline="")
            blocks.append({"type": "diff", "path": str(target), "oldText": old, "newText": new})
        normalized = normalize_update({"method": "session/update", "params": {"update": {
            "sessionUpdate": "tool_call", "toolCallId": "edit-files", "title": "修改文件",
            "kind": "edit", "status": "completed", "content": blocks,
        }}})
        await storage.append_event(run["id"], **normalized)
        await storage.append_event(run["id"], "agent.text", {"channel": "message", "text": "已修改文档，并新增文件。"})
        app.state.fixture = {"project_id": project["id"], "session_id": session["id"], "run_id": run["id"]}
        yield


app.router.lifespan_context = lifespan


@app.get("/fixture")
async def fixture():
    return app.state.fixture


@app.post("/fixture/finish")
async def finish():
    await app.state.storage.transition_run(app.state.fixture["run_id"], {RunStatus.RUNNING}, RunStatus.COMPLETED, {})
    return {"ok": True}


# 静态挂载必须排在验收专用路由之后。
app.router.routes.append(static_mount)
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[2]), log_level="error")
