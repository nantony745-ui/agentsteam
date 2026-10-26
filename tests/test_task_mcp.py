"""真实 stdio MCP → 临时 HTTP → SQLite/编排器；仅 ACP Agent 使用替身。"""

import asyncio
from pathlib import Path

import pytest

from test_delegation import delegation, drain, task_input
from test_feedback import feedback
from test_http_runs import live_server
from test_mcp_bridge import bridge_session, MEMORY_TOOLS
from test_storage import repository
from test_task_operations import snapshots
from test_git_delivery import worktree


async def call(session, name, arguments):
    response = await session.call_tool(name, arguments)
    assert not response.is_error, response
    return response.structured_content


@pytest.mark.asyncio
async def test_mcp_task_and_feedback_business_round_trip(delegation, tmp_path, monkeypatch):
    _, app, factory, project, parent, auth = delegation
    token = auth["Authorization"].split(" ")[1]
    worker_path = tmp_path / "worker-bridge"
    worker_path.mkdir()
    async with live_server(app) as http:
        async with bridge_session(tmp_path, "coordinator", str(http.base_url), token) as coordinator:
            handle = await call(coordinator, "delegate_task", task_input())
            worker, _ = await factory.started()
            status = await call(coordinator, "get_task_status", {"task_ids": [handle["task_id"]]})
            assert status["tasks"] == [{**handle, "status": "running", "run_status": "running"}]
            initial = await call(coordinator, "get_task_result", {"task_id": handle["task_id"]})
            assert initial["result"] is None
            note = await feedback(http, parent["id"], "主会话经真实 MCP 拉取")
            child_note = await feedback(http, handle["child_session_id"], "Worker 经真实 MCP 拉取")
            received = await call(coordinator, "check_user_feedback", {})
            assert received == {"messages": [{"id": note["id"], "message": note["payload"]["message"],
                                              "created_at": note["created_at"]}]}
            assert await call(coordinator, "check_user_feedback", {}) == {"messages": []}
            async with bridge_session(worker_path, "worker", str(http.base_url), worker.token) as child:
                assert {tool.name for tool in (await child.list_tools()).tools} == {"check_user_feedback"} | MEMORY_TOOLS
                received = await call(child, "check_user_feedback", {})
                assert received["messages"][0]["id"] == child_note["id"]
                assert received["messages"][0]["message"] == child_note["payload"]["message"]
            observed = snapshots(app, monkeypatch)
            waiting = asyncio.create_task(call(coordinator, "get_task_status", {
                "task_ids": [handle["task_id"]], "wait_ms": 1200000,
            }))
            try:
                await asyncio.wait_for(observed.get(), 3)
                assert not waiting.done()
                worker.finish("真实 MCP 取得持久化结果")
                assert (await asyncio.wait_for(waiting, 3))["tasks"][0]["status"] == "review"
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            completed = await call(coordinator, "get_task_result", {"task_id": handle["task_id"]})
            assert completed["result"] == "真实 MCP 取得持久化结果" and completed["run_status"] == "completed"
            task = await app.state.storage.get_task(handle["task_id"])
            assert completed["worktree"] is task["worktree"] is None
            assert completed["branch"] is None
            root = await worktree(Path(project["path"]))
            to_cancel = await call(coordinator, "delegate_task", {**task_input(), "working_dir": str(root)})
            selected, _ = await factory.started()
            assert selected.cwd == root
            artifacts = await call(coordinator, "get_task_result", {"task_id": to_cancel["task_id"]})
            assert artifacts["worktree"] == str(root) and artifacts["branch"].startswith("chosen/")
            canceled = await call(coordinator, "cancel_task", {"task_id": to_cancel["task_id"]})
            assert canceled["status"] == canceled["run_status"] == "canceled"
            assert await call(coordinator, "cancel_task", {"task_id": to_cancel["task_id"]}) == canceled
            assert (await call(coordinator, "get_task_result", {"task_id": handle["task_id"]})) == completed
            await drain(app, to_cancel)
