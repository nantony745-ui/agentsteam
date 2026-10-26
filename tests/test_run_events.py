"""任务 2.3；复用 2.2 的临时文件库、Git 仓库与关联记录 fixture。"""

import asyncio
import copy
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from uuid import UUID

import pytest
from acp.schema import AgentMessageChunk, TextContentBlock, ToolCallProgress

from app.agents.acp_client import normalize_update, project_events
from app.domain.models import MessageCreate, RunStatus, SessionCreate
from app.db.storage import RequestConflictError, Storage
from test_storage import insert, linked_task, records, repository, storage, uid


def message(text):
    return MessageCreate(message=text, request_id=uid())


def wire(update):
    return {"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": "native-session", "update": update}}


def text_wire(text, channel):
    return wire({"sessionUpdate": channel, "content": {"type": "text", "text": text}})


def snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return {
            table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("runs", "events", "tasks")
        }


async def reject_event(storage, kind, status):
    async with storage.transaction() as db:
        # 值仅来自本文件的固定测试参数；触发器在真实 INSERT 路径抛错。
        await db.execute(
            "CREATE TEMP TRIGGER reject_event BEFORE INSERT ON events "
            f"WHEN NEW.kind='{kind}' AND json_extract(NEW.payload, '$.status')='{status}' "
            "BEGIN SELECT RAISE(ABORT, 'injected event failure'); END"
        )


@pytest.mark.asyncio
async def test_create_run_idempotency_conflict_and_reopen(storage, records):
    project, session = records
    data = message("保留 ' 原始输入\n ")
    run, created = await storage.create_run(session["id"], data)
    assert created is True
    UUID(run["id"])
    assert run["prompt"] == data.message
    assert run["status"] == "queued"
    assert run["task_id"] is None and run["attempt"] == 1
    assert datetime.fromisoformat(run["created_at"]).utcoffset().total_seconds() == 0
    before = snapshot(storage.path)
    assert len(before["runs"]) == 1
    assert [e["kind"] for e in before["events"]] == ["user.message", "run.status"]
    assert json.loads(before["events"][0]["payload"])["message"] == data.message
    assert json.loads(before["events"][1]["payload"])["status"] == "queued"
    assert all(e["project_id"] == project["id"] and e["session_id"] == session["id"]
               and e["run_id"] == run["id"] for e in before["events"])
    assert await storage.create_run(session["id"], data) == (run, False)
    with pytest.raises(RequestConflictError):
        await storage.create_run(session["id"], data.model_copy(update={"message": data.message.rstrip()}))
    assert snapshot(storage.path) == before
    await storage.close()
    reopened = Storage(storage.path)
    await reopened.open()
    try:
        assert await reopened.create_run(session["id"], data) == (run, False)
        assert snapshot(storage.path) == before
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_concurrent_create_and_session_scoped_key(storage, records):
    project, session = records
    data = message("并发提交")
    results = await asyncio.gather(*(storage.create_run(session["id"], data) for _ in range(8)))
    assert sum(created for _, created in results) == 1
    assert len({run["id"] for run, _ in results}) == 1
    state = snapshot(storage.path)
    assert len(state["runs"]) == 1 and len(state["events"]) == 2
    other = await storage.create_session(SessionCreate(project_id=project["id"], harness="opencode"))
    second, created = await storage.create_run(other["id"], data)
    assert created and second["id"] != results[0][0]["id"]
    state = snapshot(storage.path)
    assert len(state["runs"]) == 2 and len(state["events"]) == 4


@pytest.mark.asyncio
async def test_concurrent_conflicting_messages(storage, records):
    _, session = records
    data = message("A")
    results = await asyncio.gather(
        storage.create_run(session["id"], data),
        storage.create_run(session["id"], data.model_copy(update={"message": "B"})),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RequestConflictError) for result in results) == 1
    state = snapshot(storage.path)
    assert len(state["runs"]) == 1 and len(state["events"]) == 2
    assert json.loads(state["events"][0]["payload"])["message"] == state["runs"][0]["prompt"]


