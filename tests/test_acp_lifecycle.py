import asyncio
import os
from types import SimpleNamespace

import pytest
from acp.schema import PermissionOption, PromptResponse, ToolCallUpdate
from acp.exceptions import RequestError

from app.agents import acp_client
from scripts import acp_smoke


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.pending = None
        self.cancel_response = "cancelled"
        self.close_error = None

    async def prompt(self, **kwargs):
        self.calls.append(("prompt", kwargs))
        self.pending = asyncio.get_running_loop().create_future()
        return await self.pending

    async def cancel(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        if self.cancel_response is not None:
            self.pending.set_result(PromptResponse(stop_reason=self.cancel_response))

    async def close(self):
        self.calls.append(("close", {}))
        if self.pending is not None and not self.pending.done():
            self.pending.set_exception(ConnectionError("Connection closed"))
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture
def client(monkeypatch):
    rows = []
    monkeypatch.setattr(acp_client, "load_argv", lambda name: ["test-only.exe"])
    instance = acp_client.AcpClient("codex", SimpleNamespace(),
                                    lambda kind, data: rows.append({"kind": kind, **data}),
                                    lambda event: None, lambda reason: None)
    instance.conn = FakeConnection()
    instance.proc = SimpleNamespace(returncode=None, pid=123)
    instance.session_id = "native-unit-session"
    instance._usable = True

    async def stop(proc, record):
        proc.returncode = 0
        record("cleanup", {"pid": proc.pid, "forced": False})

    monkeypatch.setattr(acp_client, "stop_process", stop)
    return instance, rows


async def active_prompt(instance):
    wrapper = asyncio.create_task(instance.prompt("read only"))
    while instance.conn.pending is None:
        await asyncio.sleep(0)
    return wrapper


@pytest.mark.asyncio
async def test_async_permission_cleanup_is_awaited_for_cancel_prompt_and_close(client):
    instance, _ = client
    reasons = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def expire(reason):
        if reason == "cancel":
            entered.set()
            await release.wait()
        reasons.append(reason)

    instance.expire_permissions = expire
    prompt = await active_prompt(instance)
    cancellation = asyncio.create_task(instance.cancel())
    await asyncio.wait_for(entered.wait(), 3)
    assert not cancellation.done()
    assert not any(method == "cancel" for method, _ in instance.conn.calls)
    release.set()
    await cancellation
    await prompt
    assert reasons[:2] == ["cancel", "prompt_ended"]
    await asyncio.gather(instance.close(), instance.close())
    assert reasons.count("close") == 1


@pytest.mark.asyncio
async def test_process_exit_awaits_permission_cleanup_before_closing_protocol(client):
    instance, _ = client
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait():
        return 1

    async def expire(reason):
        assert reason == "process_exit"
        entered.set()
        await release.wait()

    instance.proc.wait = wait
    instance.expire_permissions = expire
    watch = asyncio.create_task(instance._watch_process())
    await asyncio.wait_for(entered.wait(), 3)
    assert not watch.done() and not instance.conn.calls
    release.set()
    await watch
    assert instance.conn.calls == [("close", {})]


@pytest.mark.asyncio
async def test_cancel_and_reuse_same_native_session(client):
    instance, rows = client
    wrapper = await active_prompt(instance)
    await instance.cancel()
    assert (await wrapper).stop_reason == "cancelled"
    assert instance.reusable
    assert [r for r in rows if r["kind"] == "cancel_result"][-1]["outcome"] == "cancelled"
    instance.conn.pending = None
    followup = await active_prompt(instance)
    instance.conn.pending.set_result(PromptResponse(stop_reason="end_turn"))
    await followup
    assert instance.session_id == "native-unit-session"
    await instance.close()


@pytest.mark.asyncio
async def test_finished_prompt_is_not_cancelled(client):
    instance, rows = client
    wrapper = await active_prompt(instance)
    instance.conn.pending.set_result(PromptResponse(stop_reason="end_turn"))
    await wrapper
    await instance.cancel()
    assert not any(name == "cancel" for name, _ in instance.conn.calls)
    assert rows[-1]["outcome"] == "completed_before_cancel"
    await instance.close()


@pytest.mark.asyncio
async def test_completion_racing_cancel_is_not_reported_as_cancelled(client):
    instance, rows = client
    instance.conn.cancel_response = "end_turn"
    wrapper = await active_prompt(instance)
    await instance.cancel()
    await wrapper
    assert [r for r in rows if r["kind"] == "cancel_result"][-1]["outcome"] == "completed_after_cancel"
    await instance.close()


@pytest.mark.asyncio
async def test_cancel_timeout_kills_owned_process_and_disables_reuse(client, monkeypatch):
    instance, rows = client
    instance.conn.cancel_response = None
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: real_timeout(0.02 if seconds == 5 else seconds))
    killed = []

    async def kill(proc, record):
        killed.append(proc)
        proc.returncode = 1

    monkeypatch.setattr(acp_client, "kill_process_tree", kill)
    wrapper = await active_prompt(instance)
    with pytest.raises(TimeoutError, match="旧连接不可复用"):
        await instance.cancel()
    await asyncio.gather(wrapper, return_exceptions=True)
    assert killed == [instance.proc]
    assert instance.forced and not instance.reusable
    assert instance.prompt_task.done()
    with pytest.raises(ConnectionError, match="不可复用"):
        await instance.prompt("must fail")
    await instance.close()
    assert len(killed) == 1
    assert any(row["kind"] == "cancel_timeout" for row in rows)


