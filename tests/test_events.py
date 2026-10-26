"""2.5：数据库历史分页与真实 TCP SSE 补拉。"""

import asyncio
import json
from uuid import uuid4
from types import SimpleNamespace

import pytest

from app.domain.models import MessageCreate
from app.api.routes import StreamSettings, stream_events
from test_http_runs import (
    api, http_records, live_server, new_session, repository, setup_app, submit, terminal,
)


async def seed(storage, session, count):
    run, _ = await storage.create_run(session["id"], MessageCreate(message="预置", request_id=str(uuid4())))
    rows = await storage.list_events(session["project_id"], session["id"], 0, 1000)
    cursor = rows[-1]["id"]
    events = []
    for index in range(count):
        events.append(await storage.append_event(run["id"], "agent.text", {
            "text": f"第{index}条\n中文", "channel": "message",
        }))
    return run, cursor, events


def params(project, session=None, cursor=0, limit=100):
    result = {"project_id": project["id"], "after_event_id": cursor, "limit": limit}
    if session is not None:
        result["session_id"] = session["id"]
    return result


async def next_frame(lines):
    frame = []
    async with asyncio.timeout(3):
        async for line in lines:
            frame.append(line)
            if line == "":
                return "\n".join(frame) + "\n"
    raise AssertionError("SSE 提前结束")


async def next_event(lines):
    while True:
        frame = await next_frame(lines)
        if frame.startswith(":"):
            continue
        pieces = frame.splitlines()
        assert len(pieces) == 4 and pieces[1] == "event: lab" and pieces[-1] == ""
        assert pieces[0].startswith("id: ") and pieces[2].startswith("data: ")
        data = json.loads(pieces[2][6:])
        assert int(pieces[0][4:]) == data["id"]
        return data, frame


@pytest.mark.asyncio
async def test_history_pages_five_events_with_filters_and_json_payload(api, http_records):
    client, app, _ = api
    project, session = http_records
    storage = app.state.storage
    other_session = await new_session(client, project)
    run, cursor, first = await seed(storage, session, 1)
    other, _, _ = await seed(storage, other_session, 2)
    target = first[:]
    for i in range(4):
        target.append(await storage.append_event(run["id"], "agent.text", {"text": str(i), "channel": "message"}))
        await storage.append_event(other["id"], "agent.text", {"text": "别的会话", "channel": "message"})
    # 第二个项目事件和目标会话的事件 ID 交错；组合过滤不能泄露它。
    foreign = str(uuid4())
    async with storage.transaction() as db:
        await db.execute("INSERT INTO projects VALUES (?,?,?,?,?)", (foreign, "其他项目", "D:/test-other", "[]", "now"))
        await db.execute("INSERT INTO events (project_id,kind,payload,created_at) VALUES (?,?,?,?)",
                         (foreign, "raw", "{}", "now"))
    collected = []
    while True:
        response = await client.get("/api/events", params=params(project, session, cursor, 2))
        assert response.status_code == 200
        batch = response.json()
        if not batch:
            break
        assert len(batch) <= 2 and all(isinstance(row["payload"], dict) for row in batch)
        collected.extend(batch)
        cursor = batch[-1]["id"]
    assert [row["id"] for row in collected] == [row["id"] for row in target]
    assert collected[0]["payload"]["text"] == "第0条\n中文"
    project_rows = (await client.get("/api/events", params=params(project))).json()
    assert {row["session_id"] for row in project_rows} == {session["id"], other_session["id"]}
    assert (await client.get("/api/events", params=params({"id": foreign}, session))).json() == []


@pytest.mark.asyncio
async def test_sse_disconnect_append_reconnect_with_cursor_and_exact_frames(api, http_records):
    _, app, _ = api
    project, session = http_records
    storage = app.state.storage
    run, cursor, events = await seed(storage, session, 2)
    async with live_server(app) as client:
        async with client.stream("GET", "/api/stream", params=params(project, session, cursor, 1)) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = response.aiter_lines()
            received = [await next_event(lines), await next_event(lines)]
            assert [row["id"] for row, _ in received] == [event["id"] for event in events]
            assert all(frame.endswith("\n\n") for _, frame in received)
            assert "第0条\\n中文" in received[0][1]
        appended = [await storage.append_event(run["id"], "raw", {"value": i}) for i in range(3)]
        async with client.stream("GET", "/api/stream", params=params(project, session, cursor),
                                 headers={"Last-Event-ID": str(events[-1]["id"])}) as response:
            lines = response.aiter_lines()
            replay = [(await next_event(lines))[0] for _ in range(3)]
            assert [row["id"] for row in replay] == [row["id"] for row in appended]
            history = (await client.get("/api/events", params=params(project, session, events[-1]["id"]))).json()
            assert replay == history


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, "0", "bad", "-1", "1.5", ""])
async def test_header_invalid_or_smaller_never_overrides_explicit_cursor(api, http_records, header):
    _, app, _ = api
    project, session = http_records
    _, _, events = await seed(app.state.storage, session, 3)
    async with live_server(app) as client:
        headers = {} if header is None else {"Last-Event-ID": header}
        async with client.stream("GET", "/api/stream", params=params(project, session, events[1]["id"]), headers=headers) as response:
            event, _ = await next_event(response.aiter_lines())
            assert event["id"] == events[2]["id"]