@pytest.mark.asyncio
async def test_claim_once_and_reject_invalid_transitions(storage, records):
    _, session = records
    run, _ = await storage.create_run(session["id"], message("认领"))
    before = snapshot(storage.path)
    assert not await storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.COMPLETED, {})
    assert not await storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.QUEUED, {})
    assert not await storage.transition_run(run["id"], set(), RunStatus.RUNNING, {})
    assert not await storage.transition_run(uid(), {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    assert snapshot(storage.path) == before
    claims = await asyncio.gather(*(
        storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.RUNNING, {}) for _ in range(6)
    ))
    assert claims.count(True) == 1
    assert not await storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    state = snapshot(storage.path)
    assert len(state["runs"]) == 1 and state["runs"][0]["status"] == "running"
    assert state["runs"][0]["started_at"] is not None
    assert state["runs"][0]["finished_at"] is None
    assert len(state["events"]) == 3
    assert json.loads(state["events"][-1]["payload"]) == {"from": "queued", "status": "running"}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.INTERRUPTED])
async def test_terminal_is_immutable_and_permission_resume_preserves_start(storage, records, terminal):
    _, session = records
    run, _ = await storage.create_run(session["id"], message("终态"))
    assert await storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    started = (await storage.get_run(run["id"]))["started_at"]
    assert await storage.transition_run(run["id"], {RunStatus.RUNNING}, RunStatus.WAITING_PERMISSION, {})
    assert await storage.transition_run(run["id"], {RunStatus.WAITING_PERMISSION}, RunStatus.RUNNING, {})
    payload = {"result": "保留的结果", "error": None, "stop_reason": "cancelled",
               "status": "伪造状态", "from": "伪造来源", "started_at": "伪造时间"}
    assert await storage.transition_run(run["id"], {RunStatus.RUNNING}, terminal, payload)
    final = await storage.get_run(run["id"])
    assert final["status"] == terminal and final["result"] == "保留的结果"
    assert final["stop_reason"] == "cancelled" and final["started_at"] == started
    assert final["finished_at"] is not None
    before = snapshot(storage.path)
    event = json.loads(before["events"][-1]["payload"])
    assert event["from"] == "running" and event["status"] == terminal
    for target in RunStatus:
        assert not await storage.transition_run(run["id"], set(RunStatus), target, {"result": "迟到覆盖"})
    assert snapshot(storage.path) == before


@pytest.mark.asyncio
async def test_completion_cancel_race_has_one_terminal(storage, records):
    _, session = records
    run, _ = await storage.create_run(session["id"], message("取消竞争"))
    assert await storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    assert await storage.transition_run(run["id"], {RunStatus.RUNNING}, RunStatus.CANCEL_REQUESTED, {})
    outcomes = await asyncio.gather(
        storage.transition_run(run["id"], {RunStatus.CANCEL_REQUESTED}, RunStatus.COMPLETED, {"result": "done"}),
        storage.transition_run(run["id"], {RunStatus.CANCEL_REQUESTED}, RunStatus.CANCELED, {"stop_reason": "cancelled"}),
    )
    assert outcomes.count(True) == 1
    state = snapshot(storage.path)
    assert len(state["runs"]) == 1 and len(state["events"]) == 5
    final_status = state["runs"][0]["status"]
    assert final_status in {"completed", "canceled"}
    assert json.loads(state["events"][-1]["payload"])["status"] == final_status


@pytest.mark.asyncio
async def test_queued_cancel_never_marks_started(storage, records):
    _, session = records
    run, _ = await storage.create_run(session["id"], message("未运行取消"))
    assert await storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.CANCELED, {})
    state = snapshot(storage.path)
    assert state["runs"][0]["started_at"] is None
    assert state["runs"][0]["finished_at"] is not None
    assert len(state["events"]) == 3


