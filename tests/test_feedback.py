"""3.6：Session 邮箱、原子拉取、持久化与权限隔离。"""

import asyncio
import json
import sqlite3
from uuid import UUID, uuid4

import httpx
import pytest

from app.db.storage import Storage
from app.main import create_app
from test_delegation import delegation, delegate, drain, task_input
from test_http_runs import live_server, new_session, submit, terminal
from test_internal_auth import IdentityFactory
from test_permissions import pause_auto_answers, interactions, options
from test_storage import repository


async def feedback(http, session_id, message):
    response = await http.post(f"/api/sessions/{session_id}/feedback", json={"message": message})
    assert response.status_code == 201, response.text
    return response.json()


async def pull(http, auth):
    response = await http.post("/internal/feedback/pull", json={}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()["messages"]


async def mailbox_state(storage):
    async with storage.transaction() as db:
        async with db.execute("SELECT * FROM interactions ORDER BY rowid") as cursor:
            interactions = [dict(row) for row in await cursor.fetchall()]
        async with db.execute("SELECT * FROM events ORDER BY id") as cursor:
            events = [dict(row) for row in await cursor.fetchall()]
    return interactions, events


@pytest.mark.asyncio
async def test_idle_feedback_is_persistent_and_emits_session_events(delegation):
    http, app, factory, project, _, _ = delegation
    session = await new_session(http, project)
    clients = len(factory.clients)
    messages = [" 保留原文 ' \n补充要求 ", "第二条"]
    created = [await feedback(http, session["id"], message) for message in messages]
    for item, message in zip(created, messages):
        UUID(item["id"])
        assert item["session_id"] == session["id"] and item["run_id"] is None
        assert item["kind"] == "feedback" and item["status"] == "pending"
        assert item["payload"] == {"message": message} and item["response"] is None
        assert item["created_at"] == item["updated_at"]
    detail = (await http.get(f"/api/sessions/{session['id']}")).json()
    assert detail["runs"] == [] and len(factory.clients) == clients
    assert [item["id"] for item in detail["interactions"]] == [item["id"] for item in created]
    reopened = Storage(app.state.storage.path)
    await reopened.open()
    try:
        pending = await reopened.list_interactions(session["id"])
        assert {item["id"] for item in pending} == {item["id"] for item in created}
    finally:
        await reopened.close()
    events = (await http.get("/api/events", params={"project_id": project["id"], "session_id": session["id"]})).json()
    assert [event["kind"] for event in events] == ["feedback.created", "feedback.created"]
    assert all(event["run_id"] is None and event["task_id"] is None for event in events)
    assert [event["payload"] for event in events] == [
        {"feedback_id": item["id"], "status": "pending", "message": message}
        for item, message in zip(created, messages)
    ]


@pytest.mark.asyncio
async def test_pull_is_ordered_concurrent_and_scoped_to_caller(delegation):
    http, app, factory, _, parent, auth = delegation
    handle = await delegate(delegation, task_input())
    worker, _ = await factory.started()
    worker_auth = {"Authorization": "Bearer " + worker.token}
    main_notes = [await feedback(http, parent["id"], text) for text in ("主会话一", "主会话二")]
    worker_note = await feedback(http, handle["child_session_id"], "Worker 专属")
    before_cancels = factory.cancels
    batches = await asyncio.gather(*(pull(http, auth) for _ in range(8)))
    nonempty = [batch for batch in batches if batch]
    assert nonempty == [[{key: item[key] for key in ("id", "created_at")} | {"message": item["payload"]["message"]}
                         for item in main_notes]]
    assert await pull(http, auth) == []
    assert (await app.state.storage.get_interaction(worker_note["id"]))["status"] == "pending"
    assert await pull(http, worker_auth) == [{"id": worker_note["id"], "message": "Worker 专属",
                                            "created_at": worker_note["created_at"]}]
    assert await pull(http, worker_auth) == []
    for item in [*main_notes, worker_note]:
        saved = await app.state.storage.get_interaction(item["id"])
        assert saved["status"] == "pulled" and saved["updated_at"] >= saved["created_at"]
    assert factory.cancels == before_cancels and not worker.pending.done()
    events = await app.state.storage.list_events(parent["project_id"], None, 0, 1000)
    pulled = [event for event in events if event["kind"] == "feedback.pulled"]
    assert [event["session_id"] for event in pulled] == [parent["id"], parent["id"], handle["child_session_id"]]
    assert [json.loads(event["payload"])["feedback_id"] for event in pulled] == [
        item["id"] for item in [*main_notes, worker_note]
    ]
    assert all(event["task_id"] is None and event["run_id"] is None for event in pulled)
    worker.finish()
    await drain(app, handle)


@pytest.mark.asyncio
async def test_pending_feedback_survives_turns_and_connection_close(delegation):
    http, app, factory, _, parent, auth = delegation
    first = await submit(http, parent, "第一轮")
    main, _ = await factory.started()
    note = await feedback(http, parent["id"], "后续仍需处理")
    main.finish()
    await terminal(http, first["run_id"])
    second = await submit(http, parent, "第二轮")
    reused, _ = await factory.started()
    assert reused is main
    assert await pull(http, auth) == [{"id": note["id"], "message": "后续仍需处理", "created_at": note["created_at"]}]
    retained = await feedback(http, parent["id"], "关闭连接也保留")
    main.finish()
    await terminal(http, second["run_id"])
    await main.close()
    assert (await http.post("/internal/feedback/pull", json={}, headers=auth)).status_code == 401
    saved = await app.state.storage.get_interaction(retained["id"])
    assert saved["status"] == "pending" and saved["run_id"] is None


@pytest.mark.asyncio
async def test_backend_restart_preserves_pending_and_pulled_without_restoring_credentials(delegation):
    http, app, _, _, parent, auth = delegation
    consumed = await feedback(http, parent["id"], "已经拉取")
    await pull(http, auth)
    pending = await feedback(http, parent["id"], "重启后仍在")
    await app.state.service.close()
    await app.state.storage.close()
    restarted = create_app(app.state.storage.path, IdentityFactory())
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(restarted), base_url="http://test") as client:
            assert (await client.post("/internal/feedback/pull", json={}, headers=auth)).status_code == 401
            assert restarted.state.service.tokens == {} and restarted.state.service.sessions == {}
            storage = restarted.state.storage
            assert (await storage.get_interaction(consumed["id"]))["status"] == "pulled"
            assert (await storage.get_interaction(pending["id"]))["status"] == "pending"
            assert await storage.pull_feedback(parent["id"]) == [{
                "id": pending["id"], "message": "重启后仍在", "created_at": pending["created_at"],
            }]
            assert await storage.pull_feedback(parent["id"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "pull"])
