"""2.7：确定性 ACP 回调替身；不将这些结果记为真实 Harness 覆盖。"""

import asyncio
import json
import sqlite3
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from acp.connection import StreamDirection, StreamEvent
from acp.schema import PermissionOption, ToolCallUpdate

from app.main import create_app
from test_http_runs import ControlledClient, ControlledFactory, live_server, new_session, submit, terminal
from test_storage import repository


class PermissionClient(ControlledClient):
    def __init__(self, owner, harness, host, record, observe, expire):
        super().__init__(owner, harness, host, record, observe, expire)
        self.expire = expire
        self.callbacks = []

    async def prompt(self, text):
        try:
            return await super().prompt(text)
        finally:
            await self.expire("prompt_ended")

    def permission(self, options):
        tool = ToolCallUpdate(tool_call_id="native-tool", title="写文件 <script>",
                              raw_input={"path": "probe.txt"}, meta={"tool": "保留"})
        raw = {"jsonrpc": "2.0", "id": f"wire-{len(self.callbacks)}", "method": "session/request_permission",
               "params": {"sessionId": "native", "toolCall": tool.model_dump(mode="json", by_alias=True),
                          "options": [option.model_dump(mode="json", by_alias=True) for option in options],
                          "_meta": {"request": "保留"}, "futureField": {"unknown": True}}}
        self.observe(StreamEvent(StreamDirection.INCOMING, raw))
        callback = asyncio.create_task(self.host.request_permission("native", tool, options, meta={"request": "保留"}))
        self.callbacks.append(callback)
        return callback, raw

    async def cancel(self):
        await self.expire("cancel")
        await super().cancel()

    async def close(self):
        await self.expire("close")
        await super().close()
        await asyncio.gather(*self.callbacks, return_exceptions=True)

    async def disconnect(self):
        self.reusable = False
        self.proc.returncode = 1
        await self.expire("process_exit")
        self.pending.set_exception(ConnectionError("确定性连接关闭"))


class PermissionFactory(ControlledFactory):
    def __call__(self, *args):
        instance = PermissionClient(self, *args)
        self.clients.append(instance)
        return instance


def options():
    return [PermissionOption(option_id=f"native/{kind}/id", name=f"名称 {kind}", kind=kind,
                              meta={"opaque": [1, "原样"]})
            for kind in ("allow_once", "allow_always", "reject_once", "reject_always")]


@pytest_asyncio.fixture
async def permission_app(tmp_path):
    factory = PermissionFactory()
    application = create_app(tmp_path / "permissions.db", factory)
    async with application.router.lifespan_context(application):
        yield application, factory


@pytest_asyncio.fixture
async def permission_api(permission_app, repository):
    application, factory = permission_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(application), base_url="http://test") as client:
        project = (await client.post("/api/projects", json={
            "name": "权限测试", "path": str(repository), "check_argv": ["python", "-V"],
        })).json()
        session = await new_session(client, project)
        yield client, application, factory, project, session


