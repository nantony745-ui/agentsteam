import asyncio
import json
import os
import sys
import time
import tomllib
from copy import deepcopy
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from inspect import isawaitable
from pathlib import Path
from typing import Any, get_args

from acp import Client, PROTOCOL_VERSION, connect_to_agent
from acp.connection import StreamDirection, StreamObserver
from acp.exceptions import RequestError
from acp.schema import ClientCapabilities, EnvVariable, Implementation, McpServerStdio, PromptResponse, TextContentBlock

from app.domain.models import Harness
from app.db.event_codec import business_payload

CONFIG_PATH = Path(__file__).resolve().parents[2] / "agents.toml"
Record = Callable[[str, dict[str, Any]], None]


def build_mcp_bridge(base_url: str, token: str, role: str) -> McpServerStdio:
    harnesses = [name for name, spec in load_agents().items()
                 if spec["enabled"] and name in get_args(Harness)]
    return McpServerStdio(
        name="harness_lab", command=str(Path(sys.executable).resolve()),
        args=[str(Path(__file__).resolve().parents[1] / "mcp" / "bridge.py")],
        env=[EnvVariable(name="HARNESS_LAB_URL", value=base_url),
             EnvVariable(name="HARNESS_LAB_TOKEN", value=token),
             EnvVariable(name="HARNESS_LAB_ROLE", value=role),
             EnvVariable(name="HARNESS_LAB_HARNESSES", value=json.dumps(harnesses))],
    )


def session_model_id(message: dict[str, Any]) -> str | None:
    """读取 ACP 会话响应或配置通知中的当前模型，不从可选模型列表推断。"""
    if message.get("method") == "session/update":
        data = message.get("params", {}).get("update", {})
        if data.get("sessionUpdate") != "config_option_update":
            return None
    elif message.get("method") is None:
        data = message.get("result")
    else:
        return None
    if not isinstance(data, dict):
        return None
    for option in data.get("configOptions") or []:
        if (option.get("category") == "model" and option.get("id") != "reasoning"
                and option.get("type") == "select"):
            value = option.get("currentValue")
            if isinstance(value, str) and value.strip():
                return value
    value = (data.get("models") or {}).get("currentModelId")
    return value if isinstance(value, str) and value.strip() else None


def normalize_update(message: dict[str, Any]) -> dict[str, Any]:
    """接收验证前的 StreamObserver 消息，避免 SDK 丢弃未知类型和原始字段。"""
    raw = message
    kind, payload = "raw", {}
    method = raw.get("method")
    if method == "session/update":
        update = raw["params"]["update"]
        update_type = update.get("sessionUpdate")
        if update_type in {"agent_message_chunk", "agent_thought_chunk"}:
            content = update["content"]
            if content.get("type") == "text":
                kind = "agent.text"
                payload = {"text": content["text"],
                           "channel": "message" if update_type == "agent_message_chunk" else "thought"}
        elif update_type in {"tool_call", "tool_call_update"}:
            kind, payload = "agent.tool", dict(update)
        elif update_type in {"plan", "plan_update", "plan_removed"}:
            kind, payload = "agent.plan", dict(update)
        elif update_type == "session_info_update":
            title = update.get("title")
            if isinstance(title, str) and title.strip():
                kind, payload = "session.title", {"title": " ".join(title.split())}
    elif method == "session/request_permission":
        payload = {**raw["params"], "request_id": raw["id"]}
    elif method is None and "error" in raw:
        kind, payload = "error", {"error": raw["error"], "request_id": raw.get("id")}
    model_id = session_model_id(raw)
    if model_id is not None:
        kind, payload = "session.model", {"model_id": model_id}
    if kind == "agent.text":
        metadata = {key: value for key, value in update.items() if key not in {"content", "sessionUpdate"}}
        metadata.update({key: value for key, value in content.items() if key not in {"text", "type"}})
        if metadata:
            payload["metadata"] = metadata
    if kind == "raw" and not payload:
        if method == "session/update":
            payload = dict(update)
        else:
            payload = raw.get("params", raw.get("result", {}))
            payload = dict(payload) if isinstance(payload, dict) else {"data": payload}
            if method is not None:
                payload["method"] = method
    return {"kind": kind, "payload": business_payload(kind, payload)}


