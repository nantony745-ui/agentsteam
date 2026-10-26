"""设置使用独立 ACP 替身；运行路径保留真实 HTTP、SQLite、FIFO 和权限响应。"""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from acp.schema import PermissionOption, ToolCallUpdate

from app.agents.acp_client import session_model_id
from app.agents.harness_config import actual_values, configuration_fields
from app.main import create_app
from app.domain.models import TaskCreate
from test_http_runs import ControlledClient, ControlledFactory, submit, terminal
from test_storage import repository


def select(config_id, values, current):
    return {"id": config_id, "name": config_id, "type": "select", "currentValue": current,
            "options": [{"value": value, "name": value} for value in values]}


class ConfigClient(ControlledClient):
    def __init__(self, owner, *args):
        super().__init__(owner, *args)
        self.argv = ["node", "agent", "--permission-mode", "bypassPermissions"]
        self.env = {"OPENCODE_PERMISSION": '{"*":"allow"}', "CODEBUDDY_IS_SANDBOX": "1"}
        self.launch_values = {}
        self.launch_defaults = {}
        self.config_defaults = {}
        self.config_options = []
        self.modes = None
        self.calls = []
        self.rejected = set()

    async def connect(self, cwd, servers):
        self.servers = servers
        self.owner.connect_count += 1
        await super().connect(cwd, servers)
        if self.owner.failure:
            raise ConnectionError("测试握手失败")
        effort = {"codex": "reasoning_effort", "opencode": "effort",
                  "codebuddy": "thought_level", "deepseek": "reasoning"}[self.harness]
        self.effort = effort
        self.config_options = [select("model", ["m1", "fast"], "m1"), select(effort, ["low", "high"], "high")]
        if self.harness == "deepseek":
            self.config_options.append(select("sandbox", ["read-only", "workspace-write", "danger-full-access"], "workspace-write"))
            self.modes = {"currentModeId": "default", "availableModes": [
                {"id": "default", "name": "常规"}, {"id": "plan", "name": "计划"}]}
        else:
            choices = {"codex": ["read-only", "agent-full-access"], "opencode": ["build", "plan"],
                       "codebuddy": ["default", "bypassPermissions", "fullAccess"]}[self.harness]
            initial = {"codex": "agent-full-access", "opencode": "build", "codebuddy": "bypassPermissions"}[self.harness]
            if self.harness == "codebuddy" and self.env.get("CODEBUDDY_IS_SANDBOX") != "1":
                initial = "default"
            self.config_options.append(select("mode", choices, initial))
        self.config_defaults = {**actual_values(self), **self.launch_defaults}
        return "native-settings-test"

    async def configure(self, config_id, value):
        if config_id in self.rejected:
            raise ValueError("Harness 拒绝此配置")
        self.calls.append((config_id, value))
        next(item for item in self.config_options if item["id"] == config_id)["currentValue"] = value
        if config_id == "model":
            target = next(item for item in self.config_options if item["id"] == self.effort)
            target["options"] = [{"value": value, "name": value} for value in (["low"] if value == "fast" else ["low", "high"])]
            target["currentValue"] = "low" if value == "fast" else "high"

    async def configure_mode(self, value):
        self.modes["currentModeId"] = value


class ConfigFactory(ControlledFactory):
    def __init__(self):
        super().__init__()
        self.connect_count = 0
        self.failure = False

    def __call__(self, *args):
        client = ConfigClient(self, *args)
        self.clients.append(client)
        return client