def pause_auto_answers(service, monkeypatch):
    """只在测试中暂停首次自动回答，让 HTTP、取消和过期能确定性地竞争。"""
    original = service.answer_permission
    release = asyncio.Event()
    started = set()

    async def answer(permission_id, option_id):
        if permission_id not in started and permission_id in service.permissions:
            started.add(permission_id)
            future = service.permissions[permission_id].future
            waiting = asyncio.create_task(release.wait())
            try:
                await asyncio.wait([future, waiting], return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            if future.done():
                return await service.storage.get_interaction(permission_id)
        return await original(permission_id, option_id)

    monkeypatch.setattr(service, "answer_permission", answer)
    return release


@pytest_asyncio.fixture
async def paused_permission_api(permission_api, monkeypatch):
    pause_auto_answers(permission_api[1].state.service, monkeypatch)
    yield permission_api


async def interactions(client, session, count):
    async with asyncio.timeout(3):
        while True:
            detail = (await client.get(f"/api/sessions/{session['id']}")).json()
            if len(detail["interactions"]) == count:
                return detail["interactions"]
            await asyncio.sleep(0.005)


async def start(permission_api):
    client, application, factory, project, session = permission_api
    run = await submit(client, session, "确定性权限回调")
    agent, _ = await factory.started()
    return run["run_id"], agent


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", range(4))
async def test_paused_approval_preserves_options_and_http_answer(paused_permission_api, selected):
    client, app, _, project, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    callback, raw = agent.permission(options())
    interaction, = await interactions(client, session, 1)
    permission_id = interaction["id"]
    UUID(permission_id)
    assert permission_id not in {raw["id"], "native-tool"}
    assert not callback.done()
    future = app.state.service.permissions[permission_id].future
    assert not future.done() and not app.state.storage._connection().in_transaction
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "waiting_permission"
    payload = json.loads(interaction["payload"])
    assert payload["options"] == raw["params"]["options"]
    assert payload["tool_call"] == raw["params"]["toolCall"]
    assert payload["meta"] == {"meta": {"request": "保留"}}
    # 新 HTTP 客户端模拟刷新查询，仍恢复原 ID，不新建 responder。
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as refreshed:
        assert (await refreshed.get(f"/api/sessions/{session['id']}")).json()["interactions"] == [interaction]
        answer = await refreshed.post(f"/api/interactions/{permission_id}/answer",
                                      json={"option_id": options()[selected].option_id})
    assert answer.status_code == 200
    assert answer.json()["status"] == "answered"
    assert json.loads(answer.json()["response"]) == {"option_id": options()[selected].option_id}
    response = await asyncio.wait_for(callback, 3)
    assert response.outcome.outcome == "selected"
    assert response.outcome.option_id == options()[selected].option_id
    assert future.done() and permission_id not in app.state.service.permissions
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "running"
    assert (await client.get(f"/api/sessions/{session['id']}")).json()["interactions"] == []
    assert (await client.post(f"/api/interactions/{permission_id}/answer",
                              json={"option_id": options()[selected].option_id})).status_code == 409
    agent.finish("权限之后继续")
    assert (await terminal(client, run_id))["result"] == "权限之后继续"
    rows = (await client.get("/api/events", params={"project_id": project["id"]})).json()
    assert len([row for row in rows if row["kind"] == "permission.requested"]) == 1
    assert next(row for row in rows if row["kind"] == "raw")["payload"]["request_id"] == raw["id"]
    assert next(row for row in rows if row["kind"] == "permission.requested")["payload"]["permission_id"] == permission_id


@pytest.mark.asyncio
async def test_independent_permissions_and_other_session_progress(paused_permission_api):
    client, app, factory, project, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    first, _ = agent.permission(options())
    second, _ = agent.permission(options())
    pending = await interactions(client, session, 2)
    assert pending[0]["id"] != pending[1]["id"]
    other_session = await new_session(client, project)
    other_run = await submit(client, other_session, "另一个会话")
    other, _ = await factory.started()
    other.finish("不受权限等待阻塞")
    assert (await terminal(client, other_run["run_id"]))["result"] == "不受权限等待阻塞"
    for index, permission in enumerate(pending):
        answer = await client.post(f"/api/interactions/{permission['id']}/answer",
                                   json={"option_id": options()[index].option_id})
        assert answer.status_code == 200
        expected = "waiting_permission" if index == 0 else "running"
        assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == expected
    assert all(response.outcome.outcome == "selected" for response in await asyncio.gather(first, second))
    assert app.state.service.permissions == {}
    agent.finish()
    await terminal(client, run_id)


@pytest.mark.asyncio
async def test_invalid_and_foreign_option_leave_future_pending(paused_permission_api):
    client, _, _, _, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    callback, _ = agent.permission(options())
    permission, = await interactions(client, session, 1)
    endpoint = f"/api/interactions/{permission['id']}/answer"
    for body in ({"option_id": "allow"}, {"option_id": " "}, {"option_id": True}, {},
                 {"option_id": options()[0].option_id, "allowed": True}):
        assert (await client.post(endpoint, json=body)).status_code == 422
        assert not callback.done()
    assert (await client.post(f"/api/interactions/{uuid4()}/answer", json={"option_id": "x"})).status_code == 404
    assert (await client.post("/api/interactions/not-uuid/answer", json={"option_id": "x"})).status_code == 422
    assert (await client.post(f"/api/runs/{run_id}/cancel")).json()["status"] == "canceled"
    assert (await callback).outcome.outcome == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "close", "process_exit", "prompt_ended", "shutdown"])
