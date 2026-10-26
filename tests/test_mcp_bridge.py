"""真实 MCP stdio 子进程与本机 HTTP 转发；不调用 Agent。"""

import json
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp import ClientSession
from mcp.client import stdio
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.agents import acp_client
from app.mcp import bridge
from test_http_runs import live_server


BRIDGE = Path(__file__).resolve().parents[1] / "app/mcp/bridge.py"
MEMORY_TOOLS = {"memory_retain", "memory_recall", "memory_reflect"}
COORDINATOR_TOOLS = MEMORY_TOOLS | {"delegate_task", "get_task_status", "get_task_result", "cancel_task", "check_user_feedback"}


@asynccontextmanager
async def bridge_session(tmp_path, role, base_url, token, team=False):
    cwd = tmp_path / "different worktree with spaces"
    cwd.mkdir(exist_ok=True)
    bridge = acp_client.build_mcp_bridge(base_url, token, role)
    parameters = StdioServerParameters(
        command=sys.executable, args=[str(BRIDGE)], cwd=cwd,
        env={**{item.name: item.value for item in bridge.env}, "HTTP_PROXY": "http://127.0.0.1:1",
             "HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1",
             "NO_PROXY": "", "HARNESS_LAB_TEAM": "1" if team else "0"},
    )
    with (tmp_path / "bridge-stderr.log").open("w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(*streams, read_timeout_seconds=10) as session:
                await session.initialize()
                yield session


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["coordinator", "worker"])
async def test_role_tools_and_protocol_only_stdout(tmp_path, role, monkeypatch):
    lines = []
    parse_line = stdio._parse_line

    def capture(line):
        lines.append(line)
        return parse_line(line)

    monkeypatch.setattr(stdio, "_parse_line", capture)
    async with bridge_session(tmp_path, role, "http://127.0.0.1:1", "test-session-secret") as session:
        tools = (await session.list_tools()).tools
        assert {tool.name for tool in tools} == (COORDINATOR_TOOLS if role == "coordinator" else {"check_user_feedback"} | MEMORY_TOOLS)
        for tool in tools:
            assert "parent_session_id" not in tool.input_schema.get("properties", {})
            assert tool.description
            assert all(prop.get("description") for prop in tool.input_schema["properties"].values())
        if role == "worker":
            result = await session.call_tool("delegate_task", {})
            assert result.is_error
    assert lines and all(json.loads(line)["jsonrpc"] == "2.0" for line in lines)
    assert "test-session-secret" not in (tmp_path / "bridge-stderr.log").read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["coordinator", "worker"])
@pytest.mark.parametrize("enabled", ["opencode", "codebuddy", "deepseek", None])
async def test_service_metadata_uses_current_enabled_harnesses(tmp_path, monkeypatch, role, enabled):
    config = tmp_path / "agents.toml"
    config.write_text("\n".join(
        f"[agents.{name}]\nenabled = {str(name == enabled).lower()}"
        for name in ("codex", "opencode", "codebuddy", "deepseek")
    ) + "\n[agents.unsupported]\nenabled = true\n", encoding="utf-8-sig")
    monkeypatch.setattr(acp_client, "CONFIG_PATH", config)
    async with bridge_session(tmp_path, role, "http://127.0.0.1:1", "test-session-secret") as session:
        initialized = await session.initialize()
        assert initialized.server_info.name == "harness_lab"
        assert f"当前启用的 harness：{enabled or '无'}。" in initialized.server_info.description
        assert f"当前启用的 harness：{enabled or '无'}。" in initialized.instructions
        assert "unsupported" not in initialized.instructions
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        if role == "coordinator":
            assert tools["delegate_task"].input_schema["properties"]["harness"]["enum"] == ([enabled] if enabled else [])
            directory = tools["delegate_task"].input_schema["properties"]["working_dir"]
            assert directory["default"] is None
            assert "working_dir" not in tools["delegate_task"].input_schema["required"]
            assert {item["type"] for item in directory["anyOf"]} == {"string", "null"}
            wait = tools["get_task_status"].input_schema["properties"]["wait_ms"]
            assert (wait["minimum"], wait["maximum"], wait["default"]) == (0, 1200000, 0)
        else:
            assert set(tools) == {"check_user_feedback"} | MEMORY_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["coordinator", "worker"])
async def test_team_tools_and_employee_payload_over_stdio(tmp_path, role):
    application = FastAPI()
    received = []

    @application.post('/internal/{path:path}')
    async def receive(path: str, request: Request):
        received.append((path, request.headers.get('authorization'), await request.json()))
        return {"members": [], "task_id": "fixture"}

    async with live_server(application) as base:
        async with bridge_session(tmp_path, role, str(base.base_url), 'team-secret', team=True) as session:
            tools = {item.name for item in (await session.list_tools()).tools}
            assert 'team_members' in tools
            assert not (await session.call_tool('team_members', {})).is_error
            assert received[-1] == ('team/members', 'Bearer team-secret', {})
            if role == 'coordinator':
                body = {"employee_id":str(uuid4()), "goal":"核验", "acceptance":"证据", "context_mode":"new", "request_id":str(uuid4())}
                assert not (await session.call_tool('delegate_task', body)).is_error
                assert received[-1] == ('tasks', 'Bearer team-secret', body)
            else:
                assert 'delegate_task' not in tools


