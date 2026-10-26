"""3.4：真实 HTTP、编排器和临时 SQLite；仅 ACP 使用可控替身。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from acp.schema import PromptResponse

from app.agents import acp_client
from app.db.storage import Storage
from app.domain.models import RunStatus, TaskCreate
from app.main import create_app
from app.services.orchestrator import SessionUnavailableError
from test_http_runs import new_session, submit, terminal
from test_internal_auth import IdentityClient, IdentityFactory
from test_permissions import pause_auto_answers, interactions, options
from test_storage import repository


class DelegationClient(IdentityClient):
    async def prompt(self, text):
        if self.owner.immediate:
            self.owner.starts.put_nowait((self, text))
            self.emit("立即完成的结果")
            self.owner.ends.append(text)
            return PromptResponse(stop_reason="end_turn")
        return await super().prompt(text)


class DelegationFactory(IdentityFactory):
    def __init__(self):
        super().__init__()
        self.immediate = False

    def __call__(self, *args):
        agent = DelegationClient(self, *args)
        self.clients.append(agent)
        return agent


@pytest_asyncio.fixture
async def delegation(tmp_path, repository):
    factory = DelegationFactory()
    app = create_app(tmp_path / "delegation.db", factory)
    async with app.router.lifespan_context(app):
        factory.service = app.state.service
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as http:
            response = await http.post("/api/projects", json={"path": str(repository), "name": "委派测试"})
            project = response.json()
            parent = await new_session(http, project)
            main_run = await submit(http, parent, "只读主会话")
            agent, _ = await factory.started()
            agent.finish("主会话已就绪")
            await terminal(http, main_run["run_id"])
            yield http, app, factory, project, parent, {"Authorization": "Bearer " + agent.token}


def task_input():
    return {"harness": "opencode", "goal": "只读列出文件 ' 原文\n ",
            "acceptance": "返回文件名及用途", "request_id": str(uuid4())}


def database_snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("sessions", "tasks", "runs", "events")}


async def delegate(context, body):
    http, _, _, _, _, auth = context
    response = await http.post("/internal/tasks", json=body, headers=auth)
    assert response.status_code == 202, response.text
    return response.json()


async def drain(app, handle):
    async with asyncio.timeout(3):
        await app.state.service.sessions[handle["child_session_id"]].queue.join()
        await app.state.service.worker_queue.join()


@pytest.mark.asyncio
async def test_delegate_commits_before_claim_and_returns_queued(delegation, monkeypatch):
    http, app, factory, project, parent, _ = delegation
    service, storage = app.state.service, app.state.storage
    entered, release = asyncio.Event(), asyncio.Event()
    execute = service.execute_run

    async def before_claim(run_id):
        entered.set()
        await release.wait()
        await execute(run_id)

    monkeypatch.setattr(service, "execute_run", before_claim)
    before = database_snapshot(storage.path)
    data = task_input()
    data.update(goal="修改 README.md，补充项目启动说明", acceptance="返回修改内容及验证结果")
    try:
        handle = await asyncio.wait_for(delegate(delegation, data), 3)
        await asyncio.wait_for(entered.wait(), 3)
        assert set(handle) == {"task_id", "child_session_id", "run_id", "status"}
        assert handle["status"] == "queued"
        state = database_snapshot(storage.path)
        assert [len(state[t]) - len(before[t]) for t in state] == [1, 1, 1, 3]
        task, child, run = state["tasks"][0], state["sessions"][-1], state["runs"][-1]
        assert task["id"] == handle["task_id"] and task["status"] == "queued"
        assert task["parent_session_id"] == child["parent_id"] == parent["id"]
        assert task["child_session_id"] == run["session_id"] == child["id"] == handle["child_session_id"]
        assert task["current_run_id"] == run["id"] == handle["run_id"]
        assert task["project_id"] == child["project_id"] == project["id"]
        assert child["harness"] == data["harness"] and child["cwd"] == project["path"]
        assert child["native_id"] is child["process_pid"] is None
        assert run["task_id"] == task["id"] and run["attempt"] == 1 and run["status"] == "queued"
        assert run["request_id"] == task["request_id"] == data["request_id"]
        assert run["prompt"] == f"目标：\n{data['goal']}\n\n验收要求：\n{data['acceptance']}"
        events = state["events"][len(before["events"]):]
        assert [event["kind"] for event in events] == ["task.created", "user.message", "run.status"]
        assert [event["session_id"] for event in events] == [parent["id"], child["id"], child["id"]]
        assert all(event["project_id"] == project["id"] and event["run_id"] == run["id"]
                   and event["task_id"] == task["id"] for event in events)
        assert json.loads(events[0]["payload"])["status"] == "queued"
        assert json.loads(events[1]["payload"])["message"] == run["prompt"]
        assert json.loads(events[2]["payload"]) == {"from": None, "status": "queued"}
        assert len(factory.clients) == 1
        history = (await http.get("/api/events", params={
            "project_id": project["id"], "session_id": parent["id"],
            "after_event_id": before["events"][-1]["id"],
        })).json()
        assert [event["id"] for event in history] == [events[0]["id"]]
    finally:
        release.set()
    agent, prompt = await factory.started()
    assert prompt == run["prompt"]
    agent.finish("只读清单")
    await drain(app, handle)


@pytest.mark.asyncio
async def test_concurrent_and_sequential_replays_execute_only_once(delegation):
    http, app, factory, _, parent, auth = delegation
    data = task_input()
    handles = await asyncio.gather(*(delegate(delegation, data) for _ in range(8)))
    first = handles[0]
    assert len({(h["task_id"], h["child_session_id"], h["run_id"]) for h in handles}) == 1
    agent, _ = await factory.started()
    before = database_snapshot(app.state.storage.path)
    repeated = await delegate(delegation, data)
    assert repeated == {**first, "status": "running"}
    assert database_snapshot(app.state.storage.path) == before
    for field, value in (("harness", "codex"), ("goal", "其他目标"), ("acceptance", "其他验收")):
        conflict = await http.post("/internal/tasks", json={**data, field: value}, headers=auth)
        assert conflict.status_code == 409
    assert database_snapshot(app.state.storage.path) == before
    agent.finish("唯一执行结果")
    await drain(app, first)
    before = database_snapshot(app.state.storage.path)
    assert await delegate(delegation, data) == {**first, "status": "review"}
    assert database_snapshot(app.state.storage.path) == before
    assert len(before["sessions"]) == 2 and len(before["tasks"]) == 1 and len(before["runs"]) == 2
    assert len(factory.clients) == 2 and len(factory.ends) == 2 and factory.starts.empty()
    assert (await app.state.storage.get_owned_task(first["task_id"], parent["id"]))["summary"] == "唯一执行结果"


@pytest.mark.asyncio
async def test_same_request_id_is_scoped_to_parent(delegation):
    http, app, factory, project, _, _ = delegation
    data = task_input()
    first = await delegate(delegation, data)
    worker, _ = await factory.started()
    worker.finish()
    await drain(app, first)
    other = await new_session(http, project)
    run = await submit(http, other, "其他主会话")
    agent, _ = await factory.started()
    agent.finish()
    await terminal(http, run["run_id"])
    response = await http.post("/internal/tasks", json=data, headers={"Authorization": "Bearer " + agent.token})
    assert response.status_code == 202
    second = response.json()
    assert all(first[key] != second[key] for key in ("task_id", "child_session_id", "run_id"))
    worker, _ = await factory.started()
    worker.finish()
    await drain(app, second)
    assert len(database_snapshot(app.state.storage.path)["tasks"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["task.created", "user.message", "run.status"])
async def test_creation_failure_rolls_back_all_objects_and_never_enqueues(delegation, failure_kind):
    http, app, factory, _, _, auth = delegation
    storage, service = app.state.storage, app.state.service
    before = database_snapshot(storage.path)
    async with storage.transaction() as db:
        await db.execute(
            "CREATE TEMP TRIGGER reject_delegation BEFORE INSERT ON events "
            f"WHEN NEW.kind='{failure_kind}' AND NEW.task_id IS NOT NULL "
            "BEGIN SELECT RAISE(ABORT, 'delegation rollback'); END"
        )
    data = task_input()
    with pytest.raises(sqlite3.IntegrityError, match="delegation rollback"):
        await http.post("/internal/tasks", json=data, headers=auth)
    assert database_snapshot(storage.path) == before
    assert len(service.sessions) == 1 and len(factory.clients) == 1
    assert service.worker_queue.empty() and not service.worker_tasks and not service.worker_waiters
    async with storage.transaction() as db:
        await db.execute("DROP TRIGGER reject_delegation")
    handle = await delegate(delegation, data)
    worker, _ = await factory.started()
    worker.finish()
    await drain(app, handle)


@pytest.mark.asyncio
async def test_immediate_completion_persists_result_and_parent_events(delegation):
    _, app, factory, project, parent, _ = delegation
    factory.immediate = True
    data = task_input()
    handle = await delegate(delegation, data)
    assert handle["status"] == "queued"
    await drain(app, handle)
    task = await app.state.storage.get_owned_task(handle["task_id"], parent["id"])
    run = await app.state.storage.get_run(handle["run_id"])
    assert run["status"] == "completed" and run["result"] == "立即完成的结果"
    assert task["status"] == "review" and task["summary"] == run["result"] and task["error"] is None
    events = await app.state.storage.list_events(project["id"], parent["id"], 0, 100)
    events = [e for e in events if e["task_id"] == task["id"]]
    assert [e["kind"] for e in events] == ["task.created", "task.status", "task.status"]
    assert [json.loads(e["payload"])["status"] for e in events] == ["queued", "running", "review"]
    assert all(e["run_id"] == run["id"] for e in events)
    assert task["updated_at"] == run["finished_at"]
    assert not app.state.service.worker_tasks and not app.state.service.worker_waiters
    before = database_snapshot(app.state.storage.path)
    assert (await delegate(delegation, data))["status"] == "review"
    assert database_snapshot(app.state.storage.path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", "prompt", "disconnect"])
async def test_failures_set_both_run_and_task_terminal(delegation, failure):
    http, app, factory, _, parent, _ = delegation
    factory.connect_error = failure == "connect"
    handle = await delegate(delegation, task_input())
    if failure != "connect":
        worker, _ = await factory.started()
        if failure == "prompt":
            worker.pending.set_exception(RuntimeError("执行失败"))
        else:
            await worker.disconnect()
    run = await terminal(http, handle["run_id"])
    await drain(app, handle)
    task = await app.state.storage.get_owned_task(handle["task_id"], parent["id"])
    assert run["status"] == ("failed" if failure == "prompt" else "interrupted")
    assert task["status"] == "failed" and task["error"] == run["error"]
    assert task["summary"] == run["result"] and run["error"]


@pytest.mark.asyncio
async def test_permission_cancel_requested_and_confirmed_cancel(delegation, monkeypatch):
    http, app, factory, _, parent, _ = delegation
    pause_auto_answers(app.state.service, monkeypatch)
    handle = await delegate(delegation, task_input())
    worker, _ = await factory.started()
    child = await app.state.storage.get_session(handle["child_session_id"])
    callback, _ = worker.permission(options())
    await interactions(http, child, 1)
    storage = app.state.storage
    assert (await storage.get_run(handle["run_id"]))["status"] == "waiting_permission"
    assert (await storage.get_owned_task(handle["task_id"], parent["id"]))["status"] == "running"
    factory.cancel_release.clear()
    cancel = asyncio.create_task(http.post(f"/api/runs/{handle['run_id']}/cancel"))
    try:
        await asyncio.wait_for(factory.cancel_entered.wait(), 3)
        assert (await storage.get_run(handle["run_id"]))["status"] == "cancel_requested"
        assert (await storage.get_owned_task(handle["task_id"], parent["id"]))["status"] == "running"
    finally:
        factory.cancel_release.set()
        response = await asyncio.wait_for(cancel, 3)
    assert response.json()["status"] == "canceled"
    assert (await callback).outcome.outcome == "cancelled"
    await drain(app, handle)
    assert (await storage.get_owned_task(handle["task_id"], parent["id"]))["status"] == "canceled"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex", "opencode", "codebuddy", "deepseek"])
async def test_default_auto_approval_matches_main_and_workers_without_blocking_sibling(delegation, harness):
    http, app, factory, project, parent, _ = delegation
    main_run = await submit(http, parent, "主会话自动授权")
    main, _ = await factory.started()
    first = await delegate(delegation, {**task_input(), "harness": harness})
    worker, _ = await factory.started()
    second = await delegate(delegation, task_input())
    sibling, _ = await factory.started()
    responses = await asyncio.wait_for(asyncio.gather(
        main.permission(options())[0], worker.permission(options())[0],
    ), 3)
    assert worker.harness == harness
    assert all(response.outcome.option_id == options()[1].option_id for response in responses)
    assert app.state.service.permissions == {}
    assert (await app.state.storage.get_run(main_run["run_id"]))["status"] == "running"
    assert (await app.state.storage.get_run(first["run_id"]))["status"] == "running"
    assert not sibling.pending.done()
    assert (await http.post(f"/api/runs/{first['run_id']}/cancel")).json()["status"] == "canceled"
    main.finish("主会话继续")
    sibling.finish("其他 Worker 继续")
    assert (await terminal(http, main_run["run_id"]))["result"] == "主会话继续"
    assert (await terminal(http, second["run_id"]))["result"] == "其他 Worker 继续"
    await drain(app, first)
    for session_id in (parent["id"], first["child_session_id"]):
        rows = await app.state.storage.list_events(project["id"], session_id, 0, 100)
        assert any(row["kind"] == "permission.answered" for row in rows)
        assert await app.state.storage.list_interactions(session_id) == []


@pytest.mark.asyncio
async def test_queued_cancel_never_starts_agent(delegation, monkeypatch):
    http, app, factory, _, parent, _ = delegation
    entered, release = asyncio.Event(), asyncio.Event()
    execute = app.state.service.execute_run

    async def before_claim(run_id):
        entered.set()
        await release.wait()
        await execute(run_id)

    monkeypatch.setattr(app.state.service, "execute_run", before_claim)
    handle = await delegate(delegation, task_input())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        response = await http.post(f"/api/runs/{handle['run_id']}/cancel")
        assert response.json()["status"] == "canceled" and response.json()["started_at"] is None
    finally:
        release.set()
    await drain(app, handle)
    assert len(factory.clients) == 1
    assert (await app.state.storage.get_owned_task(handle["task_id"], parent["id"]))["status"] == "canceled"


@pytest.mark.asyncio
async def test_disabled_harness_and_invalid_fields_leave_no_records(delegation, monkeypatch):
    http, app, _, _, parent, auth = delegation
    before = database_snapshot(app.state.storage.path)
    config = acp_client.load_agents()
    config["opencode"]["enabled"] = False
    monkeypatch.setattr(acp_client, "load_agents", lambda: config)
    data = task_input()
    assert (await http.post("/internal/tasks", json=data, headers=auth)).status_code == 409
    for body in ({**data, "parent_session_id": parent["id"]}, {**data, "request_id": "inventory-v1"}):
        assert (await http.post("/internal/tasks", json=body, headers=auth)).status_code == 422
    assert database_snapshot(app.state.storage.path) == before


@pytest.mark.asyncio
async def test_old_run_cannot_overwrite_current_task(delegation):
    _, app, factory, _, parent, _ = delegation
    factory.immediate = True
    handle = await delegate(delegation, task_input())
    await drain(app, handle)
    storage = app.state.storage
    new_id = str(uuid4())
    async with storage.transaction() as db:
        await db.execute(
            "INSERT INTO runs (id,session_id,task_id,attempt,request_id,prompt,status,created_at) "
            "SELECT ?,session_id,task_id,2,?,'新尝试','queued',created_at FROM runs WHERE id=?",
            (new_id, str(uuid4()), handle["run_id"]),
        )
        await db.execute("UPDATE tasks SET current_run_id=?,status='queued',summary=NULL WHERE id=?",
                         (new_id, handle["task_id"]))
        await db.execute("UPDATE runs SET status='running' WHERE id=?", (handle["run_id"],))
    before = database_snapshot(storage.path)
    assert not await storage.transition_run(handle["run_id"], {RunStatus.RUNNING}, RunStatus.COMPLETED,
                                            {"result": "迟到的旧结果"})
    assert database_snapshot(storage.path) == before
    assert await storage.transition_run(new_id, {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    assert await storage.transition_run(new_id, {RunStatus.RUNNING}, RunStatus.COMPLETED, {"result": "新结果"})
    assert (await storage.get_owned_task(handle["task_id"], parent["id"]))["summary"] == "新结果"


@pytest.mark.asyncio
async def test_task_event_failure_rolls_back_run_and_task_state(delegation):
    _, app, factory, _, parent, _ = delegation
    handle = await delegate(delegation, task_input())
    worker, _ = await factory.started()
    storage = app.state.storage
    async with storage.transaction() as db:
        await db.execute(
            "CREATE TEMP TRIGGER reject_task_status BEFORE INSERT ON events WHEN NEW.kind='task.status' "
            "BEGIN SELECT RAISE(ABORT, 'task status rollback'); END"
        )
    before = database_snapshot(storage.path)
    with pytest.raises(sqlite3.IntegrityError, match="task status rollback"):
        await storage.transition_run(handle["run_id"], {RunStatus.RUNNING}, RunStatus.COMPLETED,
                                     {"result": "应回滚"})
    assert database_snapshot(storage.path) == before
    assert (await storage.get_owned_task(handle["task_id"], parent["id"]))["status"] == "running"
    async with storage.transaction() as db:
        await db.execute("DROP TRIGGER reject_task_status")
    worker.finish()
    await drain(app, handle)


@pytest.mark.asyncio
async def test_queued_records_survive_reopen_without_auto_execution(delegation):
    _, app, factory, _, parent, _ = delegation
    storage = app.state.storage
    data = TaskCreate(**task_input())
    task, child, run, created = await storage.create_task_with_run(parent["id"], data)
    assert created and task["status"] == run["status"] == "queued"
    before = database_snapshot(storage.path)
    reopened = Storage(storage.path)
    await reopened.open()
    try:
        assert await reopened.create_task_with_run(parent["id"], data) == (task, child, run, False)
        assert await reopened.get_owned_task(task["id"], parent["id"]) == task
        assert await reopened.get_session(child["id"]) == child
        assert await reopened.get_run(run["id"]) == run
    finally:
        await reopened.close()
    assert database_snapshot(storage.path) == before
    assert len(factory.clients) == 1 and child["id"] not in app.state.service.sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connect", "prompt"])
async def test_shutdown_settles_delegated_task(delegation, phase):
    _, app, factory, _, parent, _ = delegation
    if phase == "connect":
        factory.connect_started.clear()
        factory.connect_release.clear()
    handle = await delegate(delegation, task_input())
    if phase == "connect":
        await asyncio.wait_for(factory.connect_started.wait(), 3)
    else:
        await factory.started()
    await asyncio.wait_for(app.state.service.close(), 3)
    run = await app.state.storage.get_run(handle["run_id"])
    task = await app.state.storage.get_owned_task(handle["task_id"], parent["id"])
    assert run["status"] == ("interrupted" if phase == "connect" else "canceled")
    assert task["status"] == ("failed" if phase == "connect" else "canceled")
    assert all(agent.closed for agent in factory.clients)
    assert not app.state.service.tokens and not app.state.service.worker_tasks and not app.state.service.worker_waiters
    before = database_snapshot(app.state.storage.path)
    with pytest.raises(SessionUnavailableError):
        await app.state.service.delegate_task(parent["id"], TaskCreate(**task_input()))
    assert database_snapshot(app.state.storage.path) == before


@pytest.mark.asyncio
async def test_shutdown_waits_for_admitted_delegation(delegation, monkeypatch):
    _, app, factory, _, parent, _ = delegation
    service, storage = app.state.service, app.state.storage
    entered, release = asyncio.Event(), asyncio.Event()
    create = storage.create_task_with_run

    async def paused_create(parent_session_id, data):
        entered.set()
        await release.wait()
        return await create(parent_session_id, data)

    monkeypatch.setattr(storage, "create_task_with_run", paused_create)
    submitted = asyncio.create_task(service.delegate_task(parent["id"], TaskCreate(**task_input())))
    await asyncio.wait_for(entered.wait(), 3)
    closing = asyncio.create_task(service.close())
    try:
        await asyncio.sleep(0)
        assert not closing.done() and not service.closing
    finally:
        release.set()
    handle = await asyncio.wait_for(submitted, 3)
    await asyncio.wait_for(closing, 3)
    runtime = service.sessions[handle["child_session_id"]]
    assert runtime.dispatcher.done() and runtime.active is None
    assert (await storage.get_run(handle["run_id"]))["status"] == "canceled"
    assert (await storage.get_owned_task(handle["task_id"], parent["id"]))["status"] == "canceled"
    assert all(agent.closed for agent in factory.clients)
