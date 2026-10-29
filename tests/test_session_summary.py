import asyncio
from pathlib import Path
from threading import get_ident
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from acp.connection import StreamDirection, StreamEvent

from app.agents.acp_client import normalize_update
from app.domain.models import MessageCreate, ProjectCreate, RunStatus, SessionCreate, TaskCreate
from app.main import create_app
from test_http_runs import api, http_records, live_server, setup_app, submit, terminal
from test_events import next_event
from test_storage import records, repository, storage


def message(text):
    return MessageCreate(message=text, request_id=str(uuid4()))


@pytest.mark.asyncio
async def test_title_priority_activity_and_reopen(storage, records):
    project, session = records
    empty = await storage.get_session_summary(session["id"])
    assert empty["title"] == "新会话"
    assert empty["updated_at"] == session["created_at"]
    assert empty["activity_event_id"] == 0
    run, _ = await storage.create_run(session["id"], message("  修复\n 侧栏   排序  "))
    await storage.create_run(session["id"], message("第二条消息不改变标题"))
    first = await storage.get_session_summary(session["id"])
    assert first["title"] == "修复 侧栏 排序"
    raw = {"method": "session/update", "params": {"update": {
        "sessionUpdate": "session_info_update", "title": "旧协议 标题",
    }}}
    await storage.append_event(run["id"], "raw", {"raw": raw})
    assert (await storage.get_session_summary(session["id"]))["title"] == "旧协议 标题"
    await storage.append_event(run["id"], **normalize_update({**raw, "params": {"update": {
        "sessionUpdate": "session_info_update", "title": " 智能体\n新标题 ",
    }}}))
    await storage.append_event(run["id"], "session.title", {"title": "\n \t"})
    await storage.append_event(run["id"], "session.model", {"model_id": "model"})
    latest = await storage.get_session_summary(session["id"])
    assert latest["title"] == "智能体 新标题"
    assert latest["updated_at"] == first["updated_at"]
    assert latest["activity_event_id"] == first["activity_event_id"]
    assert "agent_title" not in latest
    await storage.close()
    await storage.open()
    assert await storage.get_session_summary(session["id"]) == latest
    assert (await storage.list_sessions(project["id"]))[0] == latest


@pytest.mark.asyncio
async def test_task_goal_and_old_session_activity_sort(storage, records):
    project, parent = records
    other = await storage.create_session(SessionCreate(project_id=project["id"], harness="opencode"))
    parent_run, _ = await storage.create_run(parent["id"], message("主会话"))
    await storage.create_run(other["id"], message("较新会话"))
    assert (await storage.list_sessions(None))[0]["id"] == other["id"]
    activity = await storage.append_event(parent_run["id"], "agent.text", {"text": "继续", "channel": "message"})
    rows = await storage.list_sessions(project["id"])
    assert rows[0]["id"] == parent["id"]
    assert rows[0]["activity_event_id"] == activity["id"]
    _, child, child_run, _ = await storage.create_task_with_run(parent["id"], TaskCreate(
        harness="codebuddy", goal=" 调研\n方案 ", acceptance="给出依据", request_id=str(uuid4()),
    ))
    assert (await storage.get_session_summary(child["id"]))["title"] == "调研 方案"
    await storage.append_event(child_run["id"], "session.title", {"title": "子会话自动标题"})
    summary = await storage.get_session_summary(child["id"])
    assert summary["title"] == "子会话自动标题"
    assert summary["parent_id"] == parent["id"]


@pytest.mark.asyncio
async def test_equal_activity_times_sort_by_event_id_and_scope(storage, records):
    project, first = records
    second = await storage.create_session(SessionCreate(project_id=project["id"], harness="codex"))
    async with storage.transaction() as db:
        for session in (first, second):
            await db.execute(
                "INSERT INTO events(project_id,session_id,kind,payload,created_at) VALUES(?,?,'agent.text','{}',?)",
                (project["id"], session["id"], "2026-10-18T00:00:00+00:00"),
            )
    assert [row["id"] for row in await storage.list_sessions(project["id"])] == [second["id"], first["id"]]
    assert await storage.list_sessions(str(uuid4())) == []