async def test_all_lifecycle_exits_expire_every_permission(paused_permission_api, ending):
    client, app, _, project, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    callbacks = [agent.permission(options())[0] for _ in range(2)]
    pending = await interactions(client, session, 2)
    if ending == "cancel":
        await client.post(f"/api/runs/{run_id}/cancel")
    elif ending == "process_exit":
        await agent.disconnect()
    elif ending == "close":
        await agent.close()
    elif ending == "shutdown":
        await app.state.service.close()
    else:
        agent.finish()
    await terminal(client, run_id)
    assert all(result.outcome.outcome == "cancelled" for result in await asyncio.gather(*callbacks))
    assert app.state.service.permissions == {}
    assert await interactions(client, session, 0) == []
    for permission in pending:
        assert (await app.state.storage.get_interaction(permission["id"]))["status"] == "expired"
        assert (await client.post(f"/api/interactions/{permission['id']}/answer",
                                  json={"option_id": options()[0].option_id})).status_code == 409
    events = await app.state.storage.list_events(project["id"], session["id"], 0, 100)
    assert len([row for row in events if row["kind"] == "permission.expired"]) == 2


@pytest.mark.asyncio
async def test_callback_cancellation_only_expires_its_own_request(paused_permission_api):
    client, app, _, _, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    first, _ = agent.permission(options())
    await interactions(client, session, 1)
    second, _ = agent.permission(options())
    pending = await interactions(client, session, 2)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    remaining, = await interactions(client, session, 1)
    assert remaining["id"] == pending[1]["id"] and not second.done()
    assert len(app.state.service.permissions) == 1
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "waiting_permission"
    await client.post(f"/api/interactions/{remaining['id']}/answer", json={"option_id": options()[0].option_id})
    await second
    agent.finish()
    await terminal(client, run_id)


@pytest.mark.asyncio
async def test_cancel_rejects_answer_before_agent_cancel_finishes(paused_permission_api):
    client, app, factory, _, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    callback, _ = agent.permission(options())
    permission, = await interactions(client, session, 1)
    factory.cancel_release.clear()
    cancellation = asyncio.create_task(client.post(f"/api/runs/{run_id}/cancel"))
    await asyncio.wait_for(factory.cancel_entered.wait(), 3)
    try:
        assert (await client.post(f"/api/interactions/{permission['id']}/answer",
                                  json={"option_id": options()[0].option_id})).status_code == 409
        assert (await callback).outcome.outcome == "cancelled"
        assert app.state.service.permissions == {}
    finally:
        factory.cancel_release.set()
        await cancellation


async def reject(storage, kind):
    async with storage.transaction() as db:
        await db.execute("CREATE TEMP TRIGGER reject_permission BEFORE INSERT ON events "
                         f"WHEN NEW.kind='{kind}' BEGIN SELECT RAISE(ABORT, 'permission failure'); END")


@pytest.mark.asyncio
async def test_create_failure_rolls_back_and_leaks_no_future(paused_permission_api):
    client, app, _, _, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    await reject(app.state.storage, "permission.requested")
    callback, _ = agent.permission(options())
    with pytest.raises(sqlite3.IntegrityError, match="permission failure"):
        await callback
    assert app.state.service.permissions == {}
    assert await interactions(client, session, 0) == []
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "running"
    agent.finish()
    await terminal(client, run_id)