def project_events(events: Iterable[dict[str, Any]]) -> dict[str, dict[tuple[str, str], Any]]:
    """从持久化事件重建展示值；保留事件本身，不把工具补丁改写成历史快照。"""
    tools: dict[tuple[str, str], dict[str, Any]] = {}
    texts: dict[tuple[str, str], str] = {}
    for event in sorted(events, key=lambda item: item["id"]):
        if event["kind"] not in {"agent.tool", "agent.text"}:
            continue
        payload = json.loads(event["payload"])
        if event["kind"] == "agent.tool":
            key = (event["session_id"], payload["toolCallId"])
            tool = tools.setdefault(key, {})
            tool.update({field: value for field, value in payload.items()
                         if field not in {"raw", "sessionUpdate"} and value is not None})
        else:
            key = (event["run_id"], payload["channel"])
            texts[key] = texts.get(key, "") + payload["text"]
    return {"tools": tools, "texts": texts}


def error_details(error: BaseException) -> dict[str, Any]:
    details = {"error": repr(error), "error_type": type(error).__name__,
               "notes": getattr(error, "__notes__", [])}
    if isinstance(error, RequestError):
        details["protocol_error"] = error.to_error_obj()
    return details


def load_agents() -> dict[str, Any]:
    return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))["agents"]


def load_argv(name: str) -> list[str]:
    spec = load_agents()[name]
    if not spec["enabled"]:
        raise ValueError(f"Harness 未启用: {name}")
    return [spec["executable"], *spec["args"]]


def load_env(name: str) -> dict[str, str]:
    return {**os.environ, **load_agents()[name].get("env", {})}


async def drain_stderr(reader: asyncio.StreamReader, record: Record) -> None:
    while chunk := await reader.read(4096):
        record("stderr", {"text": chunk.decode("utf-8", "replace")})


async def stop_process(proc: asyncio.subprocess.Process, record: Record) -> None:
    if proc.returncode is not None:
        await proc.wait()
        record("process_reaped", {"pid": proc.pid, "returncode": proc.returncode})
        return
    if proc.stdin is not None:
        proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), 5)
        record("cleanup", {"pid": proc.pid, "returncode": proc.returncode, "forced": False})
        return
    except TimeoutError:
        record("cleanup_escalation", {"pid": proc.pid})
    await kill_process_tree(proc, record)


async def kill_process_tree(proc: asyncio.subprocess.Process, record: Record) -> None:
    if proc.returncode is not None:
        await proc.wait()
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            str(Path(os.environ["SYSTEMROOT"]) / "System32/taskkill.exe"),
            "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000,
        )
        output, error = await asyncio.wait_for(killer.communicate(), 5)
        record("taskkill", {"returncode": killer.returncode,
                            "stdout": output.decode("oem", "replace"),
                            "stderr": error.decode("oem", "replace")})
        if killer.returncode:
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError as error:
                raise RuntimeError(f"taskkill 失败，退出码 {killer.returncode}，PID {proc.pid}") from error
    else:
        proc.kill()
    await asyncio.wait_for(proc.wait(), 5)
    record("cleanup", {"pid": proc.pid, "returncode": proc.returncode, "forced": True})


async def spawn_process(argv: list[str], cwd: Path, record: Record,
                        env: dict[str, str]) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    record("spawn", {"argv": argv, "cwd": str(cwd), "pid": proc.pid})
    return proc


@asynccontextmanager
async def running_process(name: str, cwd: Path, record: Record) -> AsyncIterator[asyncio.subprocess.Process]:
    proc = await spawn_process(load_argv(name), cwd, record, load_env(name))
    drain = asyncio.create_task(drain_stderr(proc.stderr, record))
    try:
        yield proc
    finally:
        try:
            await stop_process(proc, record)
        finally:
            try:
                await asyncio.wait_for(drain, 5)
            except TimeoutError:
                record("cleanup_error", {"message": "stderr 未在 5 秒内 EOF，读取任务已取消"})