@pytest.mark.asyncio
async def test_folder_name_derived_without_rewriting_legacy_name(storage, records, repository):
    project, _ = records
    assert project["name"] == repository.name
    async with storage.transaction() as db:
        await db.execute("UPDATE projects SET name='旧项目名' WHERE id=?", (project["id"],))
    assert (await storage.get_project(project["id"]))["name"] == repository.name
    assert (await storage.list_projects())[0]["name"] == repository.name
    async with storage.transaction() as db:
        async with db.execute("SELECT name FROM projects WHERE id=?", (project["id"],)) as cursor:
            assert (await cursor.fetchone())[0] == "旧项目名"


@pytest.mark.asyncio
async def test_summary_http_and_path_only_project_creation(tmp_path, repository):
    application = create_app(tmp_path / "http.db")
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
            response = await client.post("/api/projects", json={"path": str(repository)})
            assert response.status_code == 201
            project = response.json()
            assert project["name"] == repository.name
            session = (await client.post("/api/sessions", json={"project_id": project["id"], "harness": "codex"})).json()
            await application.state.storage.create_run(session["id"], message("HTTP 摘要"))
            detail = (await client.get(f"/api/sessions/{session['id']}")).json()["session"]
            assert detail["title"] == "HTTP 摘要"
            assert detail["activity_event_id"] > 0
            assert (await client.get("/api/sessions")).json() == [detail]
            assert (await client.get(f"/api/projects/{project['id']}/sessions")).json() == [detail]
            assert (await client.get(f"/api/sessions/{uuid4()}")).status_code == 404
    assert ProjectCreate(path=str(repository), name="兼容旧客户端").name == "兼容旧客户端"


@pytest.mark.asyncio
async def test_sidebar_conditional_snapshot_reuses_io_and_tracks_changes(api, http_records, monkeypatch):
    client, application, _ = api
    project, session = http_records
    storage = application.state.storage
    employees = application.state.employees
    listing = AsyncMock(wraps=storage.list_sessions)
    decorate = employees.decorate
    loop_thread = get_ident()

    def decorated(row):
        assert get_ident() != loop_thread
        return decorate(row)

    decoration = Mock(side_effect=decorated)
    monkeypatch.setattr(storage, "list_sessions", listing)
    monkeypatch.setattr(employees, "decorate", decoration)
    first = await client.get("/api/sessions")
    etag = first.headers["etag"]
    headers = {"If-None-Match": etag}
    responses = await asyncio.gather(*(client.get("/api/sessions", headers=headers) for _ in range(12)))
    assert all(response.status_code == 304 and response.content == b"" for response in responses)
    assert listing.await_count == decoration.call_count == 1
    assert first.headers["cache-control"] == "private, no-cache"
    assert (await client.get("/api/sessions")).json() == first.json()
    assert listing.await_count == 1

    await storage.save_harness_settings("codex", {})
    assert (await client.get("/api/sessions", headers=headers)).status_code == 304
    assert listing.await_count == decoration.call_count == 2
    created = await storage.create_session(SessionCreate(project_id=project["id"], harness="codex"))
    changed = await client.get("/api/sessions", headers=headers)
    assert changed.status_code == 200
    assert {row["id"] for row in changed.json()} == {session["id"], created["id"]}
    await storage.set_session_connection(session["id"], "native-sidebar", 123)
    connected = await client.get("/api/sessions", headers={"If-None-Match": changed.headers["etag"]})
    assert connected.status_code == 200
    assert next(row for row in connected.json() if row["id"] == session["id"])["native_id"] == "native-sidebar"
    await storage.delete_sessions([created["id"]])
    deleted = await client.get("/api/sessions", headers={"If-None-Match": connected.headers["etag"]})
    assert deleted.status_code == 200
    assert [row["id"] for row in deleted.json()] == [session["id"]]
    run, _ = await storage.create_run(session["id"], message("侧栏新标题"))
    await storage.append_event(run["id"], "session.title", {"title": "跨页面新标题"})
    titled = await client.get("/api/sessions", headers={"If-None-Match": deleted.headers["etag"]})
    assert titled.status_code == 200
    assert titled.json()[0]["title"] == "跨页面新标题"


