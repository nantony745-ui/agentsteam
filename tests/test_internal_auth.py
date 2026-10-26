"""内部接口身份、归属、注入和凭据生命周期；仅 ACP 边界使用替身。"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from acp.connection import StreamDirection, StreamEvent
from acp.schema import ToolCallUpdate

from app.agents import acp_client
from app.domain.models import TaskCreate
from app.main import create_app
from test_http_runs import ControlledFactory, new_session, submit, terminal
from test_permissions import PermissionClient, options
from test_scheduler import child_session
from test_storage import repository


class IdentityClient(PermissionClient):
    def __init__(self, owner, *args):
        super().__init__(owner, *args)
        self.record = args[2]
        self.servers = []
        self.connect_count = 0

    async def connect(self, cwd, servers):
        self.servers = servers
        self.connect_count += 1
        env = {item.name: item.value for item in servers[0].env}
        self.token = env["HARNESS_LAB_TOKEN"]
        assert self.token in self.owner.service.tokens
        self.record("test_config", {"env": env})
        return await super().connect(cwd, servers)


class IdentityFactory(ControlledFactory):
    def __call__(self, *args):
        instance = IdentityClient(self, *args)
        self.clients.append(instance)
        return instance


@pytest_asyncio.fixture
async def identity_api(tmp_path, repository):
    factory = IdentityFactory()
    app = create_app(tmp_path / "identity.db", factory, internal_base_url="http://127.0.0.1:9876")
    async with app.router.lifespan_context(app):
        factory.service = app.state.service
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as http:
            project = (await http.post("/api/projects", json={"path": str(repository), "name": "身份测试"})).json()
            yield http, app, factory, project


async def start_session(context, session):
    http, app, factory, _ = context
    run = await submit(http, session, "只读")
    agent, _ = await factory.started()
    return run, agent, {"Authorization": "Bearer " + agent.token}


async def seed_task(app, parent):
    task, _, _, _ = await app.state.storage.create_task_with_run(parent["id"], TaskCreate(
        harness="codex", goal="只读", acceptance="结构", request_id=str(uuid4()),
    ))
    return task["id"]


@pytest.mark.asyncio
async def test_internal_auth_roles_ownership_and_task_operations(identity_api):
    http, app, factory, project = identity_api
    parent_a, parent_b = await new_session(http, project), await new_session(http, project)
    child = await child_session(app, parent_a)
    _, agent_a, auth_a = await start_session(identity_api, parent_a)
    _, agent_b, auth_b = await start_session(identity_api, parent_b)
    _, worker, auth_worker = await start_session(identity_api, child)
    assert len({agent_a.token, agent_b.token, worker.token}) == 3
    assert {item.name: item.value for item in worker.servers[0].env}["HARNESS_LAB_ROLE"] == "worker"
    task_a, task_b = await seed_task(app, parent_a), await seed_task(app, parent_b)
    delegate = {"harness": "codex", "goal": "阅读", "acceptance": "结构", "request_id": str(uuid4())}
    routes = [("/internal/tasks", delegate), ("/internal/tasks/status", {"task_ids": [task_a]}),
              (f"/internal/tasks/{task_a}/result", {}), (f"/internal/tasks/{task_a}/cancel", {}),
              ("/internal/feedback/pull", {})]
    for path, body in routes:
        for headers in ({}, {"Authorization": "Bearer unknown"}, {"Authorization": "Basic abc"}):
            assert (await http.post(path, json=body, headers=headers)).status_code == 401
        expected = 202 if path == "/internal/tasks" else 200
        assert (await http.post(path, json=body, headers=auth_a)).status_code == expected
        if path != "/internal/feedback/pull":
            assert (await http.post(path, json=body, headers=auth_worker)).status_code == 403
        assert (await http.post(path, json={**body, "parent_session_id": parent_b["id"]}, headers=auth_a)).status_code == 422
    assert (await http.post("/internal/feedback/pull", json={}, headers=auth_worker)).status_code == 200
    for task_id in (task_b, str(uuid4())):
        for suffix in ("result", "cancel"):
            response = await http.post(f"/internal/tasks/{task_id}/{suffix}", json={}, headers=auth_a)
            assert response.status_code == 404
        response = await http.post("/internal/tasks/status", json={"task_ids": [task_a, task_id]}, headers=auth_a)
        assert response.status_code == 404
    assert (await http.post(f"/internal/tasks/{task_b}/result", json={}, headers=auth_b)).status_code == 200
    for wait_ms in (-1, 1200001, True):
        assert (await http.post("/internal/tasks/status", json={"task_ids": [task_a], "wait_ms": wait_ms}, headers=auth_a)).status_code == 422
    assert (await http.post("/internal/tasks/status", json={"task_ids": ["invalid"]}, headers=auth_a)).status_code == 422
    # 身份还要核对数据库，不能只相信注册时的角色。
    async with app.state.storage.transaction() as db:
        await db.execute("UPDATE sessions SET parent_id=? WHERE id=?", (parent_b["id"], parent_a["id"]))
    assert (await http.post("/internal/tasks", json=delegate, headers=auth_a)).status_code == 403


@pytest.mark.asyncio
async def test_injection_absolute_paths_reuse_and_no_secret_in_history(identity_api, caplog):
    http, app, factory, project = identity_api
    caplog.set_level(logging.DEBUG, logger="app.services.orchestrator")
    parent = await new_session(http, project)
    run, agent, auth = await start_session(identity_api, parent)
    server = agent.servers[0]
    assert server.name == "harness_lab"
    assert Path(server.command).is_absolute() and Path(server.command) == Path(sys.executable).resolve()
    assert Path(server.args[0]).is_absolute() and Path(server.args[0]).is_file()
    env = {item.name: item.value for item in server.env}
    assert env["HARNESS_LAB_URL"] == "http://127.0.0.1:9876"
    assert env["HARNESS_LAB_ROLE"] == "coordinator"
    agent.finish("模型错误地回显凭据 " + agent.token)
    result = await terminal(http, run["run_id"])
    assert agent.token not in json.dumps(result)
    assert (await http.post("/internal/feedback/pull", json={}, headers=auth)).status_code == 200
    next_run = await submit(http, parent, "下一轮")
    reused, _ = await factory.started()
    assert reused is agent and agent.connect_count == 1
    await http.post(f"/api/runs/{next_run['run_id']}/cancel")
    assert agent.token in app.state.service.tokens
    events = await app.state.storage.list_events(project["id"], parent["id"], 0, 1000)
    assert agent.token not in json.dumps(events)
    assert agent.token not in caplog.text
    await agent.close()
    assert (await http.post("/internal/feedback/pull", json={}, headers=auth)).status_code == 401


@pytest.mark.asyncio
async def test_sdk_session_new_receives_bridge_and_worktree_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(acp_client, "load_argv", lambda name: ["test-only.exe"])
    captured = {}

    async def initialize(**kwargs):
        return SimpleNamespace(model_dump=lambda **kwargs: {})

    async def new_native_session(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(session_id="native-injection", model_dump=lambda **kwargs: {})

    instance = acp_client.AcpClient("codex", SimpleNamespace(), lambda kind, data: None,
                                    lambda event: None, lambda reason: None)
    instance.conn = SimpleNamespace(initialize=initialize, new_session=new_native_session)
    cwd = tmp_path / "worktree with spaces"
    cwd.mkdir()
    bridge = acp_client.build_mcp_bridge("http://127.0.0.1:8765", "test-secret", "worker")
    await instance._handshake(cwd, [bridge])
    assert captured == {"cwd": str(cwd), "mcp_servers": [bridge]}
    assert instance.session_id == "native-injection"
    assert {item.name: item.value for item in bridge.env}["HARNESS_LAB_ROLE"] == "worker"


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["process_exit", "connect_failure", "connect_cancel", "connect_shutdown", "shutdown", "force_cancel"])
async def test_token_revoked_on_connection_lifecycle(identity_api, ending):
    http, app, factory, project = identity_api
    parent = await new_session(http, project)
    if ending.startswith("connect_"):
        factory.connect_release.clear()
        factory.connect_error = ending == "connect_failure"
        run = await submit(http, parent, "连接阶段")
        await asyncio.wait_for(factory.connect_started.wait(), 3)
        agent = factory.clients[0]
        token = agent.token
        if ending == "connect_failure":
            factory.connect_release.set()
        elif ending == "connect_shutdown":
            await app.state.service.close()
        else:
            await http.post(f"/api/runs/{run['run_id']}/cancel")
        await terminal(http, run["run_id"])
    else:
        run, agent, _ = await start_session(identity_api, parent)
        token = agent.token
        if ending == "process_exit":
            await agent.disconnect()
        elif ending == "force_cancel":
            factory.force_cancel = True
            await http.post(f"/api/runs/{run['run_id']}/cancel")
        else:
            await app.state.service.close()
        await terminal(http, run["run_id"])
    assert token not in app.state.service.tokens
    assert (await http.post("/internal/feedback/pull", json={}, headers={"Authorization": "Bearer " + token})).status_code == 401


@pytest.mark.asyncio
async def test_error_payloads_are_redacted(identity_api, caplog):
    http, app, factory, project = identity_api
    caplog.set_level(logging.DEBUG, logger="app.services.orchestrator")
    parent = await new_session(http, project)
    run, agent, _ = await start_session(identity_api, parent)
    agent.observe(StreamEvent(StreamDirection.INCOMING, {"jsonrpc": "2.0", "id": 7,
        "error": {"code": -32603, "message": agent.token, "data": {"nested": [agent.token]}}}))
    agent.pending.set_exception(RuntimeError("回显 " + agent.token))
    result = await terminal(http, run["run_id"])
    assert result["status"] == "failed"
    assert agent.token not in json.dumps(result)
    assert agent.token not in json.dumps(await app.state.storage.list_events(project["id"], parent["id"], 0, 1000))
    assert agent.token not in caplog.text


@pytest.mark.asyncio
async def test_permission_payload_is_redacted_without_changing_options(identity_api):
    http, app, factory, project = identity_api
    parent = await new_session(http, project)
    run, agent, _ = await start_session(identity_api, parent)
    # 元数据和工具输入可能回显 MCP 配置，不能进入 Interaction 或 SSE。
    callback = asyncio.create_task(agent.host.request_permission(
        "native", ToolCallUpdate(tool_call_id="permission", raw_input={"token": agent.token}),
        options(), meta={"echo": agent.token},
    ))
    assert (await asyncio.wait_for(callback, 3)).outcome.option_id == options()[1].option_id
    events = await app.state.storage.list_events(project["id"], parent["id"], 0, 1000)
    requested = next(json.loads(row["payload"]) for row in events if row["kind"] == "permission.requested")
    answered = await app.state.storage.get_interaction(requested["permission_id"])
    assert answered["status"] == "answered" and agent.token not in json.dumps(answered)
    agent.finish("完成")
    await terminal(http, run["run_id"])
    events = await app.state.storage.list_events(project["id"], parent["id"], 0, 1000)
    assert agent.token not in json.dumps(events)


@pytest.mark.asyncio
async def test_restart_does_not_restore_tokens(identity_api):
    http, app, factory, project = identity_api
    parent = await new_session(http, project)
    _, agent, auth = await start_session(identity_api, parent)
    database_path = app.state.storage.path
    await app.state.service.close()
    await app.state.storage.close()
    restarted = create_app(database_path, IdentityFactory())
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(restarted), base_url="http://test") as client:
            assert restarted.state.service.tokens == {}
            assert (await client.post("/internal/feedback/pull", json={}, headers=auth)).status_code == 401
