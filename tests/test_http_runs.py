"""2.4：真实路由和 lifespan；仅替换 ACP 边界，所有状态写入临时文件库。"""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import uvicorn
from acp.connection import StreamDirection, StreamEvent
from acp.schema import PermissionOption, PromptResponse, ToolCallUpdate

from app.api.routes import StreamSettings
from app.main import create_app
from app.db.storage import Storage
from test_storage import repository


class ControlledClient:
    def __init__(self, owner, harness, host, record, observe, expire):
        self.owner, self.host, self.observe = owner, host, observe
        self.harness = harness
        self.proc = SimpleNamespace(pid=123, returncode=None)
        self.reusable = False
        self.forced = False
        self.pending = None
        self.closed = False

    async def connect(self, cwd, servers):
        self.cwd = cwd
        self.owner.connect_started.set()
        await self.owner.connect_release.wait()
        if self.owner.connect_error:
            raise ConnectionError("连接失败")
        self.reusable = True
        return f"native-{len(self.owner.clients)}"

    async def prompt(self, text):
        self.pending = asyncio.get_running_loop().create_future()
        self.owner.starts.put_nowait((self, text))
        try:
            return await self.pending
        finally:
            self.owner.ends.append(text)

    def emit(self, text, message_id=None):
        update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
        if message_id is not None:
            update["messageId"] = message_id
        self.observe(StreamEvent(StreamDirection.INCOMING, {
            "jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": "native", "update": update},
        }))

    def finish(self, text="结果", reason="end_turn"):
        self.emit(text)
        self.pending.set_result(PromptResponse(stop_reason=reason))

    async def cancel(self):
        self.owner.cancel_entered.set()
        await self.owner.cancel_release.wait()
        self.owner.cancels += 1
        if self.owner.force_cancel:
            self.forced = True
            self.reusable = False
            self.proc.returncode = 1
            self.pending.cancel()
            raise TimeoutError("受控取消超时，进程已回收")
        if not self.pending.done():
            self.pending.set_result(PromptResponse(stop_reason="cancelled"))

    async def close(self):
        self.closed = True
        self.reusable = False
        self.proc.returncode = 0
        if self.pending is not None and not self.pending.done():
            self.pending.cancel()
        self.owner.close_order.append("agent")


class ControlledFactory:
    def __init__(self):
        self.clients = []
        self.starts = asyncio.Queue()
        self.ends = []
        self.cancels = 0
        self.connect_error = False
        self.force_cancel = False
        self.close_order = []
        self.connect_started = asyncio.Event()
        self.connect_release = asyncio.Event()
        self.connect_release.set()
        self.cancel_entered = asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.cancel_release.set()

    def __call__(self, *args):
        instance = ControlledClient(self, *args)
        self.clients.append(instance)
        return instance

    async def started(self):
        return await asyncio.wait_for(self.starts.get(), 3)


@pytest_asyncio.fixture
async def setup_app(tmp_path):
    factory = ControlledFactory()
    app = create_app(tmp_path / "http.db", factory, StreamSettings(0.01, 0.05))
    async with app.router.lifespan_context(app):
        yield app, factory


@pytest_asyncio.fixture
async def api(setup_app):
    app, factory = setup_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        yield client, app, factory


@pytest_asyncio.fixture
async def http_records(api, repository):
    client, _, _ = api
    response = await client.post("/api/projects", json={
        "path": str(repository), "name": "测试项目", "check_argv": ["python", "-V"],
    })
    assert response.status_code == 201, response.text
    project = response.json()
    session = await new_session(client, project)
    return project, session