@pytest.mark.asyncio
async def test_callback_cancelled_after_create_commit_expires_fact(paused_permission_api, monkeypatch):
    client, app, _, _, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    committed = asyncio.Event()
    release = asyncio.Event()
    original = app.state.storage.create_permission

    async def create(*args):
        await original(*args)
        committed.set()
        await release.wait()

    monkeypatch.setattr(app.state.storage, "create_permission", create)
    callback, _ = agent.permission(options())
    await asyncio.wait_for(committed.wait(), 3)
    permission, = await interactions(client, session, 1)
    callback.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await callback
    assert app.state.service.permissions == {}
    assert (await app.state.storage.get_interaction(permission["id"]))["status"] == "expired"
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "running"
    agent.finish()
    await terminal(client, run_id)


@pytest.mark.asyncio
async def test_answer_failure_rolls_back_and_can_retry(paused_permission_api):
    client, app, _, _, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    callback, _ = agent.permission(options())
    permission, = await interactions(client, session, 1)
    await reject(app.state.storage, "permission.answered")
    endpoint = f"/api/interactions/{permission['id']}/answer"
    with pytest.raises(sqlite3.IntegrityError, match="permission failure"):
        await client.post(endpoint, json={"option_id": options()[0].option_id})
    assert not callback.done()
    assert (await app.state.storage.get_interaction(permission["id"]))["status"] == "pending"
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "waiting_permission"
    async with app.state.storage.transaction() as db:
        await db.execute("DROP TRIGGER reject_permission")
    assert (await client.post(endpoint, json={"option_id": options()[0].option_id})).status_code == 200
    await callback
    agent.finish()
    await terminal(client, run_id)


@pytest.mark.asyncio
async def test_answer_request_disconnected_after_commit_still_resolves_acp(paused_permission_api, monkeypatch):
    client, app, _, _, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    callback, _ = agent.permission(options())
    permission, = await interactions(client, session, 1)
    committed = asyncio.Event()
    release = asyncio.Event()
    original = app.state.storage.answer_permission

    async def answer(*args):
        await original(*args)
        committed.set()
        await release.wait()

    monkeypatch.setattr(app.state.storage, "answer_permission", answer)
    request = asyncio.create_task(client.post(f"/api/interactions/{permission['id']}/answer",
                                              json={"option_id": options()[0].option_id}))
    await asyncio.wait_for(committed.wait(), 3)
    request.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert (await asyncio.wait_for(callback, 3)).outcome.option_id == options()[0].option_id
    assert app.state.service.permissions == {}
    assert (await app.state.storage.get_interaction(permission["id"]))["status"] == "answered"
    agent.finish()
    await terminal(client, run_id)