class AcpClient:
    def __init__(self, harness: str, host: Client, record: Record,
                 observe: StreamObserver, expire_permissions: Callable[[str], Awaitable[None] | None]) -> None:
        self.harness = harness
        self.argv = load_argv(harness)
        self.env = load_env(harness)
        self.host = host
        self.record = record
        self.observe = observe
        self.expire_permissions = expire_permissions
        self.proc: asyncio.subprocess.Process | None = None
        self.conn = None
        self.session_id: str | None = None
        self.prompt_task: asyncio.Task[PromptResponse] | None = None
        self._drain: asyncio.Task[None] | None = None
        self._watch: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._cancel_lock = asyncio.Lock()
        self._usable = False
        self._stderr = bytearray()
        self.forced = False
        self.config_options: list[dict[str, Any]] = []
        self.modes: dict[str, Any] | None = None
        self.config_defaults: dict[str, str] = {}
        self.launch_values: dict[str, str] = {}
        self.launch_defaults: dict[str, str] = {}

    def _observe(self, event) -> None:
        if event.direction == StreamDirection.INCOMING:
            message = event.message
            if message.get("method") == "session/update":
                data = message.get("params", {}).get("update", {})
            else:
                data = message.get("result") or {}
            self._capture_config(data)
        self.observe(event)

    def _capture_config(self, data: dict[str, Any]) -> None:
        if "configOptions" in data:
            self.config_options = deepcopy(data["configOptions"] or [])
        if "modes" in data:
            self.modes = deepcopy(data["modes"])
        if data.get("sessionUpdate") == "current_mode_update" and self.modes:
            self.modes["currentModeId"] = data["currentModeId"]

    @property
    def reusable(self) -> bool:
        return self._usable and self.proc is not None and self.proc.returncode is None

    @property
    def stderr_tail(self) -> str:
        return self._stderr.decode("utf-8", "replace")

    def _process_record(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "stderr":
            self._stderr.extend(data["text"].encode("utf-8"))
            del self._stderr[:-65536]
        if kind == "cleanup" and data["forced"]:
            self.forced = True
        self.record(kind, data)

    async def connect(self, cwd: Path, mcp_servers: list[Any]) -> str:
        if self.proc is not None or self._close_task is not None:
            raise RuntimeError("客户端实例不能重新连接；请新建 AcpClient")
        try:
            self.proc = await spawn_process(self.argv, cwd, self.record, self.env)
            self._drain = asyncio.create_task(drain_stderr(self.proc.stderr, self._process_record))
            self.conn = connect_to_agent(self.host, self.proc.stdin, self.proc.stdout,
                                         observers=[self._observe])
            self._watch = asyncio.create_task(self._watch_process())
            await self._handshake(cwd, mcp_servers)
            self._usable = True
            return self.session_id
        except BaseException as error:
            self.record("connect_error", error_details(error))
            await self._close_preserving(error, False)
            raise

    async def _handshake(self, cwd: Path, mcp_servers: list[Any]) -> None:
        self.record("stage", {"stage": "initialize", "status": "started"})
        async with asyncio.timeout(60):
            initialized = await self.conn.initialize(
                protocol_version=PROTOCOL_VERSION, client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="agentsteam", version="0.1.0"),
            )
        self.record("initialize", initialized.model_dump(mode="json", by_alias=True))
        self.record("stage", {"stage": "session/new", "status": "started"})
        async with asyncio.timeout(60):
            session = await self.conn.new_session(cwd=str(cwd), mcp_servers=mcp_servers)
        self.session_id = session.session_id
        self._capture_config(session.model_dump(mode="json", by_alias=True))
        self.config_defaults = {item["id"]: item["currentValue"] for item in self.config_options
                                if isinstance(item.get("currentValue"), str)}
        if self.modes:
            self.config_defaults["session_mode"] = self.modes["currentModeId"]
        self.config_defaults.update(self.launch_defaults)
        self.record("session", {"native_id": self.session_id,
                                "response": session.model_dump(mode="json", by_alias=True)})
        if not self.session_id:
            raise RuntimeError("新会话缺少 session_id")

    async def _watch_process(self) -> None:
        code = await self.proc.wait()
        self._usable = False
        self.record("process_exit", {"pid": self.proc.pid, "returncode": code})
        try:
            await self._expire_permissions("process_exit")
        finally:
            await self.conn.close()

    async def _expire_permissions(self, reason: str) -> None:
        result = self.expire_permissions(reason)
        if isawaitable(result):
            await result

    async def configure(self, config_id: str, value: str) -> None:
        self._require_ready()
        self.record("config_selection", {"config_id": config_id, "value": value})
        async with asyncio.timeout(60):
            result = await self.conn.set_config_option(
                config_id=config_id, session_id=self.session_id, value=value,
            )
        self.record("config_selected", result.model_dump(mode="json", by_alias=True))
        self._capture_config(result.model_dump(mode="json", by_alias=True))

    async def configure_mode(self, value: str) -> None:
        self._require_ready()
        async with asyncio.timeout(60):
            await self.conn.set_session_mode(session_id=self.session_id, mode_id=value)
        self.modes["currentModeId"] = value

    def _require_ready(self) -> None:
        if not self.reusable:
            raise ConnectionError("连接已关闭或不可复用；请新建 ACP session")

    async def prompt(self, text: str) -> PromptResponse:
        self._require_ready()
        if self.prompt_task is not None and not self.prompt_task.done():
            raise RuntimeError("同一 ACP 连接已有 prompt 正在执行")
        self.record("prompt_input", {"session_id": self.session_id, "text": text})
        task = asyncio.create_task(self.conn.prompt(
            session_id=self.session_id, prompt=[TextContentBlock(type="text", text=text)],
        ))
        self.prompt_task = task
        try:
            response = await asyncio.shield(task)
            self.record("prompt_response", {"session_id": self.session_id,
                                            **response.model_dump(mode="json", by_alias=True)})
            return response
        except BaseException as error:
            if isinstance(error, ConnectionError):
                self._usable = False
            self.record("prompt_error", {"session_id": self.session_id, **error_details(error)})
            raise
        finally:
            await self._expire_permissions("prompt_ended")

    async def cancel(self) -> None:
        async with self._cancel_lock:
            task = self.prompt_task
            if task is None or task.done():
                self.record("cancel_result", {"outcome": "completed_before_cancel"})
                return
            self._require_ready()
            started = time.monotonic()
            self.record("cancel_requested", {"session_id": self.session_id})
            await self._expire_permissions("cancel")
            deadline = asyncio.timeout(5)
            try:
                async with deadline:
                    await self.conn.cancel(session_id=self.session_id)
                    response = await asyncio.shield(task)
            except TimeoutError as error:
                if not deadline.expired():
                    self.record("cancel_error", error_details(error))
                    await self._close_preserving(error, False)
                    raise
                self.record("cancel_timeout", {"elapsed": time.monotonic() - started})
                await self._close_preserving(error, True)
                raise TimeoutError("ACP cancel 五秒未结束，已执行受控清理；旧连接不可复用") from error
            except BaseException as error:
                self.record("cancel_error", error_details(error))
                await self._close_preserving(error, False)
                raise
            self.record("cancel_result", {
                "outcome": "cancelled" if response.stop_reason == "cancelled" else "completed_after_cancel",
                "elapsed": time.monotonic() - started, "stop_reason": response.stop_reason,
                "reusable": self.reusable,
            })

    async def terminate_owned_process(self, reason: str) -> None:
        self._usable = False
        await self._expire_permissions(reason)
        if self.proc is not None and self.proc.returncode is None:
            self.forced = True
            self.record("force_terminate", {"pid": self.proc.pid, "reason": reason})
            await kill_process_tree(self.proc, self.record)

    async def _close_preserving(self, original: BaseException, force: bool) -> None:
        try:
            if force:
                await self.terminate_owned_process("cancel_timeout")
        except Exception as cleanup:
            original.add_note(f"进程树清理失败: {cleanup!r}")
            self.record("cleanup_error", {"stage": "force_terminate", "error": repr(cleanup)})
        try:
            await self.close()
        except Exception as cleanup:
            original.add_note(f"关闭失败: {cleanup!r}")

    async def close(self) -> None:
        if self._close_task is None:
            self._usable = False
            self._close_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._close_task)

    async def _shutdown(self) -> None:
        errors = []
        steps = [("permissions", lambda: self._expire_permissions("close"))]
        if self.conn is not None:
            steps.append(("protocol", self.conn.close))
        if self.proc is not None:
            steps.append(("process", lambda: stop_process(self.proc, self._process_record)))
        for stage, action in steps:
            try:
                async with asyncio.timeout(12 if stage == "process" else 5):
                    await action()
            except Exception as error:
                errors.append(error)
                self.record("cleanup_error", {"stage": stage, "error": repr(error)})
        for label, task in (("prompt", self.prompt_task), ("stderr", self._drain), ("watch", self._watch)):
            if task is None:
                continue
            try:
                async with asyncio.timeout(5):
                    if label == "prompt" and not task.done():
                        task.cancel()
                    await task
            except asyncio.CancelledError:
                if label != "prompt":
                    raise
            except Exception as error:
                self.record("task_exit", {"task": label, **error_details(error)})
                if label != "prompt":
                    errors.append(error)
        self.record("closed", {"reusable": False, "forced": self.forced,
                               "returncode": self.proc.returncode if self.proc else None,
                               "stderr_tail": self.stderr_tail})
        if errors:
            raise ExceptionGroup("ACP 资源清理失败", errors)
