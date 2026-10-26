"""仅使用临时库、Git 目录及 ACP 替身验证会话物理删除。"""

import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest
from acp.schema import PermissionOption, ToolCallUpdate

from app.domain.models import FeedbackCreate, MessageCreate, TaskCreate
from app.services.orchestrator import SessionUnavailableError
from test_http_runs import api, http_records, live_server, new_session, repository, setup_app, submit, terminal
from test_events import next_event, params


def task_input():
    return TaskCreate(harness="codex", goal="只读核对", acceptance="返回结论", request_id=str(uuid4()))


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex", "opencode", "codebuddy", "deepseek"])
async def test_delete_finished_session_removes_all_application_records(api, http_records, harness):
    client, app, factory = api
    project, _ = http_records
    session = (await client.post("/api/sessions", json={"project_id": project["id"], "harness": harness})).json()
    run = await submit(client, session, "需要删除的正文")
    agent, _ = await factory.started()
    await app.state.storage.append_event(run["run_id"], "agent.tool", {
        "toolCallId": "large", "rawOutput": "待删除的输出" * 20000})
    await app.state.storage.create_feedback(session["id"], FeedbackCreate(message="待删除反馈"))
    agent.finish()
    await terminal(client, run["run_id"])
    token = app.state.service.sessions[session["id"]].mcp_token
    assert await app.state.storage._fetch_all("SELECT * FROM event_details", ())
    response = await client.delete(f"/api/sessions/{session['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["deleted_session_ids"] == [session["id"]]
    assert agent.closed and agent.proc.returncode is not None
    assert token not in app.state.service.tokens and session["id"] not in app.state.service.sessions
    for table, column, value in [("sessions", "id", session["id"]), ("runs", "session_id", session["id"]),
                                 ("events", "session_id", session["id"]), ("interactions", "session_id", session["id"])]:
        assert not await app.state.storage._fetch_all(f"SELECT * FROM {table} WHERE {column}=?", (value,))
    assert not await app.state.storage._fetch_all("SELECT * FROM event_details", ())
    for url in [f"/api/sessions/{session['id']}", f"/api/sessions/{session['id']}/history", f"/api/runs/{run['run_id']}"]:
        assert (await client.get(url)).status_code == 404
    assert (await client.delete(f"/api/sessions/{session['id']}")).status_code == 404
    assert (await client.get("/api/projects")).json()[0]["id"] == project["id"]
    assert not await app.state.storage._fetch_all("PRAGMA foreign_key_check", ())


@pytest.mark.asyncio
async def test_parent_deletion_stops_entire_tree_but_other_session_keeps_running(api, http_records, repository):
    client, app, factory = api
    project, parent = http_records
    marker = repository / "keep.txt"
    marker.write_text("保留项目文件", encoding="utf-8")
    parent_run = await submit(client, parent, "父会话运行")
    parent_agent, _ = await factory.started()
    task = await app.state.service.delegate_task(parent["id"], task_input())
    worker, _ = await factory.started()
    grandchild = str(uuid4())
    async with app.state.storage.transaction() as db:
        await db.execute("INSERT INTO sessions(id,project_id,parent_id,harness,cwd,created_at) VALUES (?,?,?,'codex',?,'now')",
                         (grandchild, project["id"], task["child_session_id"], str(repository)))
    other = await new_session(client, project)
    other_run = await submit(client, other, "继续运行")
    other_agent, _ = await factory.started()
    response = await client.delete(f"/api/sessions/{parent['id']}")
    assert response.status_code == 200, response.text
    assert set(response.json()["deleted_session_ids"]) == {parent["id"], task["child_session_id"], grandchild}
    assert parent_agent.closed and worker.closed and not other_agent.closed
    assert (await client.get(f"/api/runs/{parent_run['run_id']}")).status_code == 404
    assert (await client.get(f"/api/tasks/{task['task_id']}/diff")).status_code == 404
    assert set(row["id"] for row in (await client.get("/api/sessions")).json()) == {other["id"]}
    assert marker.read_text(encoding="utf-8") == "保留项目文件"
    other_agent.emit("删除期间仍可推送")
    other_agent.finish()
    assert (await terminal(client, other_run["run_id"]))["status"] == "completed"
    assert not app.state.service.worker_tasks and not app.state.service.worker_waiters
    assert not await app.state.storage._fetch_all("PRAGMA foreign_key_check", ())


@pytest.mark.asyncio
async def test_delete_worker_cleans_parent_task_events_and_preserves_sibling(api, http_records):
    client, app, factory = api
    project, parent = http_records
    tasks = []
    for _ in range(2):
        task = await app.state.service.delegate_task(parent["id"], task_input())
        agent, _ = await factory.started()
        agent.finish()
        await terminal(client, task["run_id"])
        tasks.append(task)
    target, sibling = tasks
    response = await client.delete(f"/api/sessions/{target['child_session_id']}")
    assert response.status_code == 200, response.text
    assert await app.state.storage.get_session(parent["id"])
    assert await app.state.storage.get_task(sibling["task_id"])
    rows = (await client.get("/api/events", params={"project_id": project["id"], "session_id": parent["id"]})).json()
    assert not any(row["task_id"] == target["task_id"] or row["run_id"] == target["run_id"] for row in rows)
    assert any(row["task_id"] == sibling["task_id"] for row in rows)


@pytest.mark.asyncio
async def test_delete_during_connection_and_queued_input_never_starts_prompt(api, http_records):
    client, app, factory = api
    _, session = http_records
    factory.connect_release.clear()
    await submit(client, session, "还在连接")
    await factory.connect_started.wait()
    await submit(client, session, "排队输入")
    response = await asyncio.wait_for(client.delete(f"/api/sessions/{session['id']}"), 3)
    assert response.status_code == 200, response.text
    assert factory.starts.empty() and factory.clients[0].closed
    assert not await app.state.storage.list_runs(session["id"])


@pytest.mark.asyncio
async def test_delete_permission_wait_expires_and_old_answer_is_not_accepted(api, http_records):
    client, app, factory = api
    _, session = http_records
    await submit(client, session, "等待授权")
    agent, _ = await factory.started()
    app.state.service.sessions[session["id"]].automatic_approval = False
    permission = asyncio.create_task(agent.host.request_permission("native", ToolCallUpdate(tool_call_id="p"), [
        PermissionOption(option_id="allow", name="允许", kind="allow_once")]))
    async with asyncio.timeout(3):
        while not (pending := await app.state.storage.list_interactions(session["id"])):
            await asyncio.sleep(0.005)
    response = await client.delete(f"/api/sessions/{session['id']}")
    assert response.status_code == 200, response.text
    assert (await permission).outcome.outcome == "cancelled"
    assert not app.state.service.permissions
    assert (await client.post(f"/api/interactions/{pending[0]['id']}/answer", json={"option_id": "allow"})).status_code == 404


@pytest.mark.asyncio
async def test_deleting_rejects_new_input_and_delegation_and_serializes_duplicate_deletes(api, http_records):
    client, app, factory = api
    _, session = http_records
    await submit(client, session, "活动输入")
    await factory.started()
    await submit(client, session, "不能再启动的排队输入")
    factory.cancel_release.clear()
    deletion = asyncio.create_task(client.delete(f"/api/sessions/{session['id']}"))
    await factory.cancel_entered.wait()
    response = await client.post(f"/api/sessions/{session['id']}/messages", json={"message": "不接受", "request_id": str(uuid4())})
    assert response.status_code == 409
    with pytest.raises(SessionUnavailableError):
        await app.state.service.delegate_task(session["id"], task_input())
    duplicate = asyncio.create_task(client.delete(f"/api/sessions/{session['id']}"))
    factory.cancel_release.set()
    assert (await deletion).status_code == 200
    assert (await duplicate).status_code == 404
    assert factory.starts.empty()


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_records_and_allows_delete_retry(api, http_records, monkeypatch):
    client, app, factory = api
    _, session = http_records
    run = await submit(client, session, "已结束")
    agent, _ = await factory.started()
    agent.finish()
    await terminal(client, run["run_id"])
    close = agent.close
    async def fail():
        raise OSError("受控关闭失败")
    monkeypatch.setattr(agent, "close", fail)
    response = await client.delete(f"/api/sessions/{session['id']}")
    assert response.status_code == 409 and "关闭失败" in response.text
    assert await app.state.storage.get_run(run["run_id"])
    monkeypatch.setattr(agent, "close", close)
    assert (await client.delete(f"/api/sessions/{session['id']}")).status_code == 200


@pytest.mark.asyncio
async def test_delete_transaction_rolls_back_and_persists_after_reopen(api, http_records):
    client, app, _ = api
    _, session = http_records
    db = app.state.storage
    run, _ = await db.create_run(session["id"], MessageCreate(message="回滚内容", request_id=str(uuid4())))
    async with db.transaction() as connection:
        await connection.execute("CREATE TRIGGER block_session_delete BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT,'rollback test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await db.delete_sessions([session["id"]])
    assert await db.get_run(run["id"])
    assert await db.list_events(session["project_id"], session["id"], 0, 100)
    async with db.transaction() as connection:
        await connection.execute("DROP TRIGGER block_session_delete")
    assert (await client.delete(f"/api/sessions/{session['id']}")).status_code == 200
    await db.close()
    await db.open()
    assert await db.get_session(session["id"]) is None
    assert await db.get_run(run["id"]) is None
    assert not await db._fetch_all("PRAGMA foreign_key_check", ())


@pytest.mark.asyncio
async def test_deletion_event_reaches_selected_session_sse_and_replays(api, http_records):
    _, app, _ = api
    project, session = http_records
    async with live_server(app) as client:
        async with client.stream("GET", "/api/stream", params=params(project, session)) as response:
            deletion = await client.delete(f"/api/sessions/{session['id']}")
            assert deletion.status_code == 200
            event, _ = await next_event(response.aiter_lines())
            assert event["kind"] == "session.deleted" and event["session_id"] is None
            assert event["payload"] == {"session_ids": [session["id"]]}
        events = (await client.get("/api/events", params=params(project, session))).json()
        assert events == [event]


@pytest.mark.asyncio
async def test_delete_request_disconnect_does_not_interrupt_cleanup(api, http_records):
    _, app, factory = api
    _, session = http_records
    run = await app.state.service.submit(session["id"], MessageCreate(message="删除中断开", request_id=str(uuid4())))
    await factory.started()
    factory.cancel_release.clear()
    caller = asyncio.create_task(app.state.service.delete_session(session["id"]))
    await factory.cancel_entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    jobs = list(app.state.service.deletions)
    factory.cancel_release.set()
    await asyncio.gather(*jobs)
    assert await app.state.storage.get_run(run["id"]) is None


@pytest.mark.asyncio
async def test_submit_waits_for_database_delete_and_cannot_recreate_runtime(api, http_records, monkeypatch):
    client, app, _ = api
    _, session = http_records
    storage = app.state.storage
    removed, release = asyncio.Event(), asyncio.Event()
    delete = storage.delete_sessions

    async def paused_delete(session_ids):
        await delete(session_ids)
        removed.set()
        await release.wait()

    monkeypatch.setattr(storage, "delete_sessions", paused_delete)
    deletion = asyncio.create_task(client.delete(f"/api/sessions/{session['id']}"))
    await removed.wait()
    submission = asyncio.create_task(app.state.service.submit(
        session["id"], MessageCreate(message="并发提交", request_id=str(uuid4()))))
    release.set()
    assert (await deletion).status_code == 200
    with pytest.raises(KeyError):
        await submission
    assert session["id"] not in app.state.service.sessions
    assert not await storage.list_runs(session["id"])


@pytest.mark.asyncio
async def test_runtime_read_is_serialized_with_delete_admission(api, http_records, monkeypatch):
    client, app, _ = api
    _, session = http_records
    service = app.state.service
    read, release, deleting, started = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    get_session, session_tree = app.state.storage.get_session, app.state.storage.session_tree
    delete_session = service._delete_session

    async def paused_read(session_id):
        result = await get_session(session_id)
        read.set()
        await release.wait()
        return result

    async def track_delete(session_id):
        deleting.set()
        return await session_tree(session_id)

    async def start_delete(session_id):
        started.set()
        return await delete_session(session_id)

    monkeypatch.setattr(app.state.storage, "get_session", paused_read)
    monkeypatch.setattr(app.state.storage, "session_tree", track_delete)
    monkeypatch.setattr(service, "_delete_session", start_delete)
    runtime = asyncio.create_task(service._runtime(session["id"]))
    await read.wait()
    deletion = asyncio.create_task(client.delete(f"/api/sessions/{session['id']}"))
    await started.wait()
    assert not deleting.is_set()
    release.set()
    await runtime
    assert (await deletion).status_code == 200
    assert session["id"] not in service.sessions


@pytest.mark.asyncio
async def test_cancel_racing_delete_returns_404(api, http_records, monkeypatch):
    client, app, factory = api
    _, session = http_records
    run = await submit(client, session, "已结束")
    agent, _ = await factory.started()
    agent.finish()
    await terminal(client, run["run_id"])
    read, release = asyncio.Event(), asyncio.Event()
    runtime = app.state.service._runtime

    async def paused_runtime(session_id):
        result = await runtime(session_id)
        read.set()
        await release.wait()
        return result

    monkeypatch.setattr(app.state.service, "_runtime", paused_runtime)
    cancellation = asyncio.create_task(client.post(f"/api/runs/{run['run_id']}/cancel"))
    await read.wait()
    assert (await client.delete(f"/api/sessions/{session['id']}")).status_code == 200
    release.set()
    assert (await cancellation).status_code == 404


@pytest.mark.asyncio
async def test_feedback_racing_delete_returns_404_without_orphan(api, http_records, monkeypatch):
    client, app, _ = api
    _, session = http_records
    entered, release = asyncio.Event(), asyncio.Event()
    create_feedback = app.state.storage.create_feedback

    async def paused_feedback(session_id, data):
        entered.set()
        await release.wait()
        return await create_feedback(session_id, data)

    monkeypatch.setattr(app.state.storage, "create_feedback", paused_feedback)
    feedback = asyncio.create_task(client.post(f"/api/sessions/{session['id']}/feedback", json={"message": "并发反馈"}))
    await entered.wait()
    assert (await client.delete(f"/api/sessions/{session['id']}")).status_code == 200
    release.set()
    assert (await feedback).status_code == 404
    assert not await app.state.storage.list_interactions(session["id"])