@pytest.mark.asyncio
async def test_create_event_failure_rolls_back_run_and_input(storage, records):
    _, session = records
    data = message("创建回滚")
    before = snapshot(storage.path)
    await reject_event(storage, "run.status", "queued")
    with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
        await storage.create_run(session["id"], data)
    assert snapshot(storage.path) == before
    async with storage.transaction() as db:
        await db.execute("DROP TRIGGER reject_event")
    assert (await storage.create_run(session["id"], data))[1] is True
    state = snapshot(storage.path)
    assert len(state["runs"]) == 1 and len(state["events"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [RunStatus.RUNNING, RunStatus.COMPLETED])
async def test_transition_event_failure_rolls_back_all_fields(storage, records, target):
    _, session = records
    run, _ = await storage.create_run(session["id"], message("转换回滚"))
    previous = RunStatus.QUEUED
    if target == RunStatus.COMPLETED:
        assert await storage.transition_run(run["id"], {previous}, RunStatus.RUNNING, {})
        previous = RunStatus.RUNNING
    before = snapshot(storage.path)
    await reject_event(storage, "run.status", target)
    with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
        await storage.transition_run(run["id"], {previous}, target,
                                     {"result": "应回滚", "error": "应回滚", "stop_reason": "end_turn"})
    assert snapshot(storage.path) == before
    async with storage.transaction() as db:
        await db.execute("DROP TRIGGER reject_event")
    assert await storage.transition_run(run["id"], {previous}, target, {})


@pytest.mark.asyncio
async def test_old_task_run_cannot_overwrite_new_attempt(storage, records):
    project, parent = records
    async with storage.transaction() as db:
        task, old = await linked_task(db, project, parent)
    assert await storage.transition_run(old["id"], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    newer = {**old, "id": uid(), "request_id": uid(), "attempt": 2}
    async with storage.transaction() as db:
        await insert(db, "runs", newer)
        await db.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (newer["id"], task["id"]))
    before = snapshot(storage.path)
    assert not await storage.transition_run(old["id"], {RunStatus.RUNNING}, RunStatus.COMPLETED,
                                            {"result": "旧结果"})
    assert snapshot(storage.path) == before
    assert await storage.transition_run(newer["id"], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    event = await storage.append_event(old["id"], "raw", {"late": True})
    assert event["run_id"] == old["id"] and event["task_id"] == task["id"]
    assert event["session_id"] == old["session_id"] and event["project_id"] == project["id"]
    state = snapshot(storage.path)
    assert len(state["runs"]) == 2 and len(state["events"]) == 4
    task_event, = [row for row in state["events"] if row["kind"] == "task.status"]
    assert task_event["session_id"] == parent["id"] and task_event["run_id"] == old["id"]
    assert json.loads(task_event["payload"]) == {"from": "queued", "status": "running"}
    assert state["tasks"] == before["tasks"]
    assert (await storage.get_run(old["id"]))["result"] is None


@pytest.mark.asyncio
async def test_append_event_guards_and_missing_session(storage, records):
    _, session = records
    before = snapshot(storage.path)
    with pytest.raises(sqlite3.IntegrityError):
        await storage.create_run(uid(), message("未知会话"))
    assert snapshot(storage.path) == before
    run, _ = await storage.create_run(session["id"], message("事件"))
    before = snapshot(storage.path)
    with pytest.raises(ValueError, match="run.status"):
        await storage.append_event(run["id"], "run.status", {"status": "running"})
    with pytest.raises(ValueError, match="Run 不存在"):
        await storage.append_event(uid(), "raw", {})
    assert snapshot(storage.path) == before


@pytest.mark.asyncio
async def test_sender_contract_observes_committed_rows_and_only_sends_once(storage, records):
    _, session = records
    data = message("先提交再发送")
    sent = []

    async def send(run):
        committed = snapshot(storage.path)
        assert len(committed["runs"]) == 1
        assert committed["runs"][0]["status"] == "running"
        assert [e["kind"] for e in committed["events"]] == ["user.message", "run.status", "run.status"]
        assert json.loads(committed["events"][0]["payload"])["message"] == data.message
        sent.append(run["id"])

    async def claim_and_send(run):
        if await storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.RUNNING, {}):
            await send(run)

    async def submit():
        run, created = await storage.create_run(session["id"], data)
        if created:
            await claim_and_send(run)
        return run

    runs = await asyncio.gather(*(submit() for _ in range(5)))
    await claim_and_send(runs[0])
    assert sent == [runs[0]["id"]]
    assert len(snapshot(storage.path)["events"]) == 3


@pytest.mark.parametrize("update_type,channel", [("agent_message_chunk", "message"), ("agent_thought_chunk", "thought")])
def test_normalize_text_and_immutable_raw(update_type, channel):
    raw = text_wire("文本", update_type)
    original = copy.deepcopy(raw)
    normalized = normalize_update(raw)
    assert normalized["kind"] == "agent.text"
    assert normalized["payload"]["text"] == "文本"
    assert normalized["payload"]["channel"] == channel
    assert "raw" not in normalized["payload"]
    assert normalized["payload"]["text"] == "文本"
    raw["params"]["update"]["content"]["text"] = "外部修改"
    assert "raw" not in normalized["payload"]
    assert normalized["payload"]["text"] == "文本"


@pytest.mark.parametrize("update", [
    {"sessionUpdate": "plan", "entries": []},
    {"sessionUpdate": "plan_update", "content": {"type": "markdown", "markdown": "计划"}},
    {"sessionUpdate": "plan_removed"},
])
def test_normalize_plans(update):
    raw = wire(update)
    normalized = normalize_update(raw)
    assert normalized["kind"] == "agent.plan"
    assert "raw" not in normalized["payload"]
    assert all(normalized["payload"][key] == value for key, value in update.items())


@pytest.mark.parametrize("data", [
    {"configOptions": [{"id": "model", "category": "model", "type": "select", "currentValue": "gpt-6.1-sol"}]},
    {"models": {"currentModelId": "gpt-6.1-sol"}},
])
def test_session_response_current_model(data):
    raw = {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "native", **data}}
    assert normalize_update(raw) == {
        "kind": "session.model", "payload": {"model_id": "gpt-6.1-sol"},
    }


