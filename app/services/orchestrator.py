"""应用生命周期持有的 Session FIFO；原始 ACP 通知只经串行日志写入一次。"""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from acp import Client
from acp.connection import StreamDirection, StreamEvent
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse, EnvVariable

from app.agents.acp_client import AcpClient, build_mcp_bridge, load_argv, normalize_update, session_model_id
from app.agents.harness_config import PERMISSION_KEYS, actual_values, apply_values, automatic_permission, launch_settings
from app.services.run_journal import JournalBudget, RunJournal
from app.domain.models import MessageCreate, RunStatus, TaskCreate
from app.db.storage import PermissionConflictError, RUN_TERMINALS, Storage
from app.delivery.git_delivery import WorkingDirectoryError, selected_worktree, validate_worktree


logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {RunStatus.RUNNING, RunStatus.WAITING_PERMISSION, RunStatus.CANCEL_REQUESTED}
ClientFactory = Callable[..., AcpClient]


class SessionUnavailableError(ValueError):
    """该会话的活动连接已丢失，不能用新连接替换历史身份。"""


@dataclass
class Execution:
    run: dict[str, Any]
    journal: RunJournal
    started: asyncio.Event = field(default_factory=asyncio.Event)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    connection: asyncio.Task | None = None
    prompt: asyncio.Task | None = None
    task: asyncio.Task | None = None
    cancel_task: asyncio.Task | None = None
    cancel_error: Exception | None = None
    cancel_requested: bool = False
    shutdown: bool = False
    permission_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    permissions_closed: bool = False
    settings: dict = field(default_factory=dict)


@dataclass
class PendingPermission:
    execution: Execution
    future: asyncio.Future[RequestPermissionResponse | dict[str, Any]]
    kind: str = "permission"


@dataclass(frozen=True)
class CallerIdentity:
    session_id: str
    role: Literal["coordinator", "worker"]


@dataclass
class SessionRuntime:
    session: dict[str, Any]
    queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)
    admission: asyncio.Lock = field(default_factory=asyncio.Lock)
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    prompt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    client: AcpClient | None = None
    dispatcher: asyncio.Task | None = None
    active: Execution | None = None
    unavailable: bool = False
    diagnostics: Any = None
    mcp_token: str | None = field(default=None, repr=False)
    config_overrides: dict[str, str] = field(default_factory=dict)
    automatic_approval: bool = True

    def redact(self, value: Any) -> Any:
        if self.mcp_token is None:
            return value
        if isinstance(value, str):
            return value.replace(self.mcp_token, "[REDACTED]")
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        return value

    def observe(self, event: StreamEvent) -> None:
        if self.diagnostics is not None:
            self.diagnostics.record(str(event.direction), self.redact(event.message))
        if event.direction != StreamDirection.INCOMING or self.active is None:
            return
        message = self.redact(event.message)
        if (message.get("method") in {"session/update", "session/request_permission"}
                or "error" in message or session_model_id(message) is not None):
            self.active.journal.receive(normalize_update(message))


class SessionHost(Client):
    def __init__(self, runtime: SessionRuntime, service: "Orchestrator") -> None:
        self.runtime = runtime
        self.service = service

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        # observer 保存验证前的 raw；这里不能重复保存 SDK 已解析的同一通知。
        return None

    async def request_permission(self, session_id: str, tool_call: Any,
                                 options: list[Any], **kwargs: Any) -> RequestPermissionResponse:
        execution = self.runtime.active
        if execution is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return await self.service.request_permission(execution, tool_call, options, kwargs)

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any) -> dict:
        execution = self.runtime.active
        if execution is None or not hasattr(mode, "requested_schema"):
            return {"action": "cancel"}
        if getattr(mode, "session_id", self.runtime.client.session_id) != self.runtime.client.session_id:
            return {"action": "cancel"}
        payload = {"message": message, "schema": mode.requested_schema.model_dump(mode="json", by_alias=True, exclude_none=True)}
        return await self.service.request_question(execution, payload)