@pytest.mark.asyncio
async def test_close_is_idempotent_and_preserves_stderr_tail(client):
    instance, _ = client
    instance._process_record("stderr", {"text": "A" * 70000 + "尾部"})
    await instance.close()
    await instance.close()
    assert len(instance._stderr) == 65536
    assert instance.stderr_tail.endswith("尾部")
    assert [name for name, _ in instance.conn.calls].count("close") == 1


@pytest.mark.asyncio
async def test_cleanup_error_does_not_replace_original(client):
    instance, rows = client
    original = ValueError("original API failure")
    instance.conn.close_error = RuntimeError("secondary close failure")
    await instance._close_preserving(original, False)
    assert str(original) == "original API failure"
    assert "secondary close failure" in original.__notes__[0]
    assert any(row["kind"] == "cleanup_error" for row in rows)
    assert instance.proc.returncode == 0


@pytest.mark.asyncio
async def test_protocol_error_retains_code_data_and_identity(client):
    instance, rows = client
    wrapper = await active_prompt(instance)
    error = RequestError(-32603, "API failure", {"provider": "unit", "detail": "connection refused"})
    instance.conn.pending.set_exception(error)
    with pytest.raises(RequestError) as caught:
        await wrapper
    assert caught.value is error
    recorded = next(row for row in rows if row["kind"] == "prompt_error")
    assert recorded["protocol_error"] == error.to_error_obj()
    await instance.close()


@pytest.mark.asyncio
async def test_prompt_timeout_is_not_mislabeled_as_cancel_deadline(client):
    instance, rows = client
    instance.conn.cancel_response = None
    wrapper = await active_prompt(instance)
    error = TimeoutError("provider timeout, not host cancel deadline")
    instance.conn.pending.set_exception(error)
    with pytest.raises(TimeoutError) as caught:
        await instance.cancel()
    await asyncio.gather(wrapper, return_exceptions=True)
    assert caught.value is error and not instance.forced
    assert not any(row["kind"] == "cancel_timeout" for row in rows)


@pytest.fixture
def host(tmp_path):
    evidence = acp_smoke.Evidence("unit", tmp_path)
    instance = acp_smoke.Host(evidence, False)
    yield instance
    evidence.stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("exits", [True, False])
async def test_taskkill_nonzero_waits_for_actual_process_exit(monkeypatch, exits):
    rows = []
    proc = SimpleNamespace(returncode=None, pid=123)

    async def wait():
        await asyncio.sleep(0)
        if not exits:
            raise TimeoutError("主进程未退出")
        proc.returncode = 1
        return 1

    async def communicate():
        return b"root terminated", b"child already exiting"

    async def spawn(*args, **kwargs):
        return SimpleNamespace(returncode=128, communicate=communicate)

    proc.wait = wait
    monkeypatch.setattr(acp_client, "os", SimpleNamespace(name="nt", environ={"SYSTEMROOT": "C:/Windows"}))
    monkeypatch.setattr(acp_client.asyncio, "create_subprocess_exec", spawn)
    record = lambda kind, data: rows.append({"kind": kind, **data})
    if exits:
        await acp_client.kill_process_tree(proc, record)
        assert rows[-1]["kind"] == "cleanup" and proc.returncode == 1
    else:
        with pytest.raises(RuntimeError, match="taskkill 失败"):
            await acp_client.kill_process_tree(proc, record)
        assert not any(row["kind"] == "cleanup" for row in rows)


@pytest.mark.asyncio
async def test_handshake_scenario_never_submits_prompt(host, monkeypatch, tmp_path):
    calls = []

    async def connect(cwd, servers):
        calls.append("connect")

    async def close():
        calls.append("close")

    instance = SimpleNamespace(connect=connect, close=close)
    monkeypatch.setattr(acp_smoke, "make_client", lambda *args: (instance, host))
    args = SimpleNamespace(harness="codebuddy", scenario="handshake", model=None)
    await acp_smoke.conversation(args, tmp_path, host.evidence)
    assert calls == ["connect", "close"]