async def test_feedback_event_failure_rolls_back_entire_operation(delegation, operation):
    http, app, _, _, parent, auth = delegation
    notes = [await feedback(http, parent["id"], text) for text in ("第一条", "第二条")]
    before = await mailbox_state(app.state.storage)
    kind = "feedback.created" if operation == "create" else "feedback.pulled"
    condition = "" if operation == "create" else " AND (SELECT count(*) FROM events WHERE kind='feedback.pulled')=1"
    async with app.state.storage.transaction() as db:
        await db.execute(
            f"CREATE TRIGGER reject_feedback BEFORE INSERT ON events WHEN NEW.kind='{kind}'{condition} "
            "BEGIN SELECT RAISE(ABORT, 'feedback write failed'); END",
        )
    with pytest.raises(sqlite3.IntegrityError, match="feedback write failed"):
        if operation == "create":
            await feedback(http, parent["id"], "应完整回滚")
        else:
            await pull(http, auth)
    assert await mailbox_state(app.state.storage) == before
    async with app.state.storage.transaction() as db:
        await db.execute("DROP TRIGGER reject_feedback")
    if operation == "create":
        recovered = await feedback(http, parent["id"], "移除故障后可提交")
        assert recovered["status"] == "pending"
    else:
        assert [item["id"] for item in await pull(http, auth)] == [item["id"] for item in notes]