class Orchestrator:
    def __init__(self, storage: Storage, client_factory: ClientFactory, internal_base_url: str) -> None:
        self.storage = storage
        self.client_factory = client_factory
        self.internal_base_url = internal_base_url
        self.tokens: dict[str, CallerIdentity] = {}
        self.sessions: dict[str, SessionRuntime] = {}
        self.permissions: dict[str, PendingPermission] = {}
        self.worker_queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker_dispatcher: asyncio.Task | None = None
        self.worker_tasks: dict[str, asyncio.Task] = {}
        self.worker_waiters: dict[str, asyncio.Future[None]] = {}
        self.delegation_admission = asyncio.Lock()
        self.deletion_lock = asyncio.Lock()
        self.deletions: set[asyncio.Task] = set()
        self.deleting_sessions: set[str] = set()
        self.closing = False
        self.journal_budget = JournalBudget()
        self.diagnostics = None
        self.memory = None
        self.employees = None
        self.teams = None

    async def start(self) -> None:
        if self.worker_dispatcher is None:
            await self.storage.expire_orphan_questions()
            self.worker_dispatcher = asyncio.create_task(self._worker_loop(), name="worker-dispatcher")

    async def _worker_loop(self) -> None:
        while True:
            run_id = await self.worker_queue.get()
            if run_id in self.worker_tasks or run_id not in self.worker_waiters:
                self.worker_queue.task_done()
                continue
            self.worker_tasks[run_id] = asyncio.create_task(self._run_worker(run_id), name=f"worker:{run_id}")

    async def _run_worker(self, run_id: str) -> None:
        waiter = self.worker_waiters[run_id]
        try:
            await self.execute_run(run_id)
        except asyncio.CancelledError:
            waiter.cancel()
            raise
        except Exception as error:
            waiter.set_exception(error)
        else:
            waiter.set_result(None)
        finally:
            self.worker_tasks.pop(run_id)
            self.worker_waiters.pop(run_id)
            self.worker_queue.task_done()

    async def request_permission(self, execution: Execution, tool_call: Any,
                                 options: list[Any], meta: dict[str, Any]) -> RequestPermissionResponse:
        if self.closing or execution.permissions_closed or execution.cancel_requested or not execution.journal.accepting:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        selected = next((option for kind in ("allow_always", "allow_once")
                         for option in options if option.kind == kind), None)
        permission_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        payload = {"tool_call": tool_call.model_dump(mode="json", by_alias=True),
                   "options": [option.model_dump(mode="json", by_alias=True) for option in options],
                   "meta": meta}
        payload = self.sessions[execution.run["session_id"]].redact(payload)
        await execution.journal.flush()
        try:
            async with execution.permission_lock:
                if self.closing or execution.permissions_closed or execution.cancel_requested:
                    return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
                self.permissions[permission_id] = PendingPermission(execution, future)
                await execution.journal.call(lambda: self.storage.create_permission(execution.run["id"], permission_id, payload))
            automatic = self.sessions[execution.run["session_id"]].automatic_approval
            if not automatic:
                return await asyncio.shield(future)
            if selected is None:
                return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
            await self.answer_permission(permission_id, selected.option_id)
            return await asyncio.shield(future)
        except PermissionConflictError:
            if future.done():
                return future.result()
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        finally:
            if permission_id in self.permissions:
                await self._expire_permissions(execution, "callback_ended", permission_id)

    async def answer_permission(self, permission_id: str, option_id: str) -> dict[str, Any]:
        pending = self.permissions.get(permission_id)
        if pending is None:
            if await self.storage.get_interaction(permission_id) is None:
                raise KeyError(permission_id)
            raise PermissionConflictError("权限已经解决或不属于当前进程的活动连接")
        async with pending.execution.permission_lock:
            if pending.kind != "permission":
                raise ValueError("此请求需要填写问题表单")
            execution = pending.execution
            if (self.closing or execution.permissions_closed or execution.cancel_requested
                    or permission_id not in self.permissions):
                raise PermissionConflictError("权限请求已取消或过期")
            try:
                interaction = await execution.journal.call(lambda: self.storage.answer_permission(permission_id, option_id))
            except asyncio.CancelledError:
                interaction = await self.storage.get_interaction(permission_id)
                if interaction["status"] == "answered":
                    self._answer_future(permission_id, json.loads(interaction["response"])["option_id"])
                raise
            self._answer_future(permission_id, option_id)
            return interaction

    async def request_question(self, execution: Execution, payload: dict) -> dict:
        question_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        await execution.journal.flush()
        try:
            async with execution.permission_lock:
                if self.closing or execution.permissions_closed or execution.cancel_requested:
                    return {"action": "cancel"}
                self.permissions[question_id] = PendingPermission(execution, future, "question")
                await execution.journal.call(lambda: self.storage.create_permission(
                    execution.run["id"], question_id, payload, "question"))
            return await asyncio.shield(future)
        finally:
            if question_id in self.permissions:
                await self._expire_permissions(execution, "callback_ended", question_id)

    async def answer_question(self, question_id: str, response: dict) -> dict:
        pending = self.permissions.get(question_id)
        if pending is None or pending.kind != "question":
            raise PermissionConflictError("问题已结束或不属于当前活动连接")
        execution = pending.execution
        async with execution.permission_lock:
            if self.closing or execution.permissions_closed or execution.cancel_requested or question_id not in self.permissions:
                raise PermissionConflictError("问题已取消或过期")
            try:
                result = await execution.journal.call(lambda: self.storage.answer_question(question_id, response))
            except asyncio.CancelledError:
                result = await self.storage.get_interaction(question_id)
                if result["status"] == "answered":
                    self.permissions.pop(question_id).future.set_result(json.loads(result["response"]))
                raise
            self.permissions.pop(question_id).future.set_result(response)
            return result

    def _answer_future(self, permission_id: str, option_id: str) -> None:
        pending = self.permissions.pop(permission_id)
        pending.future.set_result(RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option_id),
        ))

    async def _expire_permissions(self, execution: Execution, reason: str,
                                  permission_id: str | None = None) -> None:
        async with execution.permission_lock:
            if permission_id is not None and permission_id not in self.permissions:
                return
            if permission_id is None:
                execution.permissions_closed = True
            if execution.journal.write_error is None and not execution.journal.task.done():
                await execution.journal.call(lambda: self.storage.expire_permissions(execution.run["id"], reason, permission_id))
            else:
                await self.storage.expire_permissions(execution.run["id"], reason, permission_id)
            ids = [key for key, pending in self.permissions.items()
                   if pending.execution is execution and (permission_id is None or key == permission_id)]
            for key in ids:
                pending = self.permissions.pop(key)
                pending.future.set_result({"action": "cancel"} if pending.kind == "question" else
                                          RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled")))

    async def expire_permissions(self, run_id: str, reason: str) -> None:
        for runtime in self.sessions.values():
            execution = runtime.active
            if execution is not None and execution.run["id"] == run_id:
                await self._expire_permissions(execution, reason)
                return

    async def _expire_session_permissions(self, runtime: SessionRuntime, reason: str) -> None:
        if (reason in {"close", "process_exit", "cancel_timeout"}
                or (runtime.client is not None and not runtime.client.reusable)):
            self._revoke_token(runtime)
        execution = runtime.active
        if execution is not None:
            await self._expire_permissions(execution, reason)

    async def _runtime(self, session_id: str) -> SessionRuntime:
        async with self.delegation_admission:
            session = await self.storage.get_session(session_id)
            if session is None:
                raise KeyError(session_id)
            if session_id not in self.sessions:
                self.sessions[session_id] = SessionRuntime(session, unavailable=session["native_id"] is not None)
            return self.sessions[session_id]

    async def submit(self, session_id: str, data: MessageCreate) -> dict[str, Any]:
        runtime = await self._runtime(session_id)
        if data.collaboration_mode is not None and runtime.session["harness"] != "codex":
            raise ValueError("此工作模式仅适用于 Codex")
        async with runtime.admission:
            if runtime.active is None and runtime.client is not None and not runtime.client.reusable:
                runtime.unavailable = True
            if runtime.dispatcher is not None and runtime.dispatcher.done():
                runtime.unavailable = True
            if self.closing or runtime.unavailable:
                raise SessionUnavailableError("服务正在关闭或会话连接不可复用，请新建 Session")
            run, created = await self.storage.create_run(session_id, data)
            if created:
                self._enqueue_run(runtime, run)
            return run

    def _enqueue_run(self, runtime: SessionRuntime, run: dict[str, Any]) -> None:
        runtime.queue.put_nowait(run)
        if runtime.dispatcher is None:
            runtime.dispatcher = asyncio.create_task(self._dispatch(runtime), name=f"session:{runtime.session['id']}")

    async def delegate_task(self, parent_session_id: str, data: TaskCreate) -> dict[str, Any]:
        async with self.delegation_admission:
            if self.closing or parent_session_id in self.deleting_sessions:
                raise SessionUnavailableError("服务正在关闭或会话正在删除，不能创建委派")
            association = await self.teams.association(parent_session_id) if self.teams else None
            if association or data.employee_id:
                async with self.employees.admission:
                    return await self._delegate_employee_task(parent_session_id, data, association)
            try:
                load_argv(data.harness)
            except ValueError as error:
                raise SessionUnavailableError(str(error)) from error
            task, child, run, created = await self.storage.create_task_with_run(parent_session_id, data)
            if created:
                runtime = SessionRuntime(child)
                self.sessions[child["id"]] = runtime
                self._enqueue_run(runtime, run)
            return {"task_id": task["id"], "child_session_id": task["child_session_id"],
                    "run_id": task["current_run_id"], "status": task["status"]}

    async def _delegate_employee_task(self, parent_id, data, association):
        parent = await self.storage.get_session(parent_id)
        if parent is None:
            raise KeyError(parent_id)
        if parent["parent_id"] or not association:
            raise PermissionError("仅团队队长可委派给员工")
        team = await self.teams.project(parent["project_id"])
        if not data.employee_id or data.employee_id not in team["member_ids"] or data.employee_id == team["leader_id"]:
            raise PermissionError("请选择本团队的其他员工")
        previous = await self.storage._fetch_one("SELECT * FROM tasks WHERE parent_session_id=? AND request_id=?",
                                                (parent_id, data.request_id))
        if previous:
            child = await self.storage.get_session(previous["child_session_id"])
            resolved = data.model_copy(update={"harness": child["harness"]})
            task, child, run, _ = await self.storage.create_task_with_run(parent_id, resolved)
            return {"task_id": task["id"], "child_session_id": child["id"], "run_id": run["id"], "status": task["status"]}
        try:
            self.employees.get(team["leader_id"])
            profile = self.teams.profiles([data.employee_id])[0]
        except ValueError as error:
            raise SessionUnavailableError(str(error)) from error
        mode = data.context_mode or team["context_mode"]
        cwd = data.working_dir or parent["cwd"]
        child = None
        if mode == "continue":
            child = await self.storage._fetch_one(
                "SELECT s.* FROM sessions s JOIN team_sessions ts ON ts.session_id=s.id "
                "WHERE ts.leader_session_id=? AND ts.employee_id=? AND s.cwd=? COLLATE NOCASE ORDER BY s.created_at DESC,s.rowid DESC LIMIT 1",
                (parent_id, data.employee_id, cwd))
        reuse = child is not None
        if reuse:
            runtime = self.sessions.get(child["id"])
            connecting = (runtime and runtime.active and runtime.active.connection
                          and not runtime.active.connection.done() and child["native_id"] is None)
            if (runtime and (runtime.unavailable or (runtime.client and not runtime.client.reusable and not connecting))) or (not runtime and child["native_id"]):
                raise SessionUnavailableError("员工历史会话不可继续，请指定 context_mode=new 单开")
        child_id = child["id"] if reuse else str(uuid4())
        resolved = data.model_copy(update={"harness": child["harness"] if reuse else profile["harness"]})
        try:
            if not reuse:
                settings = await self.storage.get_harness_settings(profile["harness"])
                await self.employees.prepare_async(data.employee_id, child_id, settings, Path(cwd), await self.teams.context(team, data.employee_id))
            task, child, run, created = await self.storage.create_task_with_run(parent_id, resolved, child_id, reuse)
        except BaseException:
            if not reuse and await self.storage.get_session(child_id) is None:
                self.employees.discard(child_id)
            raise
        if created:
            runtime = self.sessions.setdefault(child_id, SessionRuntime(child))
            self._enqueue_run(runtime, run)
        return {"task_id": task["id"], "child_session_id": child_id, "run_id": run["id"], "status": task["status"]}

    async def _dispatch(self, runtime: SessionRuntime) -> None:
        while True:
            run = await runtime.queue.get()
            try:
                if run is None:
                    return
                if runtime.session["parent_id"] is None:
                    await self.execute_run(run["id"])
                else:
                    waiter = asyncio.get_running_loop().create_future()
                    self.worker_waiters[run["id"]] = waiter
                    self.worker_queue.put_nowait(run["id"])
                    await asyncio.shield(waiter)
            finally:
                runtime.queue.task_done()

    async def execute_run(self, run_id: str) -> None:
        run = await self.storage.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        runtime = await self._runtime(run["session_id"])
        async with runtime.prompt_lock:
            async with runtime.gate:
                if self.closing or runtime.unavailable:
                    await self._cancel_queued(run_id, "服务关闭或会话连接不可复用")
                    return
                claimed = await self.storage.transition_run(run_id, {RunStatus.QUEUED}, RunStatus.RUNNING, {})
                if not claimed:
                    return
                execution = Execution(run, RunJournal(self.storage, run_id, self.journal_budget))
                runtime.active = execution
                execution.task = asyncio.create_task(self._execute(runtime, execution), name=f"run:{run_id}")
            try:
                await execution.task
            finally:
                try:
                    if not execution.permissions_closed:
                        await self._expire_permissions(execution, "run_ended")
                finally:
                    try:
                        if not execution.journal.task.done():
                            await execution.journal.close()
                    finally:
                        async with runtime.gate:
                            runtime.active = None

    def _new_client(self, runtime: SessionRuntime) -> AcpClient:
        runtime.diagnostics = self.diagnostics
        return self.client_factory(
            runtime.session["harness"], SessionHost(runtime, self),
            lambda kind, data: logger.debug("ACP %s %s", kind, runtime.redact(data)),
            runtime.observe, lambda reason: self._expire_session_permissions(runtime, reason),
        )

    def _revoke_token(self, runtime: SessionRuntime) -> None:
        if runtime.mcp_token is not None:
            self.tokens.pop(runtime.mcp_token, None)

    async def _prepare_directory(self, runtime: SessionRuntime) -> None:
        if runtime.session["native_id"] is not None and not (self.teams and await self.teams.association(runtime.session["id"])):
            return
        if runtime.active is None:
            task = await self.storage.get_session_task(runtime.session["id"])
        else:
            task = await self.storage.get_task(runtime.active.run["task_id"]) if runtime.active.run["task_id"] else None
        if task is None:
            return
        root = Path(runtime.session["cwd"])
        if not root.is_absolute() or not root.is_dir():
            raise WorkingDirectoryError("Worker 执行目录不存在或不是绝对目录")
        project = await self.storage.get_project(task["project_id"])
        repo = Path(project["path"])
        if task["worktree"]:
            recorded = await validate_worktree(task, repo)
            if root != recorded:
                raise WorkingDirectoryError("Worker 执行目录与已绑定 worktree 不一致")
        elif await self.storage.get_task_working_dir(task["id"]) is not None:
            baseline = await selected_worktree(root, repo)
            if baseline is not None:
                sha, branch = baseline
                await self.storage.attach_worktree(task["id"], runtime.active.run["id"], str(root), sha, branch)

    async def _connect(self, runtime: SessionRuntime) -> str:
        if runtime.session["parent_id"] is not None:
            await self._prepare_directory(runtime)
        if runtime.active.cancel_requested or self.closing:
            raise asyncio.CancelledError
        role = "coordinator" if runtime.session["parent_id"] is None else "worker"
        runtime.mcp_token = secrets.token_urlsafe(32)
        self.tokens[runtime.mcp_token] = CallerIdentity(runtime.session["id"], role)
        try:
            runtime.client = self._new_client(runtime)
            if execution_values := runtime.active.settings.get("config_values"):
                launch_settings(runtime.client, execution_values)
            elif isinstance(runtime.client, AcpClient):
                launch_settings(runtime.client, {})
            binding = self.employees.binding(runtime.session["id"]) if self.employees else None
            if binding:
                from app.agents.employee_native import configure_employee
                if isinstance(runtime.client, AcpClient):
                    await configure_employee(runtime.client, binding, Path(runtime.session["cwd"]))
                else:
                    await runtime.client.configure_employee(binding)
            bridge = build_mcp_bridge(self.internal_base_url, runtime.mcp_token, role)
            if self.teams and await self.teams.association(runtime.session["id"]):
                bridge.env.append(EnvVariable(name="HARNESS_LAB_TEAM", value="1"))
            return await runtime.client.connect(Path(runtime.session["cwd"]), [bridge])
        except BaseException:
            self._revoke_token(runtime)
            raise

    async def _perform(self, runtime: SessionRuntime, execution: Execution) -> tuple[RunStatus, dict[str, Any]]:
        if execution.cancel_requested:
            return RunStatus.CANCELED, {"reason": "执行启动前取消"}
        binding = self.employees.binding(runtime.session["id"]) if self.employees else None
        execution.settings = binding["settings"] if binding else await self.storage.get_harness_settings(runtime.session["harness"])
        mode = await self.storage.run_collaboration_mode(execution.run["id"])
        if mode is not None:
            execution.settings = {**execution.settings, "config_values": {
                **execution.settings["config_values"], "collaboration_mode": mode}}
        if binding:
            self.employees.record_run(binding, execution.run["id"])
        fresh = runtime.client is None
        if runtime.client is None:
            execution.connection = asyncio.create_task(
                self._connect(runtime), name=f"connect:{execution.run['id']}",
            )
            native_id = await execution.connection
            await self.storage.set_session_connection(runtime.session["id"], native_id, runtime.client.proc.pid)
        elif execution.run["task_id"]:
            execution.connection = asyncio.create_task(self._prepare_directory(runtime), name=f"directory:{execution.run['id']}")
            await execution.connection
        await self._apply_settings(runtime, execution, fresh)
        if mode is not None and actual_values(runtime.client).get("collaboration_mode") != mode:
            raise ValueError("Codex 未确认所选工作模式，消息未发送")
        if execution.cancel_requested:
            return RunStatus.CANCELED, {"reason": "prompt 启动前取消"}
        prompt = await self.memory.augment(execution.run) if self.memory else execution.run["prompt"]
        if execution.cancel_requested:
            return RunStatus.CANCELED, {"reason": "记忆检索期间取消"}
        execution.prompt = asyncio.create_task(runtime.client.prompt(prompt), name="acp-prompt")
        await asyncio.sleep(0)
        execution.ready.set()
        failed = asyncio.create_task(execution.journal.failed.wait(), name="event-write-failure")
        try:
            await asyncio.wait({execution.prompt, failed}, return_when=asyncio.FIRST_COMPLETED)
            if execution.journal.error is not None:
                runtime.unavailable = True
                await runtime.client.close()
                await asyncio.gather(execution.prompt, return_exceptions=True)
                raise execution.journal.error
            response = await execution.prompt
            status = RunStatus.CANCELED if response.stop_reason == "cancelled" else RunStatus.COMPLETED
            return status, {"stop_reason": response.stop_reason}
        finally:
            failed.cancel()
            await asyncio.gather(failed, return_exceptions=True)

    async def _apply_settings(self, runtime: SessionRuntime, execution: Execution, fresh: bool) -> None:
        client = runtime.client
        values = execution.settings["config_values"]
        if not values and not runtime.config_overrides and not isinstance(client, AcpClient):
            return
        desired = dict(values)
        missing_defaults = []
        if "model" in runtime.config_overrides and "model" not in values:
            desired["model"] = client.config_defaults["model"]
        warnings = await apply_values(client, {key: value for key, value in desired.items() if key == "model"}, fresh)
        for key in runtime.config_overrides.keys() - values.keys():
            if key == "model":
                continue
            if key in client.config_defaults:
                desired[key] = client.config_defaults[key]
            else:
                missing_defaults.append({"id": key, "requested": None, "reason": "无法恢复原生默认值，需要新建会话"})
        warnings += await apply_values(client, {key: value for key, value in desired.items() if key != "model"}, fresh) + missing_defaults
        binding = self.employees.binding(runtime.session["id"]) if self.employees else None
        if binding:
            warnings += [{"id": f"skill:{item['name']}", "requested": None, "reason": item["error"]}
                         for item in binding["warnings"]]
        permission_changed = bool(PERMISSION_KEYS[client.harness] & (desired.keys() | runtime.config_overrides.keys()))
        failed = {item["id"] for item in warnings}
        runtime.config_overrides = {**{key: value for key, value in runtime.config_overrides.items() if key in failed},
                                    **{key: value for key, value in values.items() if key not in failed}}
        if permission_changed and (PERMISSION_KEYS[client.harness] & desired.keys()) - failed:
            runtime.automatic_approval = automatic_permission(client, not bool(PERMISSION_KEYS[client.harness] & runtime.config_overrides.keys()))
        payload = {"revision": execution.settings["revision"], "actual": actual_values(client),
                   "warnings": warnings, "automatic_approval": runtime.automatic_approval}
        execution.journal.receive({"kind": "session.config", "payload": payload})
        await execution.journal.flush()

    async def _execute(self, runtime: SessionRuntime, execution: Execution) -> None:
        execution.started.set()
        try:
            status, payload = await self._perform(runtime, execution)
        except asyncio.CancelledError:
            status, payload = RunStatus.INTERRUPTED, {"error": "执行中断或服务关闭"}
            try:
                if runtime.client is not None:
                    await runtime.client.close()
                if execution.cancel_requested and not execution.shutdown:
                    status, payload = RunStatus.CANCELED, {"reason": "取消后资源已清理"}
            except Exception as error:
                payload["error"] += f"；关闭失败：{error!r}"
            if execution.prompt is not None:
                await asyncio.gather(execution.prompt, return_exceptions=True)
        except Exception as error:
            status = RunStatus.INTERRUPTED if isinstance(error, ConnectionError) else RunStatus.FAILED
            payload = {"error": repr(error)}
        finally:
            execution.ready.set()
        async with runtime.gate:
            await self._settle(runtime, execution, status, payload)

    async def _settle(self, runtime: SessionRuntime, execution: Execution,
                      status: RunStatus, payload: dict[str, Any]) -> None:
        if execution.cancel_task is not None:
            await execution.cancel_task
        execution.journal.accepting = False
        await self._expire_permissions(execution, "run_ended")
        if execution.cancel_error is not None and status != RunStatus.COMPLETED:
            status, payload = self._cancel_failure(runtime, execution)
        payload = runtime.redact(payload)
        write_error = None
        try:
            await execution.journal.flush()
            if execution.journal.error is not None:
                raise execution.journal.error
            await execution.journal.call(lambda: self.storage.transition_run(
                execution.run["id"], ACTIVE_STATUSES, status, payload))
        except Exception as error:
            write_error = error
        finally:
            try:
                await execution.journal.close()
            except Exception as error:
                write_error = write_error or error
        if write_error is not None:
            runtime.unavailable = True
            status, payload = RunStatus.FAILED, {"error": f"事件写入失败：{write_error!r}"}
            try:
                if runtime.client is not None:
                    await runtime.client.close()
            except Exception as cleanup:
                payload["error"] += f"；关闭失败：{cleanup!r}"
            await self.storage.transition_run(execution.run["id"], ACTIVE_STATUSES, status, runtime.redact(payload))
        if runtime.client is None or not runtime.client.reusable:
            self._revoke_token(runtime)
            runtime.unavailable = True

    def _cancel_failure(self, runtime: SessionRuntime, execution: Execution) -> tuple[RunStatus, dict[str, Any]]:
        client = runtime.client
        cleaned = client is not None and client.forced and client.proc is not None and client.proc.returncode is not None
        status = RunStatus.CANCELED if cleaned else RunStatus.INTERRUPTED
        return status, {"error": repr(execution.cancel_error), "reason": "取消清理完成" if cleaned else "取消未确认"}

    async def _cancel_queued(self, run_id: str, reason: str) -> None:
        await self.storage.transition_run(run_id, {RunStatus.QUEUED}, RunStatus.CANCELED, {"reason": reason})

    async def _cancel_active(self, runtime: SessionRuntime, execution: Execution) -> None:
        if execution.connection is not None and not execution.connection.done():
            execution.connection.cancel()
        await execution.ready.wait()
        try:
            await self._expire_permissions(execution, "shutdown" if execution.shutdown else "cancel")
            if execution.prompt is not None and not execution.prompt.done():
                await runtime.client.cancel()
        except Exception as error:
            execution.cancel_error = error

    async def cancel(self, run_id: str) -> dict[str, Any]:
        run = await self.storage.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        runtime = await self._runtime(run["session_id"])
        async with runtime.gate:
            run = await self.storage.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            if run["status"] == RunStatus.QUEUED:
                await self._cancel_queued(run_id, "用户取消排队输入")
            elif run["status"] not in RUN_TERMINALS:
                execution = runtime.active
                if execution is None or execution.run["id"] != run_id:
                    raise SessionUnavailableError("Run 不属于本进程的活动连接；不能恢复旧执行")
                await execution.journal.call(lambda: self.storage.transition_run(
                    run_id, {RunStatus.RUNNING, RunStatus.WAITING_PERMISSION}, RunStatus.CANCEL_REQUESTED, {}))
                execution.cancel_requested = True
                if execution.cancel_task is None:
                    execution.cancel_task = asyncio.create_task(self._cancel_active(runtime, execution), name=f"cancel:{run_id}")
            else:
                return run
        if run["status"] != RunStatus.QUEUED:
            await asyncio.shield(execution.task)
        result = await self.storage.get_run(run_id)
        if result is None:
            raise KeyError(run_id)
        return result

    async def _stop_session(self, runtime: SessionRuntime) -> None:
        self._revoke_token(runtime)
        async with runtime.admission:
            async with runtime.gate:
                execution = runtime.active
                if execution is not None and not execution.task.done():
                    execution.shutdown = True
                    execution.cancel_requested = True
                    if not execution.ready.is_set():
                        await execution.started.wait()
                        execution.task.cancel()
                    elif execution.cancel_task is None:
                        execution.cancel_task = asyncio.create_task(self._cancel_active(runtime, execution))
                runtime.queue.put_nowait(None)
        try:
            if runtime.dispatcher is not None:
                await runtime.dispatcher
        finally:
            if runtime.client is not None:
                await runtime.client.close()

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        if self.closing:
            raise SessionUnavailableError("服务正在关闭，不能删除会话")
        task = asyncio.create_task(self._delete_session(session_id), name=f"delete:{session_id}")
        self.deletions.add(task)
        task.add_done_callback(self.deletions.discard)
        return await asyncio.shield(task)

    async def _delete_session(self, session_id: str) -> dict[str, Any]:
        async with self.deletion_lock:
            async with self.delegation_admission:
                records = await self.storage.session_tree(session_id)
                if not records:
                    raise KeyError(session_id)
                session_ids = [record["id"] for record in records]
                self.deleting_sessions.update(session_ids)
                runtimes = []
                for record in records:
                    runtime = self.sessions.setdefault(record["id"], SessionRuntime(record))
                    async with runtime.admission:
                        async with runtime.gate:
                            runtime.unavailable = True
                            self._revoke_token(runtime)
                    runtimes.append(runtime)
            try:
                results = await asyncio.gather(*(self._stop_session(runtime) for runtime in runtimes),
                                               return_exceptions=True)
                errors = [result for result in results if isinstance(result, BaseException)]
                if errors:
                    raise SessionUnavailableError(f"会话未删除：停止执行或清理连接失败：{errors[0]}")
                async with self.delegation_admission:
                    await self.storage.delete_sessions(session_ids)
                    for session_id in session_ids:
                        self.sessions.pop(session_id, None)
                return {"deleted_session_ids": session_ids}
            finally:
                self.deleting_sessions.difference_update(session_ids)

    async def close(self) -> None:
        async with self.delegation_admission:
            self.closing = True
            self.tokens.clear()
        await asyncio.gather(*self.deletions, return_exceptions=True)
        results = await asyncio.gather(*(self._stop_session(runtime) for runtime in self.sessions.values()),
                                       return_exceptions=True)
        if self.worker_dispatcher is not None:
            self.worker_dispatcher.cancel()
            await asyncio.gather(self.worker_dispatcher, return_exceptions=True)
        await asyncio.gather(*self.worker_tasks.values(), return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("编排器关闭失败", errors)