@pytest.mark.asyncio
async def test_concurrent_answers_only_complete_future_once(paused_permission_api):
    client, app, _, _, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    callback, _ = agent.permission(options())
    permission, = await interactions(client, session, 1)
    endpoint = f"/api/interactions/{permission['id']}/answer"
    results = await asyncio.gather(*(client.post(endpoint, json={"option_id": option.option_id})
                                     for option in options()[:2]))
    assert sorted(response.status_code for response in results) == [200, 409]
    selected = (await callback).outcome.option_id
    fact = await app.state.storage.get_interaction(permission["id"])
    assert json.loads(fact["response"])["option_id"] == selected
    agent.finish()
    await terminal(client, run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_answer_and_cancel_race_match_persisted_outcome(paused_permission_api, cancel_first):
    client, app, _, _, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    callback, _ = agent.permission(options())
    permission, = await interactions(client, session, 1)
    answer = client.post(f"/api/interactions/{permission['id']}/answer", json={"option_id": options()[0].option_id})
    cancel = client.post(f"/api/runs/{run_id}/cancel")
    responses = await asyncio.gather(*([cancel, answer] if cancel_first else [answer, cancel]))
    answer_response = responses[1 if cancel_first else 0]
    assert answer_response.status_code in {200, 409}
    response = await asyncio.wait_for(callback, 3)
    fact = await app.state.storage.get_interaction(permission["id"])
    if answer_response.status_code == 200:
        assert fact["status"] == "answered" and response.outcome.option_id == options()[0].option_id
    else:
        assert fact["status"] == "expired" and response.outcome.outcome == "cancelled"
    assert app.state.service.permissions == {}
    assert (await terminal(client, run_id))["status"] == "canceled"


@pytest.mark.asyncio
async def test_missing_responder_is_conflict_and_does_not_answer(paused_permission_api):
    client, app, _, _, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    callback, _ = agent.permission(options())
    permission, = await interactions(client, session, 1)
    pending = app.state.service.permissions.pop(permission["id"])
    try:
        assert (await client.post(f"/api/interactions/{permission['id']}/answer",
                                  json={"option_id": options()[0].option_id})).status_code == 409
        assert not callback.done()
        assert (await app.state.storage.get_interaction(permission["id"]))["status"] == "pending"
    finally:
        app.state.service.permissions[permission["id"]] = pending
    await client.post(f"/api/runs/{run_id}/cancel")
    assert (await callback).outcome.outcome == "cancelled"


@pytest.mark.asyncio
async def test_sse_disconnect_preserves_pending_and_replays_answer(paused_permission_api):
    client, app, _, project, session = paused_permission_api
    run_id, agent = await start(paused_permission_api)
    callback, _ = agent.permission(options())
    permission, = await interactions(client, session, 1)
    query = {"project_id": project["id"], "session_id": session["id"]}
    async with live_server(app) as browser:
        async with browser.stream("GET", "/api/stream", params=query) as stream:
            async with asyncio.timeout(3):
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        if event["kind"] == "permission.requested":
                            assert event["payload"]["permission_id"] == permission["id"]
                            cursor = event["id"]
                            break
        assert not callback.done()
        assert (await browser.get(f"/api/sessions/{session['id']}")).json()["interactions"] == [permission]
        assert (await browser.post(f"/api/interactions/{permission['id']}/answer",
                                   json={"option_id": options()[2].option_id})).status_code == 200
        async with browser.stream("GET", "/api/stream", params={**query, "after_event_id": cursor}) as stream:
            async with asyncio.timeout(3):
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        if event["kind"] == "permission.answered":
                            assert event["id"] > cursor
                            assert event["payload"]["option_id"] == options()[2].option_id
                            break
    assert (await callback).outcome.option_id == options()[2].option_id
    agent.finish()
    await terminal(client, run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kinds,expected", [
    (["reject_once", "allow_once", "reject_always", "allow_always"], "allow_always"),
    (["allow_always", "allow_once"], "allow_always"),
    (["reject_always", "allow_once"], "allow_once"),
])
async def test_default_auto_approval_records_native_choice_and_replays_history(permission_api, kinds, expected):
    client, app, _, project, session = permission_api
    run_id, agent = await start(permission_api)
    offered = [next(option for option in options() if option.kind == kind) for kind in kinds]
    callback, raw = agent.permission(offered)
    response = await asyncio.wait_for(callback, 3)
    assert response.outcome.option_id == f"native/{expected}/id"
    assert app.state.service.permissions == {}
    assert await interactions(client, session, 0) == []
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "running"
    rows = (await client.get("/api/events", params={"project_id": project["id"]})).json()
    requested, = [row for row in rows if row["kind"] == "permission.requested"]
    answered, = [row for row in rows if row["kind"] == "permission.answered"]
    permission_id = requested["payload"]["permission_id"]
    assert requested["id"] < answered["id"]
    assert requested["payload"]["options"] == raw["params"]["options"]
    assert answered["payload"]["option_id"] == response.outcome.option_id
    fact = await app.state.storage.get_interaction(permission_id)
    assert fact["status"] == "answered" and json.loads(fact["response"])["option_id"] == response.outcome.option_id
    assert (await client.post(f"/api/interactions/{permission_id}/answer", json={
        "option_id": offered[0].option_id,
    })).status_code == 409
    async with live_server(app) as browser:
        async with browser.stream("GET", "/api/stream", params={
            "project_id": project["id"], "session_id": session["id"], "after_event_id": requested["id"],
        }) as stream:
            async with asyncio.timeout(3):
                async for line in stream.aiter_lines():
                    if line.startswith("data: ") and json.loads(line[6:])["kind"] == "permission.answered":
                        assert json.loads(line[6:])["payload"]["option_id"] == response.outcome.option_id
                        break
    agent.finish("无需人工授权")
    assert (await terminal(client, run_id))["result"] == "无需人工授权"