def test_model_configuration_push_and_unrelated_options():
    option = {"id": "model", "category": "model", "type": "select", "currentValue": "actual-model"}
    raw = wire({"sessionUpdate": "config_option_update", "configOptions": [option]})
    assert normalize_update(raw)["payload"]["model_id"] == "actual-model"
    for unrelated in [
        {**option, "category": "thought_level"},
        {**option, "currentValue": ""},
        {**option, "currentValue": None, "options": [{"value": "must-not-guess"}]},
    ]:
        assert normalize_update(wire({"sessionUpdate": "config_option_update", "configOptions": [unrelated]}))["kind"] == "raw"
    assert normalize_update({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "native"}})["kind"] == "raw"


def test_permission_error_and_unknown_messages_keep_raw():
    permission = {"jsonrpc": "2.0", "id": "permission-native", "method": "session/request_permission",
                  "params": {"sessionId": "native-session", "toolCall": {"toolCallId": "工具"},
                             "options": [{"optionId": "allow-once", "name": "允许", "kind": "allow_once"}]}}
    result = normalize_update(permission)
    assert result["kind"] == "raw"
    assert result["payload"]["request_id"] == "permission-native"
    assert result["payload"]["options"] == permission["params"]["options"]
    assert result["payload"]["toolCall"] == permission["params"]["toolCall"]
    error = {"jsonrpc": "2.0", "id": 3, "error": {"code": -32000, "message": "失败", "data": {"why": "test"}}}
    assert normalize_update(error)["kind"] == "error"
    for raw, kind in ((permission, "raw"), (error, "error"),
                      (wire({"sessionUpdate": "future_update", "newField": {"x": 1}}), "raw"),
                      ({"jsonrpc": "2.0", "method": "extension/unknown", "params": {"x": 1}}, "raw"),
                      (wire({"sessionUpdate": "agent_message_chunk", "content": {"type": "image", "data": "AA=="}}), "raw")):
        result = normalize_update(raw)
        assert result["kind"] == kind and "raw" not in result["payload"]
        assert "jsonrpc" not in result["payload"]