async def new_session(client, project):
    response = await client.post("/api/sessions", json={"project_id": project["id"], "harness": "codex"})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_create_project_without_check_command(api, repository):
    client, app, factory = api
    response = await client.post("/api/projects", json={"path": str(repository), "name": "可直接创建"})
    assert response.status_code == 201, response.text
    project = response.json()
    assert json.loads(project["check_argv"]) == []
    saved = await app.state.storage.get_project(project["id"])
    assert json.loads(saved["check_argv"]) == []
    assert (await new_session(client, project))["project_id"] == project["id"]
    assert factory.clients == []


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codebuddy", "deepseek"])
async def test_harness_session_stream_permission_reuse_and_cancel(api, http_records, harness):
    client, app, factory = api
    project, _ = http_records
    response = await client.post('/api/sessions', json={"project_id": project["id"], "harness": harness})
    assert response.status_code == 201, response.text
    session = response.json()
    assert (await client.get(f"/api/sessions/{session['id']}")).json()["session"]["harness"] == harness
    request_id = str(uuid4())
    run = await submit(client, session, "首次输入", request_id)
    assert (await submit(client, session, "首次输入", request_id))["run_id"] == run["run_id"]
    agent, _ = await factory.started()
    assert agent.harness == harness
    agent.emit(f"{harness} 流式正文")
    option = PermissionOption(option_id=f"{harness}/allow-once", name="允许本次", kind="allow_once")
    callback = asyncio.create_task(agent.host.request_permission(
        "native", ToolCallUpdate(tool_call_id=f"{harness}-tool", title="写文件"), [option],
    ))
    assert (await asyncio.wait_for(callback, 3)).outcome.option_id == option.option_id
    assert (await client.get(f"/api/runs/{run['run_id']}")).json()["status"] == "running"
    assert await app.state.storage.list_interactions(session["id"]) == []
    agent.finish("完成")
    assert (await terminal(client, run["run_id"]))["status"] == "completed"
    events = (await client.get(f"/api/events?project_id={project['id']}&session_id={session['id']}")).json()
    assert any(row["kind"] == "agent.text" and row["payload"]["text"] == f"{harness} 流式正文" for row in events)
    next_run = await submit(client, session, "下一轮取消")
    reused, _ = await factory.started()
    assert reused is agent and len(factory.clients) == 1
    assert (await client.post(f"/api/runs/{next_run['run_id']}/cancel")).json()["status"] == "canceled"


async def submit(client, session, text, request_id=None):
    response = await client.post(f"/api/sessions/{session['id']}/messages", json={
        "message": text, "request_id": request_id or str(uuid4()),
    })
    assert response.status_code == 202, response.text
    return response.json()


async def terminal(client, run_id):
    async with asyncio.timeout(3):
        while True:
            response = await client.get(f"/api/runs/{run_id}")
            assert response.status_code == 200
            run = response.json()
            if run["status"] in {"completed", "failed", "canceled", "interrupted"}:
                return run
            await asyncio.sleep(0.005)


@asynccontextmanager
async def live_server(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.005)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=3) as client:
            yield client
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


@pytest.mark.asyncio
async def test_fifo_idempotency_parallel_sessions_and_client_disconnect(api, http_records):
    client, app, factory = api
    project, session = http_records
    request_id = str(uuid4())
    first = await submit(client, session, "第一轮", request_id)
    assert first["status"] == "queued"
    agent, text = await factory.started()
    assert text == "第一轮" and not agent.pending.done()
    assert (await client.get(f"/api/runs/{first['run_id']}")).json()["status"] == "running"
    assert await submit(client, session, "第一轮", request_id) == {"run_id": first["run_id"], "status": "running"}
    conflict = await client.post(f"/api/sessions/{session['id']}/messages", json={"message": "不同", "request_id": request_id})
    assert conflict.status_code == 409
    second = await submit(client, session, "第二轮")
    another = await new_session(client, project)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as detached:
        third = await submit(detached, another, "另一会话")
    other, text = await factory.started()
    assert text == "另一会话" and other is not agent and factory.starts.empty()
    other.finish("独立完成")
    assert (await terminal(client, third["run_id"]))["result"] == "独立完成"
    agent.emit("甲")
    agent.finish("乙")
    assert (await terminal(client, first["run_id"]))["result"] == "甲乙"
    reused, text = await factory.started()
    assert reused is agent and text == "第二轮"
    reused.finish()
    await terminal(client, second["run_id"])
    events = await app.state.storage.list_events(project["id"], session["id"], 0, 100)
    first_events = [event for event in events if event["run_id"] == first["run_id"]]
    assert [json.loads(event["payload"])["text"] for event in first_events if event["kind"] == "agent.text"] == ["甲乙"]
    assert json.loads(first_events[-1]["payload"])["status"] == "completed"
    details = (await client.get(f"/api/sessions/{session['id']}")).json()
    assert details["session"]["native_id"] and details["session"]["process_pid"] == 123
    assert len(details["runs"]) == 2 and details["interactions"] == []
    assert len((await client.get("/api/projects")).json()) == 1
    assert len((await client.get(f"/api/projects/{project['id']}/sessions")).json()) == 2