@pytest.mark.asyncio
@pytest.mark.parametrize("offered", [[], options()[2:]])
async def test_auto_approval_without_allow_cancels_and_expires_record(permission_api, offered):
    client, app, _, project, session = permission_api
    run_id, agent = await start(permission_api)
    callback, _ = agent.permission(offered)
    assert (await asyncio.wait_for(callback, 3)).outcome.outcome == "cancelled"
    assert app.state.service.permissions == {}
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "running"
    rows = await app.state.storage.list_events(project["id"], session["id"], 0, 100)
    requested, = [json.loads(row["payload"]) for row in rows if row["kind"] == "permission.requested"]
    assert (await app.state.storage.get_interaction(requested["permission_id"]))["status"] == "expired"
    assert not any(row["kind"] == "permission.answered" for row in rows)
    agent.finish()
    await terminal(client, run_id)


@pytest.mark.asyncio
async def test_concurrent_default_auto_approvals_leave_no_pending_requests(permission_api):
    client, app, _, project, session = permission_api
    run_id, agent = await start(permission_api)
    callbacks = [agent.permission(options())[0] for _ in range(3)]
    responses = await asyncio.wait_for(asyncio.gather(*callbacks), 3)
    assert all(response.outcome.option_id == options()[1].option_id for response in responses)
    assert app.state.service.permissions == {}
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "running"
    rows = await app.state.storage.list_events(project["id"], session["id"], 0, 100)
    assert len([row for row in rows if row["kind"] == "permission.answered"]) == 3
    agent.finish()
    await terminal(client, run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["permission.requested", "permission.answered"])
async def test_default_auto_approval_write_failure_never_returns_allow(permission_api, kind):
    client, app, _, project, session = permission_api
    run_id, agent = await start(permission_api)
    await reject(app.state.storage, kind)
    callback, _ = agent.permission(options())
    with pytest.raises(sqlite3.IntegrityError, match="permission failure"):
        await asyncio.wait_for(callback, 3)
    assert app.state.service.permissions == {}
    assert await interactions(client, session, 0) == []
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "running"
    rows = await app.state.storage.list_events(project["id"], session["id"], 0, 100)
    assert not any(row["kind"] == "permission.answered" for row in rows)
    requested = [json.loads(row["payload"]) for row in rows if row["kind"] == "permission.requested"]
    if kind == "permission.requested":
        assert requested == []
    else:
        assert (await app.state.storage.get_interaction(requested[0]["permission_id"]))["status"] == "expired"
    async with app.state.storage.transaction() as db:
        await db.execute("DROP TRIGGER reject_permission")
    retry, _ = agent.permission(options())
    assert (await asyncio.wait_for(retry, 3)).outcome.option_id == options()[1].option_id
    agent.finish()
    await terminal(client, run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["cancel_requested", "permissions_closed", "closing"])
async def test_default_auto_approval_rechecks_lifecycle_after_request_commit(permission_api, monkeypatch, flag):
    client, app, _, project, session = permission_api
    run_id, agent = await start(permission_api)
    service = app.state.service
    execution = service.sessions[session["id"]].active
    original = app.state.storage.create_permission

    async def create(*args):
        result = await original(*args)
        setattr(service if flag == "closing" else execution, flag, True)
        return result

    monkeypatch.setattr(app.state.storage, "create_permission", create)
    callback, _ = agent.permission(options())
    try:
        assert (await asyncio.wait_for(callback, 3)).outcome.outcome == "cancelled"
        assert service.permissions == {}
        rows = await app.state.storage.list_events(project["id"], session["id"], 0, 100)
        assert any(row["kind"] == "permission.expired" for row in rows)
        assert not any(row["kind"] == "permission.answered" for row in rows)
    finally:
        setattr(service if flag == "closing" else execution, flag, False)
    agent.finish()
    await terminal(client, run_id)