def test_installed_sdk_aliases_and_partial_update():
    update = ToolCallProgress(tool_call_id="call-1", status="completed", session_update="tool_call_update")
    raw = wire(update.model_dump(mode="json", by_alias=True, exclude_unset=True))
    result = normalize_update(raw)
    assert result["kind"] == "agent.tool"
    assert result["payload"]["toolCallId"] == "call-1"
    assert "title" not in result["payload"]
    chunk = AgentMessageChunk(content=TextContentBlock(type="text", text="SDK"), session_update="agent_message_chunk")
    assert normalize_update(wire(chunk.model_dump(mode="json", by_alias=True)))["payload"]["text"] == "SDK"


@pytest.mark.asyncio
async def test_replay_merges_tools_and_orders_text_across_runs_after_reopen(storage, records):
    project, first_session = records
    second_session = await storage.create_session(SessionCreate(project_id=project["id"], harness="opencode"))
    first, _ = await storage.create_run(first_session["id"], message("第一轮"))
    second, _ = await storage.create_run(first_session["id"], message("第二轮"))
    other, _ = await storage.create_run(second_session["id"], message("另一会话"))
    samples = [
        (first, wire({"sessionUpdate": "tool_call", "toolCallId": "same", "title": "读取", "status": "pending",
                      "content": [{"type": "content", "content": {"type": "text", "text": "旧内容"}}]})),
        (first, text_wire("甲", "agent_message_chunk")),
        (second, text_wire("第二轮", "agent_message_chunk")),
        (other, wire({"sessionUpdate": "tool_call", "toolCallId": "same", "title": "另一工具", "status": "pending"})),
        (first, wire({"sessionUpdate": "tool_call_update", "toolCallId": "same", "status": "in_progress", "title": None})),
        (first, text_wire("乙", "agent_message_chunk")),
        (first, text_wire("思考", "agent_thought_chunk")),
        (first, wire({"sessionUpdate": "tool_call_update", "toolCallId": "same", "status": "completed", "content": []})),
        (first, wire({"sessionUpdate": "future_update", "opaque": [1, 2]})),
    ]
    for index, (run, raw) in enumerate(samples):
        normalized = normalize_update(raw)
        await storage.append_event(run["id"], **normalized)
        if index == 4:
            partial = project_events(await storage.full_events(project["id"], None, 0, 100))
            tool = partial["tools"][(first_session["id"], "same")]
            assert tool["title"] == "读取" and tool["status"] == "in_progress"
            assert tool["content"] == samples[0][1]["params"]["update"]["content"]
    before = snapshot(storage.path)
    await storage.close()
    reopened = Storage(storage.path)
    await reopened.open()
    try:
        events = await reopened.full_events(project["id"], None, 0, 100)
        original = copy.deepcopy(events)
        projected = project_events(list(reversed(events)))
        assert projected["texts"] == {
            (first["id"], "message"): "甲乙", (first["id"], "thought"): "思考",
            (second["id"], "message"): "第二轮",
        }
        tools = projected["tools"]
        assert len(tools) == 2
        assert tools[(first_session["id"], "same")]["title"] == "读取"
        assert tools[(first_session["id"], "same")]["status"] == "completed"
        assert tools[(first_session["id"], "same")]["content"] == []
        assert tools[(second_session["id"], "same")]["title"] == "另一工具"
        assert tools[(second_session["id"], "same")]["status"] == "pending"
        persisted = [json.loads(e["payload"])["raw"] for e in events if "raw" in json.loads(e["payload"])]
        assert persisted == []
        assert events == original and snapshot(storage.path) == before
    finally:
        await reopened.close()