@pytest.mark.asyncio
async def test_project_and_session_filter_heartbeat_and_large_cursor(api, http_records):
    client, app, _ = api
    project, session = http_records
    other = await new_session(client, project)
    await seed(app.state.storage, other, 2)
    assert (await client.get("/api/events", params=params(project, session))).json() == []
    async with live_server(app) as client:
        async with client.stream("GET", "/api/stream", params=params(project, session)) as response:
            assert await next_frame(response.aiter_lines()) == ": ping\n\n"
        # 超过 SQLite 整数范围的游标仍表示“没有更新”，不能在响应头之后崩溃。
        async with client.stream("GET", "/api/stream", params=params(project), headers={"Last-Event-ID": str(2**64)}) as response:
            assert await next_frame(response.aiter_lines()) == ": ping\n\n"


@pytest.mark.asyncio
async def test_history_subscription_gap_and_sse_close_do_not_cancel_run(api, http_records):
    _, app, factory = api
    project, session = http_records
    async with live_server(app) as client:
        run = await submit(client, session, "流断开后继续")
        agent, _ = await factory.started()
        history = (await client.get("/api/events", params=params(project, session))).json()
        cursor = history[-1]["id"]
        agent.emit("历史与订阅间隙")
        async with client.stream("GET", "/api/stream", params=params(project, session, cursor)) as response:
            event, _ = await next_event(response.aiter_lines())
            assert event["payload"]["text"] == "历史与订阅间隙"
        assert factory.cancels == 0 and not agent.pending.done()
        agent.finish("继续完成")
        final = await terminal(client, run["run_id"])
        assert final["status"] == "completed" and final["result"] == "历史与订阅间隙继续完成"


@pytest.mark.asyncio
async def test_sse_commit_between_empty_query_and_wait_is_not_lost(api, http_records, monkeypatch):
    _, app, _ = api
    project, session = http_records
    storage = app.state.storage
    run, cursor, _ = await seed(storage, session, 0)
    original = storage.list_events
    injected = False

    async def racing_query(*args):
        nonlocal injected
        rows = await original(*args)
        if not rows and not injected:
            injected = True
            await storage.append_event(run["id"], "raw", {"gap": True})
        return rows

    monkeypatch.setattr(storage, "list_events", racing_query)
    async with live_server(app) as client:
        async with client.stream("GET", "/api/stream", params=params(project, session, cursor)) as response:
            event, _ = await next_event(response.aiter_lines())
            assert event["payload"] == {"gap": True}


@pytest.mark.asyncio
async def test_idle_sse_waits_for_notification_instead_of_polling_database(api, http_records, monkeypatch):
    _, app, _ = api
    project, session = http_records
    entered = asyncio.Event()
    queries = 0

    async def empty_query(*args):
        nonlocal queries
        queries += 1
        entered.set()
        return []

    async def connected():
        return False

    monkeypatch.setattr(app.state.storage, "list_events", empty_query)
    request = SimpleNamespace(app=app, is_disconnected=connected)
    stream = stream_events(request, project["id"], session["id"], 0, 100, StreamSettings(0.01, 30))
    pending = asyncio.create_task(anext(stream))
    try:
        await entered.wait()
        await asyncio.sleep(0.05)
        assert queries == 1
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()


@pytest.mark.asyncio
async def test_rollback_hidden_then_reopen_replays_committed_history(api, http_records):
    _, app, _ = api
    project, session = http_records
    storage = app.state.storage
    run, cursor, expected = await seed(storage, session, 1)
    async with live_server(app) as client:
        transaction_entered, rollback = asyncio.Event(), asyncio.Event()

        async def uncommitted():
            with pytest.raises(RuntimeError, match="回滚"):
                async with storage.transaction() as db:
                    await db.execute(
                        "INSERT INTO events (project_id,session_id,run_id,kind,payload,created_at) VALUES (?,?,?,?,?,?)",
                        (project["id"], session["id"], run["id"], "raw", '{"uncommitted":true}', "now"),
                    )
                    transaction_entered.set()
                    await rollback.wait()
                    raise RuntimeError("回滚")

        writer = asyncio.create_task(uncommitted())
        await transaction_entered.wait()
        reading = asyncio.create_task(client.get("/api/events", params=params(project, session, cursor)))
        try:
            response = await asyncio.wait_for(asyncio.shield(reading), 1)
            assert [row["id"] for row in response.json()] == [expected[0]["id"]]
        finally:
            rollback.set()
            await writer
        await storage.close()
        await storage.open()
        async with client.stream("GET", "/api/stream", params=params(project, session, cursor)) as response:
            row, _ = await next_event(response.aiter_lines())
            assert row["id"] == expected[0]["id"] and "uncommitted" not in row["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/events", "/api/stream"])
@pytest.mark.parametrize("overrides", [{"after_event_id": -1}, {"limit": 0}, {"limit": 1001}, {"session_id": "bad"}])
async def test_query_validation_before_stream_headers(api, http_records, path, overrides):
    client, _, _ = api
    project, session = http_records
    response = await client.get(path, params={**params(project, session), **overrides})
    assert response.status_code == 422