@pytest.mark.asyncio
async def test_feedback_pull_and_permission_answer_are_independent(delegation, monkeypatch):
    http, app, factory, _, parent, auth = delegation
    pause_auto_answers(app.state.service, monkeypatch)
    main_run = await submit(http, parent, "权限等待")
    main, _ = await factory.started()
    callback, _ = main.permission(options())
    permission, = await interactions(http, parent, 1)
    note = await feedback(http, parent["id"], "补充要求不等于授权")
    assert (await http.post(f"/api/interactions/{note['id']}/answer", json={
        "option_id": options()[0].option_id,
    })).status_code == 409
    assert [item["id"] for item in await pull(http, auth)] == [note["id"]]
    assert not callback.done()
    assert (await app.state.storage.get_run(main_run["run_id"]))["status"] == "waiting_permission"
    assert (await app.state.storage.get_interaction(permission["id"]))["status"] == "pending"
    retained = await feedback(http, parent["id"], "授权后依然待拉取")
    response = await http.post(f"/api/interactions/{permission['id']}/answer", json={
        "option_id": options()[0].option_id,
    })
    assert response.status_code == 200
    assert (await callback).outcome.option_id == options()[0].option_id
    assert (await app.state.storage.get_run(main_run["run_id"]))["status"] == "running"
    assert (await app.state.storage.get_interaction(retained["id"]))["status"] == "pending"
    await http.post(f"/api/runs/{main_run['run_id']}/cancel")
    assert (await app.state.storage.get_interaction(retained["id"]))["status"] == "pending"
    assert [item["id"] for item in await pull(http, auth)] == [retained["id"]]


@pytest.mark.asyncio
async def test_feedback_validation_and_invalid_pull_never_mutate_mailbox(delegation):
    http, app, _, _, parent, auth = delegation
    note = await feedback(http, parent["id"], "应保留")
    before = await mailbox_state(app.state.storage)
    assert (await http.post(f"/api/sessions/{uuid4()}/feedback", json={"message": "不存在"})).status_code == 404
    assert (await http.post("/api/sessions/invalid/feedback", json={"message": "无效 UUID"})).status_code == 422
    for body in ({"message": " \n"}, {"message": 12}, {"message": "额外字段", "session_id": parent["id"]}):
        assert (await http.post(f"/api/sessions/{parent['id']}/feedback", json=body)).status_code == 422
    assert (await http.post("/internal/feedback/pull", headers=auth, json={"session_id": parent["id"]})).status_code == 422
    for headers in ({}, {"Authorization": "Bearer unknown"}):
        assert (await http.post("/internal/feedback/pull", headers=headers, json={})).status_code == 401
    assert await mailbox_state(app.state.storage) == before
    assert [item["id"] for item in await pull(http, auth)] == [note["id"]]


@pytest.mark.asyncio
async def test_feedback_history_and_sse_replay_share_persisted_events(delegation):
    http, app, _, project, parent, auth = delegation
    note = await feedback(http, parent["id"], "SSE 可恢复反馈状态")
    await pull(http, auth)
    params = {"project_id": project["id"], "session_id": parent["id"], "limit": 100}
    history = (await http.get("/api/events", params=params)).json()
    expected = [event for event in history if event["kind"].startswith("feedback.")]
    assert [event["kind"] for event in expected] == ["feedback.created", "feedback.pulled"]
    assert all(event["payload"]["feedback_id"] == note["id"] for event in expected)
    async with live_server(app) as client:
        async with client.stream("GET", "/api/stream", params={
            **params, "after_event_id": expected[0]["id"],
        }) as stream:
            async for line in stream.aiter_lines():
                if line.startswith("data: "):
                    assert json.loads(line.removeprefix("data: ")) == expected[1]
                    break