@pytest.mark.asyncio
async def test_sidebar_write_during_snapshot_is_not_cached_as_current(api, http_records, monkeypatch):
    client, application, _ = api
    project, _ = http_records
    storage = application.state.storage
    original = storage.list_sessions

    async def concurrent_snapshot(project_id):
        rows = await original(project_id)
        await storage.create_session(SessionCreate(project_id=project["id"], harness="codex"))
        monkeypatch.setattr(storage, "list_sessions", original)
        return rows

    monkeypatch.setattr(storage, "list_sessions", concurrent_snapshot)
    first = await client.get("/api/sessions")
    latest = await client.get("/api/sessions", headers={"If-None-Match": first.headers["etag"]})
    assert latest.status_code == 200
    assert len(latest.json()) == len(first.json()) + 1


@pytest.mark.parametrize("title", [None, "", "\t\n "])
def test_blank_agent_title_is_metadata_only(title):
    assert normalize_update({"method": "session/update", "params": {"update": {
        "sessionUpdate": "session_info_update", "title": title,
    }}})["kind"] == "raw"


@pytest.mark.asyncio
async def test_agent_title_persisted_through_journal_http_and_sse(api, http_records):
    client, application, factory = api
    project, session = http_records
    run = await submit(client, session, "首条提问标题")
    agent, _ = await factory.started()
    before = await application.state.storage.get_session_summary(session["id"])
    agent.observe(StreamEvent(StreamDirection.INCOMING, {
        "jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "native", "update": {"sessionUpdate": "session_info_update", "title": "智能体自己的标题"},
        },
    }))
    agent.finish()
    await terminal(client, run["run_id"])
    history = (await client.get("/api/events", params={"project_id": project["id"], "session_id": session["id"]})).json()
    title_event, = [row for row in history if row["kind"] == "session.title"]
    assert title_event["payload"]["title"] == "智能体自己的标题"
    after = (await client.get(f"/api/sessions/{session['id']}")).json()["session"]
    assert after["title"] == "智能体自己的标题"
    assert after["activity_event_id"] > before["activity_event_id"]
    async with live_server(application) as live:
        async with live.stream("GET", "/api/stream", params={
            "project_id": project["id"], "session_id": session["id"],
            "after_event_id": title_event["id"] - 1,
        }) as response:
            received, _ = await next_event(response.aiter_lines())
            assert received == title_event


@pytest.mark.asyncio
async def test_frontend_loads_real_http_summary_tree_and_history(api, http_records):
    _, application, _ = api
    project, parent = http_records
    storage = application.state.storage
    run, _ = await storage.create_run(parent["id"], message("真实 HTTP 联调"))
    await storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    await storage.append_event(run["id"], "agent.text", {"text": "正常回答", "channel": "message"})
    await storage.transition_run(run["id"], {RunStatus.RUNNING}, RunStatus.COMPLETED, {})
    _, child, _, _ = await storage.create_task_with_run(parent["id"], TaskCreate(
        harness="codebuddy", goal="子会话目标", acceptance="说明结果", request_id=str(uuid4()),
    ))
    script = r"""
const assert = require('node:assert/strict');
const {environment} = require(process.argv[5]);
const env = environment();
const [origin, projectId, sessionId, childId] = process.argv.slice(1);
env.run('globalThis').fetch = (url, options) => fetch(origin + url, options);
env.run('globalThis.EventSource = class {addEventListener() {} close() {}};');
env.storage.set('agentsteam.selection', JSON.stringify({projectId, sessionId}));
(async () => {
  await env.run('init()');
  env.flushFrames();
  assert.equal(env.run('state.context.ready'), true);
  assert.equal(env.get('#session-title').textContent, '真实 HTTP 联调');
  assert.equal(env.document.querySelectorAll('.run').length, 1);
  assert.equal(env.get('.run-answer').querySelector('.agent-text').innerHTML.includes('正常回答'), true);
  assert.equal(env.get('#children-' + sessionId).querySelector('.session-button').dataset.sessionId, childId);
  assert.deepEqual(env.get('#session-list').children.map(node => node.dataset.sessionId), [sessionId]);
  assert.equal(env.run('state.workTimer'), null);
  console.log('DOM_HTTP_OK');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    async with live_server(application) as live:
        process = await asyncio.create_subprocess_exec(
            "node", "-e", script, str(live.base_url).rstrip("/"), project["id"], parent["id"], child["id"],
            str(Path(__file__).with_name("frontend_dom.cjs").resolve()),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == 0, stderr.decode("utf-8", "replace")
        assert b"DOM_HTTP_OK" in stdout
