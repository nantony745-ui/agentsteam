import sys
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Request
from acp.connection import StreamDirection, StreamEvent
from acp.schema import PermissionOption, ToolCallUpdate

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_employees import EmployeeFactory
from app.domain.employees import EmployeeInput
from app.domain.models import ProjectCreate, TaskCreate
from app.main import create_app


directory = Path(sys.argv[1])
factory = EmployeeFactory()
app = create_app(directory / "teams.db", factory)
static = app.router.routes.pop()
original = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original(application):
        project = await app.state.storage.create_project(ProjectCreate(path=str(directory / "repo")))
        await app.state.storage.save_harness_settings("codex", {"model": "m1"})
        employees = [app.state.employees.create(EmployeeInput(name=name, role=role, harness=harness,
                     config_values={"model":"fast"}, instructions="先阅读代码，提供证据与验证结果。")) for name, role, harness in (
            ("林舟", "团队负责人", "codex"), ("陈墨", "研发工程师", "opencode"),
            ("许知", "质量核验", "deepseek"), ("苏禾", "交互设计", "codebuddy"))]
        app.state.employees.create(EmployeeInput(name="待配置员工"))
        app.state.fixture = {"project":project,"employees":employees}
        yield


app.router.lifespan_context = lifespan


@app.get("/fixture")
async def fixture():
    return app.state.fixture


@app.post("/fixture/delegate")
async def delegate(request: Request):
    body = await request.json()
    return await app.state.service.delegate_task(body.pop("leader_id"), TaskCreate(**body))


@app.post("/fixture/emit")
async def emit(request: Request):
    body = await request.json()
    clients = [client for client in factory.clients
               if client.host.runtime.session["id"] == body["session_id"] and client.pending is not None]
    if not clients:
        return {"ok":False}
    for client in clients:
        client.observe(StreamEvent(StreamDirection.INCOMING, {"jsonrpc":"2.0","method":"session/update",
            "params":{"sessionId":"fixture","update":{"sessionUpdate":"tool_call","toolCallId":"shared-tool-id",
            "title":body["text"],"kind":"read","status":"completed","content":[]}}}))
        client.emit(body["text"])
    return {"ok":True}


@app.post("/fixture/finish")
async def finish():
    for client in factory.clients:
        if client.pending is not None and not client.pending.done():
            client.finish("已完成核验，结果保留在各自会话。")
    return {"ok":True}


@app.post("/fixture/permission")
async def permission(request: Request):
    body = await request.json()
    runtime = app.state.service.sessions[body["session_id"]]
    runtime.automatic_approval = False
    callback = asyncio.create_task(runtime.client.host.request_permission("fixture",
        ToolCallUpdate(tool_call_id="shared-tool-id", title="受控写入授权"),
        [PermissionOption(option_id="allow", name="允许本次", kind="allow_once")]))
    app.state.permission_callback = callback
    return {"ok":True}


app.router.routes.append(static)
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[2]), log_level="error")