@pytest.mark.asyncio
async def test_five_tools_forward_http_with_session_bearer_and_no_proxy(tmp_path):
    application = FastAPI()
    received = []

    @application.post("/internal/{path:path}")
    async def receive(path: str, request: Request):
        received.append((request.url.path, request.headers.get("authorization"), await request.json()))
        return {"messages": [{"id": "feedback-1", "message": "真实 HTTP 响应"}]}

    task_id, request_id = str(uuid4()), str(uuid4())
    calls = [
        ("check_user_feedback", {}, "/internal/feedback/pull", {}),
        ("delegate_task", {"harness": "codex", "goal": "阅读", "acceptance": "说明结构", "request_id": request_id},
         "/internal/tasks", {"harness": "codex", "goal": "阅读", "acceptance": "说明结构", "request_id": request_id}),
        ("delegate_task", {"harness": "codex", "goal": "阅读", "acceptance": "说明结构", "request_id": request_id,
                           "working_dir": "D:/已有目录 中文"},
         "/internal/tasks", {"harness": "codex", "goal": "阅读", "acceptance": "说明结构", "request_id": request_id,
                             "working_dir": "D:/已有目录 中文"}),
        ("get_task_status", {"task_ids": [task_id]}, "/internal/tasks/status", {"task_ids": [task_id], "wait_ms": 0}),
        ("get_task_status", {"task_ids": [task_id], "wait_ms": 1200000}, "/internal/tasks/status",
         {"task_ids": [task_id], "wait_ms": 1200000}),
        ("get_task_result", {"task_id": task_id}, f"/internal/tasks/{task_id}/result", {}),
        ("cancel_task", {"task_id": task_id}, f"/internal/tasks/{task_id}/cancel", {}),
    ]
    async with live_server(application) as http:
        async with bridge_session(tmp_path, "coordinator", str(http.base_url), "test-session-secret") as session:
            for name, arguments, path, payload in calls:
                result = await session.call_tool(name, arguments)
                assert not result.is_error, result
                assert result.structured_content == {"messages": [{"id": "feedback-1", "message": "真实 HTTP 响应"}]}
                assert received[-1] == (path, "Bearer test-session-secret", payload)
            count = len(received)
            for wait_ms in (-1, 1200001):
                result = await session.call_tool("get_task_status", {"task_ids": [task_id], "wait_ms": wait_ms})
                assert result.is_error
            assert len(received) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_ms", [0, 30000, 600000, 1200000])
async def test_http_read_timeout_covers_requested_wait(monkeypatch, wait_ms):
    received = []

    def receive(request):
        received.append(request)
        return httpx.Response(200, json={"tasks": []})

    client_factory = httpx.AsyncClient

    def client(**kwargs):
        return client_factory(**kwargs, transport=httpx.MockTransport(receive))

    monkeypatch.setattr(bridge.httpx, "AsyncClient", client)
    server = bridge.create_server("http://127.0.0.1:8765", "test-session-secret", "coordinator", ["codex"])
    result = await server.call_tool("get_task_status", {"task_ids": [], "wait_ms": wait_ms})
    assert result.structured_content == {"tasks": []}
    assert received[-1].extensions["timeout"] == {
        "connect": 40, "read": 40 + wait_ms / 1000, "write": 40, "pool": 40,
    }
    await server.call_tool("check_user_feedback", {})
    assert received[-1].extensions["timeout"]["read"] == 40


@pytest.mark.asyncio
async def test_http_and_network_errors_are_visible_and_do_not_break_stdio(tmp_path):
    application = FastAPI()

    @application.post("/internal/feedback/pull")
    async def unavailable():
        return JSONResponse(status_code=501, content={"detail": "尚未实现"})

    async with live_server(application) as http:
        base_url = str(http.base_url)
        async with bridge_session(tmp_path, "worker", base_url, "test-session-secret") as session:
            result = await session.call_tool("check_user_feedback", {})
            assert result.is_error
            assert "501" in str(result.content)
            assert {tool.name for tool in (await session.list_tools()).tools} == {"check_user_feedback"} | MEMORY_TOOLS
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        unreachable = f"http://127.0.0.1:{listener.getsockname()[1]}"
    async with bridge_session(tmp_path, "worker", unreachable, "test-session-secret") as session:
        result = await session.call_tool("check_user_feedback", {})
        assert result.is_error
        assert (await session.list_tools()).tools
    assert "test-session-secret" not in (tmp_path / "bridge-stderr.log").read_text(encoding="utf-8")