@pytest_asyncio.fixture
async def settings_api(tmp_path):
    factory = ConfigFactory()
    app = create_app(tmp_path / "settings.db", factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            yield client, app, factory


@pytest.mark.asyncio
@pytest.mark.parametrize("harness,effort,permission", [
    ("codex", "reasoning_effort", "mode"), ("opencode", "effort", "permission"),
    ("codebuddy", "thought_level", "mode"), ("deepseek", "reasoning", "sandbox"),
])
async def test_probe_real_fields_isolated_without_prompt(settings_api, harness, effort, permission):
    client, app, factory = settings_api
    listed = (await client.get("/api/settings/harnesses")).json()
    assert len(listed) == 4 and all(item["status"]["state"] == "unverified" for item in listed)
    response = await client.post(f"/api/settings/harnesses/{harness}/probe", json={})
    assert response.status_code == 200, response.text
    snapshot = response.json()
    fields = {item["id"]: item for item in snapshot["fields"]}
    assert fields["model"]["supported"] and fields[effort]["supported"] and fields[permission]["supported"]
    assert fields["model"]["default_value"] == "m1"
    assert factory.clients[0].servers == [] and factory.clients[0].closed
    assert factory.starts.empty()
    assert await app.state.storage.list_sessions(None) == []
    assert (await client.get("/api/settings/harnesses")).json()[list(item["id"] for item in listed).index(harness)]["status"]["state"] == "ready"


@pytest.mark.asyncio
async def test_model_dependent_effort_and_save_validation(settings_api):
    client, app, factory = settings_api
    url = "/api/settings/harnesses/deepseek"
    response = await client.post(url + "/probe", json={"config_values": {"model": "fast", "reasoning": "high"}})
    snapshot = response.json()
    assert snapshot["invalid"][0]["id"] == "reasoning"
    effort = next(field for field in snapshot["fields"] if field["id"] == "reasoning")
    assert [option["value"] for option in effort["options"]] == ["low"]
    assert (await client.put(url, json={"config_values": {"model": "fast", "reasoning": "high"}})).status_code == 422
    for values in [{"unknown": "x"}, {"model": "missing"}]:
        assert (await client.put(url, json={"config_values": values})).status_code == 422
    values = {"model": "fast", "reasoning": "low", "session_mode": "plan", "sandbox": "read-only"}
    saved = await client.put(url, json={"config_values": values})
    assert saved.status_code == 200, saved.text
    assert saved.json()["config_values"] == values and saved.json()["revision"] == 1
    assert (await app.state.storage.get_harness_settings("codex"))["revision"] == 0
    assert all(agent.closed for agent in factory.clients)


@pytest.mark.asyncio
async def test_cache_serialization_failure_cleanup_and_cwd(settings_api, repository):
    client, app, factory = settings_api
    project = (await client.post("/api/projects", json={"path": str(repository)})).json()
    body = {"project_id": project["id"]}
    url = "/api/settings/harnesses/codex/probe"
    responses = await asyncio.gather(*(client.post(url, json=body) for _ in range(3)))
    assert all(response.status_code == 200 for response in responses)
    assert factory.connect_count == 1 and responses[0].json()["cwd"] == str(repository)
    await client.post(url, json={**body, "force": True})
    assert factory.connect_count == 2
    factory.failure = True
    assert (await client.post(url, json={"force": True})).status_code == 502
    assert factory.clients[-1].closed
    assert (await client.post(url, json={"project_id": str(uuid4())})).status_code == 404


@pytest.mark.asyncio
async def test_storage_settings_survive_reopen(tmp_path):
    from app.db.storage import Storage

    path = tmp_path / "persist.db"
    db = Storage(path)
    await db.open()
    await db.save_harness_settings("codex", {"mode": "read-only"})
    await db.save_harness_settings("codex", {})
    await db.close()
    await db.open()
    try:
        saved = await db.get_harness_settings("codex")
        assert saved["revision"] == 2 and saved["config_values"] == {}
    finally:
        await db.close()


async def create_session(client, project, harness):
    return (await client.post("/api/sessions", json={"project_id": project["id"], "harness": harness})).json()


@pytest.mark.asyncio
async def test_next_turn_updates_without_affecting_active_turn_and_resets_defaults(settings_api, repository):
    client, app, factory = settings_api
    project = (await client.post("/api/projects", json={"path": str(repository)})).json()
    session = await create_session(client, project, "codex")
    await app.state.storage.save_harness_settings("codex", {"model": "m1"})
    first = await submit(client, session, "第一轮")
    agent, _ = await factory.started()
    await app.state.storage.save_harness_settings("codex", {"model": "fast", "reasoning_effort": "low", "mode": "read-only"})
    assert actual_values(agent)["model"] == "m1"
    assert app.state.service.sessions[session["id"]].automatic_approval
    agent.finish()
    await terminal(client, first["run_id"])
    second = await submit(client, session, "第二轮")
    reused, _ = await factory.started()
    assert reused is agent and agent.calls[0] == ("model", "fast")
    assert actual_values(agent)["reasoning_effort"] == "low"
    assert not app.state.service.sessions[session["id"]].automatic_approval
    callback = asyncio.create_task(agent.host.request_permission("native", ToolCallUpdate(tool_call_id="t", title="写入"), [
        PermissionOption(option_id="allow", name="允许", kind="allow_once")]))
    for _ in range(100):
        pending = await app.state.storage.list_interactions(session["id"])
        if pending:
            break
        await asyncio.sleep(0.01)
    assert not callback.done()
    answer = await client.post(f"/api/interactions/{pending[0]['id']}/answer", json={"option_id": "allow"})
    assert answer.status_code == 200
    assert (await callback).outcome.option_id == "allow"
    agent.finish()
    await terminal(client, second["run_id"])
    await app.state.storage.save_harness_settings("codex", {})
    third = await submit(client, session, "恢复默认")
    await factory.started()
    assert actual_values(agent)["model"] == "m1" and actual_values(agent)["mode"] == "agent-full-access"
    assert app.state.service.sessions[session["id"]].automatic_approval
    agent.finish()
    await terminal(client, third["run_id"])
    history = (await client.get(f"/api/sessions/{session['id']}/history")).json()
    assert next(row for row in history["metadata"] if row["kind"] == "session.config")["payload"]["revision"] == 3


@pytest.mark.asyncio
async def test_startup_only_permission_retained_with_warning(settings_api, repository):
    client, app, factory = settings_api
    project = (await client.post("/api/projects", json={"path": str(repository)})).json()
    await app.state.storage.save_harness_settings("opencode", {"permission": "allow"})
    session = await create_session(client, project, "opencode")
    first = await submit(client, session, "旧会话")
    agent, _ = await factory.started()
    agent.finish()
    await terminal(client, first["run_id"])
    await app.state.storage.save_harness_settings("opencode", {"permission": "ask", "model": "fast"})
    second = await submit(client, session, "保持上下文")
    reused, _ = await factory.started()
    assert reused is agent and actual_values(agent)["permission"] == "allow"
    assert actual_values(agent)["model"] == "fast"
    assert app.state.service.sessions[session["id"]].automatic_approval
    agent.finish()
    await terminal(client, second["run_id"])
    rows = (await client.get("/api/events", params={"project_id": project["id"]})).json()
    config = [row["payload"] for row in rows if row["kind"] == "session.config"][-1]
    assert config["actual"]["permission"] == "allow" and config["warnings"][0]["id"] == "permission"
    new = await create_session(client, project, "opencode")
    run = await submit(client, new, "新权限")
    new_agent, _ = await factory.started()
    assert actual_values(new_agent)["permission"] == "ask"
    assert not app.state.service.sessions[new["id"]].automatic_approval
    new_agent.finish()
    await terminal(client, run["run_id"])


def test_deepseek_reasoning_category_is_not_model():
    assert session_model_id({"result": {"configOptions": [
        {**select("reasoning", ["high"], "high"), "category": "model"}]}}) is None


def test_grouped_select_and_unsupported_fields():
    client = ConfigFactory()("codex", None, None, None, None)
    client.config_options = [{**select("model", [], "p/m"), "options": [
        {"group": "p", "name": "Provider", "options": [{"value": "p/m", "name": "Model"}]}]}]
    fields = configuration_fields(client)
    assert fields[0]["options"][0]["group"] == "Provider"
    assert not fields[1]["supported"] and not fields[2]["supported"]


@pytest.mark.asyncio
async def test_worker_and_main_share_defaults_and_rejected_update_keeps_actual(settings_api, repository):
    client, app, factory = settings_api
    project = (await client.post("/api/projects", json={"path": str(repository)})).json()
    await app.state.storage.save_harness_settings("codex", {"model": "fast", "mode": "read-only"})
    parent = await create_session(client, project, "codex")
    main = await submit(client, parent, "主会话")
    agent, _ = await factory.started()
    assert actual_values(agent)["model"] == "fast"
    agent.finish()
    await terminal(client, main["run_id"])
    worker = await app.state.service.delegate_task(parent["id"], TaskCreate(
        harness="codex", goal="只读检查", acceptance="返回检查结果", request_id=str(uuid4())))
    child, _ = await factory.started()
    assert actual_values(child)["model"] == "fast"
    assert not app.state.service.sessions[worker["child_session_id"]].automatic_approval
    child.finish()
    await terminal(client, worker["run_id"])
    agent.rejected.add("model")
    await app.state.storage.save_harness_settings("codex", {"model": "m1", "mode": "read-only"})
    run = await submit(client, parent, "继续旧模型")
    await factory.started()
    assert actual_values(agent)["model"] == "fast"
    agent.finish()
    await terminal(client, run["run_id"])
    history = (await client.get(f"/api/sessions/{parent['id']}/history")).json()
    config = next(row["payload"] for row in history["metadata"] if row["kind"] == "session.config")
    assert config["actual"]["model"] == "fast" and config["warnings"][0]["id"] == "model"


@pytest.mark.asyncio
async def test_codebuddy_launch_permissions_and_hot_change_limitation(settings_api, repository):
    client, app, factory = settings_api
    project = (await client.post("/api/projects", json={"path": str(repository)})).json()
    await app.state.storage.save_harness_settings("codebuddy", {"mode": "fullAccess"})
    session = await create_session(client, project, "codebuddy")
    first = await submit(client, session, "全访问")
    agent, _ = await factory.started()
    assert agent.env["CODEBUDDY_IS_SANDBOX"] == "1"
    agent.finish()
    await terminal(client, first["run_id"])
    await app.state.storage.save_harness_settings("codebuddy", {"mode": "default"})
    second = await submit(client, session, "继续旧权限")
    await factory.started()
    assert actual_values(agent)["mode"] == "fullAccess"
    assert app.state.service.sessions[session["id"]].automatic_approval
    agent.finish()
    await terminal(client, second["run_id"])
    new = await create_session(client, project, "codebuddy")
    third = await submit(client, new, "受限权限")
    restricted, _ = await factory.started()
    assert "CODEBUDDY_IS_SANDBOX" not in restricted.env
    assert actual_values(restricted)["mode"] == "default"
    assert not app.state.service.sessions[new["id"]].automatic_approval
    restricted.finish()
    await terminal(client, third["run_id"])


@pytest.mark.asyncio
async def test_manual_permission_cancel_expires_pending_request(settings_api, repository):
    client, app, factory = settings_api
    project = (await client.post("/api/projects", json={"path": str(repository)})).json()
    await app.state.storage.save_harness_settings("deepseek", {"sandbox": "read-only"})
    session = await create_session(client, project, "deepseek")
    run = await submit(client, session, "需要确认")
    agent, _ = await factory.started()
    callback = asyncio.create_task(agent.host.request_permission("native", ToolCallUpdate(tool_call_id="t"), [
        PermissionOption(option_id="allow", name="允许", kind="allow_once")]))
    async with asyncio.timeout(3):
        while not await app.state.storage.list_interactions(session["id"]):
            await asyncio.sleep(0.01)
    response = await client.post(f"/api/runs/{run['run_id']}/cancel")
    assert response.status_code == 200, response.text
    assert (await callback).outcome.outcome == "cancelled"
    assert await app.state.storage.list_interactions(session["id"]) == []


@pytest.mark.asyncio
async def test_probe_cancellation_and_timeout_clean_up(settings_api, monkeypatch):
    client, app, factory = settings_api
    factory.connect_release.clear()
    task = asyncio.create_task(app.state.harness_settings.probe("codex", None, {}, True))
    await factory.connect_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert factory.clients[-1].closed
    timeout = asyncio.timeout
    monkeypatch.setattr("app.services.harness_settings.asyncio.timeout", lambda seconds: timeout(0.02))
    response = await client.post("/api/settings/harnesses/codex/probe", json={"force": True})
    assert response.status_code == 502
    assert factory.clients[-1].closed


@pytest.mark.asyncio
async def test_probes_serialize_per_harness_without_blocking_other_harnesses(settings_api):
    _, app, factory = settings_api
    service = app.state.harness_settings
    factory.connect_release.clear()
    tasks = [asyncio.create_task(service.probe(name, None, {}, False)) for name in ["codex", "codex", "opencode"]]
    async with asyncio.timeout(3):
        while factory.connect_count < 2:
            await asyncio.sleep(0.01)
    assert factory.connect_count == 2
    factory.connect_release.set()
    snapshots = await asyncio.gather(*tasks)
    assert len(snapshots) == 3 and factory.connect_count == 2
    assert all(agent.closed for agent in factory.clients)


@pytest.mark.asyncio
async def test_removing_effort_override_uses_current_models_default(settings_api, repository):
    client, app, factory = settings_api
    project = (await client.post("/api/projects", json={"path": str(repository)})).json()
    await app.state.storage.save_harness_settings("codex", {"model": "fast", "reasoning_effort": "low"})
    session = await create_session(client, project, "codex")
    first = await submit(client, session, "低档")
    agent, _ = await factory.started()
    agent.finish()
    await terminal(client, first["run_id"])
    await app.state.storage.save_harness_settings("codex", {"model": "fast"})
    second = await submit(client, session, "继承当前模型默认")
    await factory.started()
    assert actual_values(agent)["reasoning_effort"] == "low"
    agent.finish()
    await terminal(client, second["run_id"])
    history = (await client.get(f"/api/sessions/{session['id']}/history")).json()
    config = next(row["payload"] for row in history["metadata"] if row["kind"] == "session.config")
    assert config["warnings"] == []


@pytest.mark.asyncio
async def test_partial_permission_reset_keeps_rejected_restriction(settings_api, repository):
    client, app, factory = settings_api
    project = (await client.post("/api/projects", json={"path": str(repository)})).json()
    await app.state.storage.save_harness_settings("codebuddy", {"mode": "default"})
    session = await create_session(client, project, "codebuddy")
    first = await submit(client, session, "受限权限")
    agent, _ = await factory.started()
    agent.config_options.append(select("sandbox", ["enabled", "disabled"], "enabled"))
    agent.config_defaults["sandbox"] = "disabled"
    runtime = app.state.service.sessions[session["id"]]
    runtime.config_overrides["sandbox"] = "enabled"
    assert not runtime.automatic_approval
    agent.finish()
    await terminal(client, first["run_id"])
    agent.rejected.add("mode")
    await app.state.storage.save_harness_settings("codebuddy", {})
    second = await submit(client, session, "部分恢复默认")
    await factory.started()
    assert actual_values(agent)["sandbox"] == "disabled"
    assert actual_values(agent)["mode"] == "default"
    assert not runtime.automatic_approval
    agent.finish()
    await terminal(client, second["run_id"])