@pytest.mark.asyncio
async def test_queued_and_active_cancel_are_idempotent(api, http_records):
    client, _, factory = api
    _, session = http_records
    first = await submit(client, session, "运行")
    agent, _ = await factory.started()
    queued = await submit(client, session, "排队")
    canceled = await client.post(f"/api/runs/{queued['run_id']}/cancel")
    assert canceled.json()["status"] == "canceled" and canceled.json()["started_at"] is None
    active = await client.post(f"/api/runs/{first['run_id']}/cancel")
    assert active.json()["status"] == "canceled" and active.json()["stop_reason"] == "cancelled"
    repeated = await client.post(f"/api/runs/{first['run_id']}/cancel")
    assert repeated.json() == active.json() and factory.cancels == 1
    next_run = await submit(client, session, "继续")
    reused, text = await factory.started()
    assert reused is agent and text == "继续"
    reused.finish()
    assert (await terminal(client, next_run["run_id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_completion_racing_cancel_cannot_cancel_next_run(api, http_records):
    client, _, factory = api
    _, session = http_records
    first = await submit(client, session, "竞争")
    agent, _ = await factory.started()
    second = await submit(client, session, "下一轮")
    factory.cancel_release.clear()
    cancelling = asyncio.create_task(client.post(f"/api/runs/{first['run_id']}/cancel"))
    await asyncio.wait_for(factory.cancel_entered.wait(), 3)
    agent.finish("正常抢先完成")
    await asyncio.sleep(0.02)
    assert factory.starts.empty()
    factory.cancel_release.set()
    assert (await cancelling).json()["status"] == "completed"
    next_agent, text = await factory.started()
    assert text == "下一轮" and not next_agent.pending.done()
    next_agent.finish()
    await terminal(client, second["run_id"])


@pytest.mark.asyncio
async def test_cancel_timeout_blocks_queue_and_session(api, http_records):
    client, app, factory = api
    _, session = http_records
    first = await submit(client, session, "取消超时")
    await factory.started()
    queued = await submit(client, session, "不能启动")
    factory.force_cancel = True
    response = await client.post(f"/api/runs/{first['run_id']}/cancel")
    assert response.json()["status"] == "canceled"
    assert "TimeoutError" in response.json()["error"]
    assert (await terminal(client, queued["run_id"]))["status"] == "canceled"
    response = await client.post(f"/api/sessions/{session['id']}/messages", json={"message": "重用", "request_id": str(uuid4())})
    assert response.status_code == 409
    assert factory.starts.empty()


@pytest.mark.asyncio
async def test_connection_failure_and_http_validation(api, http_records):
    client, _, factory = api
    _, session = http_records
    factory.connect_error = True
    run = await submit(client, session, "连接失败")
    assert (await terminal(client, run["run_id"]))["status"] == "interrupted"
    assert (await client.get(f"/api/runs/{uuid4()}")).status_code == 404
    assert (await client.post(f"/api/runs/{uuid4()}/cancel")).status_code == 404
    assert (await client.get("/api/runs/not-uuid")).status_code == 422
    assert (await client.post(f"/api/sessions/{uuid4()}/messages", json={"message": "x", "request_id": str(uuid4())})).status_code == 404
    assert (await client.post(f"/api/sessions/{session['id']}/messages", json={"message": " ", "request_id": str(uuid4())})).status_code == 422


@pytest.mark.asyncio
async def test_permission_without_allow_is_persisted_and_immediately_cancelled(api, http_records):
    client, app, factory = api
    project, session = http_records
    run = await submit(client, session, "权限")
    agent, _ = await factory.started()
    raw = {"jsonrpc": "2.0", "id": "permission-1", "method": "session/request_permission",
           "params": {"sessionId": "native", "toolCall": {"toolCallId": "tool"}, "options": []}}
    agent.observe(StreamEvent(StreamDirection.INCOMING, raw))
    callback = asyncio.create_task(agent.host.request_permission("native", ToolCallUpdate(tool_call_id="tool"), []))
    response = await asyncio.wait_for(callback, 3)
    assert response.outcome.outcome == "cancelled"
    assert await app.state.storage.list_interactions(session["id"]) == []
    assert (await client.get(f"/api/runs/{run['run_id']}")).json()["status"] == "running"
    events = await app.state.storage.list_events(project["id"], session["id"], 0, 100)
    assert next(json.loads(row["payload"])["request_id"] for row in events if row["kind"] == "raw") == raw["id"]
    assert any(row["kind"] == "permission.expired" for row in events)
    agent.finish()
    assert (await terminal(client, run["run_id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_model_from_handshake_and_push_survives_history_reload(api, http_records, monkeypatch):
    client, app, factory = api
    project, session = http_records
    original_connect = ControlledClient.connect
    response = {"jsonrpc": "2.0", "id": 2, "result": {
        "sessionId": "native-1", "configOptions": [
            {"id": "model", "category": "model", "type": "select", "currentValue": "gpt-6.1-sol"},
        ],
    }}

    async def connect(agent, cwd, servers):
        native_id = await original_connect(agent, cwd, servers)
        agent.observe(StreamEvent(StreamDirection.INCOMING, response))
        return native_id

    monkeypatch.setattr(ControlledClient, "connect", connect)
    run = await submit(client, session, "模型显示")
    agent, _ = await factory.started()
    update = {"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": "native-1", "update": {"sessionUpdate": "config_option_update", "configOptions": [
            {"id": "model", "category": "model", "type": "select", "currentValue": "updated-model"},
        ]},
    }}
    agent.observe(StreamEvent(StreamDirection.INCOMING, update))
    agent.finish("完成")
    assert (await terminal(client, run["run_id"]))["status"] == "completed"
    path = f"/api/events?project_id={project['id']}&session_id={session['id']}"
    history = (await client.get(path)).json()
    models = [row for row in history if row["kind"] == "session.model"]
    assert [row["payload"]["model_id"] for row in models] == ["gpt-6.1-sol", "updated-model"]
    assert all("raw" not in row["payload"] for row in models)
    assert all(row["session_id"] == session["id"] and row["run_id"] == run["run_id"] for row in models)
    assert (await client.get(path)).json() == history
    other = await new_session(client, project)
    assert (await client.get(f"/api/events?project_id={project['id']}&session_id={other['id']}")).json() == []


@pytest.mark.asyncio
async def test_shutdown_awaits_tasks_and_closes_agent_before_database(tmp_path, repository, monkeypatch):
    factory = ControlledFactory()
    app = create_app(tmp_path / "shutdown.db", factory)
    original_close = Storage.close

    async def close_storage(storage):
        factory.close_order.append("database")
        await original_close(storage)

    monkeypatch.setattr(Storage, "close", close_storage)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            project = (await client.post("/api/projects", json={"path": str(repository), "name": "关闭", "check_argv": ["python"]})).json()
            session = await new_session(client, project)
            first = await submit(client, session, "运行中")
            await factory.started()
            queued = await submit(client, session, "尚未启动")
    runtime = app.state.service.sessions[session["id"]]
    assert runtime.dispatcher.done() and runtime.active is None
    assert factory.close_order[-2:] == ["agent", "database"]
    reopened = Storage(tmp_path / "shutdown.db")
    await reopened.open()
    try:
        assert (await reopened.get_run(first["run_id"]))["status"] == "canceled"
        assert (await reopened.get_run(queued["run_id"]))["status"] == "canceled"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_event_write_failure_never_reports_completed(api, http_records, monkeypatch):
    client, app, factory = api
    _, session = http_records
    run = await submit(client, session, "落库失败")
    agent, _ = await factory.started()

    async def fail(*args, **kwargs):
        raise RuntimeError("磁盘写入失败")

    monkeypatch.setattr(app.state.storage, "append_events", fail)
    agent.emit("不会展示")
    final = await terminal(client, run["run_id"])
    assert final["status"] == "failed" and "磁盘写入失败" in final["error"]
    assert agent.closed and final["result"] == ""


@pytest.mark.asyncio
async def test_slow_connect_does_not_block_submit_or_queued_cancel(api, http_records):
    client, _, factory = api
    _, session = http_records
    factory.connect_release.clear()
    first = await submit(client, session, "连接中")
    await asyncio.wait_for(factory.connect_started.wait(), 3)
    second = await asyncio.wait_for(submit(client, session, "仍可入队"), 1)
    response = await client.post(f"/api/runs/{second['run_id']}/cancel")
    assert response.json()["status"] == "canceled" and factory.starts.empty()
    cancel = await asyncio.wait_for(client.post(f"/api/runs/{first['run_id']}/cancel"), 1)
    assert cancel.json()["status"] == "canceled"
    assert not factory.connect_release.is_set()
    assert factory.starts.empty() and factory.clients[0].closed


@pytest.mark.asyncio
async def test_submit_while_cancellation_settles_still_returns_immediately(api, http_records):
    client, _, factory = api
    _, session = http_records
    first = await submit(client, session, "取消中")
    agent, _ = await factory.started()
    factory.cancel_release.clear()
    cancel = asyncio.create_task(client.post(f"/api/runs/{first['run_id']}/cancel"))
    await asyncio.wait_for(factory.cancel_entered.wait(), 3)
    agent.finish()
    second = await asyncio.wait_for(submit(client, session, "无需等待取消结果"), 1)
    assert not cancel.done() and factory.starts.empty()
    factory.cancel_release.set()
    await cancel
    agent, _ = await factory.started()
    agent.finish()
    await terminal(client, second["run_id"])


@pytest.mark.asyncio
async def test_shutdown_during_connect_reaps_execution_and_journal(tmp_path, repository):
    factory = ControlledFactory()
    factory.connect_release.clear()
    app = create_app(tmp_path / "connect-shutdown.db", factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            project = (await client.post("/api/projects", json={"path": str(repository), "name": "连接", "check_argv": ["python"]})).json()
            session = await new_session(client, project)
            run = await submit(client, session, "连接被关闭")
            await asyncio.wait_for(factory.connect_started.wait(), 3)
            execution = app.state.service.sessions[session["id"]].active
    assert execution.task.done() and execution.journal.task.done()
    assert factory.clients[0].closed
    reopened = Storage(tmp_path / "connect-shutdown.db")
    await reopened.open()
    try:
        assert (await reopened.get_run(run["run_id"]))["status"] == "interrupted"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_agent_cleanup_error_still_closes_database(tmp_path, repository, monkeypatch):
    factory = ControlledFactory()
    app = create_app(tmp_path / "cleanup.db", factory)
    original_close = ControlledClient.close

    async def fail_close(agent):
        await original_close(agent)
        raise RuntimeError("清理错误")

    monkeypatch.setattr(ControlledClient, "close", fail_close)
    with pytest.raises(ExceptionGroup, match="编排器关闭失败"):
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                project = (await client.post("/api/projects", json={"path": str(repository), "name": "清理", "check_argv": ["python"]})).json()
                session = await new_session(client, project)
                run = await submit(client, session, "清理出错")
                agent, _ = await factory.started()
                agent.finish()
                await terminal(client, run["run_id"])
    with pytest.raises(RuntimeError, match="已关闭"):
        await app.state.storage.get_run(run["run_id"])


@pytest.mark.asyncio
async def test_event_write_fails_after_prompt_response_still_closes_agent(api, http_records, monkeypatch):
    client, app, factory = api
    _, session = http_records
    run = await submit(client, session, "完成后才写失败")
    agent, _ = await factory.started()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_failure(*args, **kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("收尾写入失败")

    monkeypatch.setattr(app.state.storage, "append_events", delayed_failure)
    agent.emit("最后一块")
    await asyncio.wait_for(entered.wait(), 3)
    agent.pending.set_result(PromptResponse(stop_reason="end_turn"))
    execution = app.state.service.sessions[session["id"]].active
    await asyncio.wait_for(asyncio.shield(execution.prompt), 3)
    release.set()
    result = await terminal(client, run["run_id"])
    assert result["status"] == "failed" and "收尾写入失败" in result["error"]
    assert agent.closed and execution.journal.task.done()


@pytest.mark.asyncio
async def test_uvicorn_shutdown_with_open_sse_reaches_lifespan_cleanup(tmp_path, repository):
    factory = ControlledFactory()
    app = create_app(tmp_path / "server-shutdown.db", factory)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", timeout_graceful_shutdown=0.05))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(0.005)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=3) as client:
            project = (await client.post("/api/projects", json={"path": str(repository), "name": "服务关闭", "check_argv": ["python"]})).json()
            session = await new_session(client, project)
            run = await submit(client, session, "流仍连接时关闭服务")
            agent, _ = await factory.started()
            async with client.stream("GET", "/api/stream", params={"project_id": project["id"]}) as response:
                assert response.status_code == 200
                server.should_exit = True
                await asyncio.wait_for(task, 3)
                assert agent.closed and app.state.service.closing
        with pytest.raises(RuntimeError, match="已关闭"):
            await app.state.storage.get_run(run["run_id"])
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 3)
        listener.close()