@pytest.mark.parametrize("extra,failure,passed", [
    ([], "", True),
    ([{"kind": "cleanup_error"}], "", False),
    ([{"kind": "prompt_input"}], "", False),
    ([], "initialize failed", False),
])
def test_handshake_acceptance_requires_clean_close_and_no_prompt(host, extra, failure, passed):
    host.evidence.rows = [{"kind": "initialize"}, {"kind": "session", "native_id": "codebuddy-id"}, *extra]
    snapshot = {"head": "same-sha", "status": "", "diff": ""}
    assert acp_smoke.summarize(host.evidence, "handshake", snapshot, snapshot, failure)["passed"] is passed


def permission_options():
    return [PermissionOption(option_id="opaque-allow-ID", name="允许本次", kind="allow_once"),
            PermissionOption(option_id="opaque-reject-ID", name="拒绝本次", kind="reject_once")]


@pytest.mark.parametrize("choice,expected", [("0", "opaque-allow-ID"), ("1", "opaque-reject-ID")])
@pytest.mark.asyncio
async def test_permission_preserves_original_option_id(host, monkeypatch, choice, expected):
    async def read():
        return choice

    monkeypatch.setattr(acp_smoke, "terminal_line", read)
    response = await host.request_permission("native", ToolCallUpdate(tool_call_id="tool-original"),
                                             permission_options())
    assert response.outcome.outcome == "selected"
    assert response.outcome.option_id == expected
    request = next(row for row in host.evidence.rows if row["kind"] == "permission_request")
    assert request["request_id"] != "tool-original"
    assert request["options"][1]["kind"] == "reject_once"
    assert not host.pending


@pytest.mark.asyncio
async def test_invalid_permission_choice_reprompts_and_empty_cancels(host, monkeypatch):
    choices = iter(["-1", "99", "invalid", ""])

    async def read():
        return next(choices)

    monkeypatch.setattr(acp_smoke, "terminal_line", read)
    response = await host.request_permission("native", ToolCallUpdate(tool_call_id="tool"), permission_options())
    assert response.outcome.outcome == "cancelled"
    assert len([r for r in host.evidence.rows if r["kind"] == "permission_invalid_input"]) == 3


@pytest.mark.asyncio
async def test_expiring_permission_cancels_reader_without_stale_answer(host, monkeypatch):
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def read():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(acp_smoke, "terminal_line", read)
    pending = asyncio.create_task(host.request_permission(
        "native", ToolCallUpdate(tool_call_id="tool"), permission_options(),
    ))
    await entered.wait()
    host.expire_permissions("cancel")
    response = await asyncio.wait_for(pending, 1)
    assert response.outcome.outcome == "cancelled" and stopped.is_set()
    assert not host.pending


@pytest.mark.asyncio
async def test_strict_smoke_never_prompts_for_permission(host, monkeypatch):
    host.strict = True

    async def forbidden():
        pytest.fail("strict smoke must not ask for authorization")

    monkeypatch.setattr(acp_smoke, "terminal_line", forbidden)
    response = await host.request_permission("native", ToolCallUpdate(tool_call_id="tool"), permission_options())
    assert response.outcome.outcome == "cancelled" and host.abort.is_set()


@pytest.mark.asyncio
async def test_real_pipe_input_is_nonblocking_and_restores_descriptor(monkeypatch):
    reader, writer = os.pipe()
    stream = os.fdopen(reader, "rb", buffering=0)
    monkeypatch.setattr(acp_smoke.sys, "stdin", stream)
    try:
        task = asyncio.create_task(acp_smoke.terminal_line())
        await asyncio.sleep(0.02)
        assert not task.done()
        os.write(writer, b"2\n")
        assert await asyncio.wait_for(task, 1) == "2"
        assert os.get_blocking(reader)
        os.close(writer)
        writer = None
        assert await acp_smoke.terminal_line() == ""
    finally:
        stream.close()
        if writer is not None:
            os.close(writer)


@pytest.mark.parametrize("decision,exists,content,passed", [
    ("allow_once", False, None, False),
    ("allow_once", True, "wrong content", False),
    ("allow_once", True, "ACP_PERMISSION_OK", True),
    ("reject_once", True, "ACP_PERMISSION_OK", False),
    ("reject_once", False, None, True),
])
def test_permission_acceptance_requires_actual_disk_result(host, decision, exists, content, passed):
    host.evidence.rows = [
        {"kind": "permission_request"},
        {"kind": "permission_selected", "option_kind": decision},
        {"kind": "prompt_response"},
        {"kind": "probe_after", "exists": exists, "content": content},
    ]
    before = {"head": "same-sha", "status": "", "diff": ""}
    after = {**before, "status": "?? acp_permission_probe.txt\n" if exists else ""}
    summary = acp_smoke.summarize(host.evidence, "permission", before, after, "")
    assert summary["passed"] is passed
