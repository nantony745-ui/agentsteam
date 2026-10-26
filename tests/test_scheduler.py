"""3.1：无 Worker 并发上限；Event 门闩控制运行区间，不调用真实模型。"""

import asyncio
import json
from collections import defaultdict
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.domain.models import SessionCreate
from app.main import create_app
from test_http_runs import ControlledFactory, new_session, submit, terminal
from test_permissions import pause_auto_answers, PermissionClient, interactions, options
from test_storage import repository


class SchedulerClient(PermissionClient):
    async def prompt(self, text):
        self.owner.trace.append(("started", text))
        self.owner.started_events[text].set()
        release = asyncio.create_task(self._release(text), name=f"test-release:{text}")
        try:
            return await super().prompt(text)
        finally:
            release.cancel()
            await asyncio.gather(release, return_exceptions=True)
            self.owner.trace.append(("finished", text))
            self.owner.finished_events[text].set()

    async def _release(self, text):
        await self.owner.releases[text].wait()
        if text in self.owner.failures:
            self.pending.set_exception(self.owner.failures[text])
        else:
            self.finish(f"{text}完成")


class SchedulerFactory(ControlledFactory):
    def __init__(self):
        super().__init__()
        self.releases = defaultdict(asyncio.Event)
        self.started_events = defaultdict(asyncio.Event)
        self.finished_events = defaultdict(asyncio.Event)
        self.failures = {}
        self.trace = []

    def __call__(self, *args):
        agent = SchedulerClient(self, *args)
        self.clients.append(agent)
        return agent


@pytest_asyncio.fixture
async def scheduler(tmp_path, repository):
    factory = SchedulerFactory()
    app = create_app(tmp_path / "scheduler.db", factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            project = (await client.post("/api/projects", json={"path": str(repository), "name": "调度测试"})).json()
            parent = await new_session(client, project)
            yield client, app, factory, parent


async def child_session(app, parent):
    storage = app.state.storage
    child = await storage.create_session(SessionCreate(project_id=parent["project_id"], harness="codex"))
    async with storage.transaction() as db:
        await db.execute("UPDATE sessions SET parent_id=? WHERE id=?", (parent["id"], child["id"]))
    return await storage.get_session(child["id"])


async def drain(app, sessions):
    async with asyncio.timeout(3):
        await asyncio.gather(*(app.state.service.sessions[s["id"]].queue.join() for s in sessions))
        await app.state.service.worker_queue.join()


@pytest.mark.asyncio
async def test_three_workers_start_before_any_finishes_while_parent_waits(scheduler):
    client, app, factory, parent = scheduler
    service = app.state.service
    dispatcher = service.worker_dispatcher
    await service.start()
    assert service.worker_dispatcher is dispatcher
    main = await submit(client, parent, "主会话等待")
    await factory.started()
    children = [await child_session(app, parent) for _ in range(3)]
    runs = [await submit(client, child, f"Worker {i}") for i, child in enumerate(children)]
    for _ in children:
        await factory.started()
    assert len(service.worker_tasks) == 3
    assert main["run_id"] not in service.worker_tasks
    assert len(factory.trace) == 4
    assert set(factory.trace) == {("started", "主会话等待"), *(("started", f"Worker {i}") for i in range(3))}
    for run in [main, *runs]:
        assert (await client.get(f"/api/runs/{run['run_id']}")).json()["status"] == "running"

    async def collect():
        await asyncio.gather(*(factory.finished_events[f"Worker {i}"].wait() for i in range(3)))
        factory.releases["主会话等待"].set()

    collecting = asyncio.create_task(collect())
    for i in range(3):
        factory.releases[f"Worker {i}"].set()
    await asyncio.wait_for(collecting, 3)
    await drain(app, [parent, *children])
    assert service.worker_tasks == service.worker_waiters == {}
    for run in [main, *runs]:
        assert (await terminal(client, run["run_id"]))["status"] == "completed"
    print("并发门闩记录：" + json.dumps(factory.trace, ensure_ascii=False))


@pytest.mark.asyncio
async def test_worker_fifo_and_other_session_progress(scheduler):
    client, app, factory, parent = scheduler
    first_child, other_child = [await child_session(app, parent) for _ in range(2)]
    first = await submit(client, first_child, "同会话第一轮")
    agent, _ = await factory.started()
    second = await submit(client, first_child, "同会话第二轮")
    other = await submit(client, other_child, "其他 Worker")
    await factory.started()
    assert (await client.get(f"/api/runs/{second['run_id']}")).json()["status"] == "queued"
    assert not factory.started_events["同会话第二轮"].is_set()
    factory.releases["同会话第一轮"].set()
    reused, text = await factory.started()
    assert reused is agent and text == "同会话第二轮"
    assert factory.trace.index(("finished", "同会话第一轮")) < factory.trace.index(("started", "同会话第二轮"))
    factory.releases["同会话第二轮"].set()
    factory.releases["其他 Worker"].set()
    await drain(app, [first_child, other_child])
    for run in [first, second, other]:
        assert (await terminal(client, run["run_id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_duplicate_submission_queue_entries_and_cas_do_not_repeat_prompt(scheduler):
    client, app, factory, parent = scheduler
    child = await child_session(app, parent)
    request_id = str(uuid4())
    first = await submit(client, child, "只执行一次", request_id)
    await factory.started()
    repeated = await submit(client, child, "只执行一次", request_id)
    assert repeated["run_id"] == first["run_id"]
    app.state.service.worker_queue.put_nowait(first["run_id"])
    duplicate = asyncio.create_task(app.state.service.execute_run(first["run_id"]))
    factory.releases["只执行一次"].set()
    await asyncio.wait_for(duplicate, 3)
    await drain(app, [child])
    app.state.service.worker_queue.put_nowait(first["run_id"])
    await asyncio.wait_for(app.state.service.worker_queue.join(), 3)
    assert factory.trace == [("started", "只执行一次"), ("finished", "只执行一次")]
    assert len(factory.clients) == 1


@pytest.mark.asyncio
async def test_cancel_before_worker_claim_never_starts_agent(scheduler, monkeypatch):
    client, app, factory, parent = scheduler
    child = await child_session(app, parent)
    entered, release = asyncio.Event(), asyncio.Event()
    execute = app.state.service.execute_run

    async def before_claim(run_id):
        entered.set()
        await release.wait()
        await execute(run_id)

    monkeypatch.setattr(app.state.service, "execute_run", before_claim)
    run = await submit(client, child, "认领前取消")
    try:
        await asyncio.wait_for(entered.wait(), 3)
        canceled = (await client.post(f"/api/runs/{run['run_id']}/cancel")).json()
        assert canceled["status"] == "canceled" and canceled["started_at"] is None
    finally:
        release.set()
    await drain(app, [child])
    assert factory.clients == [] and factory.trace == []
    later = await submit(client, child, "取消后正常执行")
    await factory.started()
    factory.releases["取消后正常执行"].set()
    await drain(app, [child])
    assert (await terminal(client, later["run_id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_failed_worker_records_error_and_dispatches_next_run(scheduler):
    client, app, factory, parent = scheduler
    child = await child_session(app, parent)
    factory.failures["失败一轮"] = RuntimeError("受控业务异常")
    failed = await submit(client, child, "失败一轮")
    await factory.started()
    later = await submit(client, child, "失败后继续")
    factory.releases["失败一轮"].set()
    await factory.started()
    record = await terminal(client, failed["run_id"])
    assert record["status"] == "failed" and "受控业务异常" in record["error"]
    assert not app.state.service.worker_dispatcher.done()
    factory.releases["失败后继续"].set()
    await drain(app, [child])
    assert (await terminal(client, later["run_id"]))["status"] == "completed"
    runtime = app.state.service.sessions[child["id"]]
    assert runtime.active is None and not runtime.prompt_lock.locked()
    assert app.state.service.worker_tasks == app.state.service.worker_waiters == {}


@pytest.mark.asyncio
async def test_permission_wait_does_not_block_workers_and_cancel_clears_callbacks(scheduler, monkeypatch):
    client, app, factory, parent = scheduler
    pause_auto_answers(app.state.service, monkeypatch)
    child = await child_session(app, parent)
    waiting = await submit(client, child, "等待权限")
    agent, _ = await factory.started()
    callback, _ = agent.permission(options())
    permission, = await interactions(client, child, 1)
    queued = await submit(client, child, "取消的同会话排队项")
    assert (await client.post(f"/api/runs/{queued['run_id']}/cancel")).json()["status"] == "canceled"
    others = [await child_session(app, parent) for _ in range(3)]
    other_runs = []
    for i, other in enumerate(others):
        other_runs.append(await submit(client, other, f"权限期间 Worker {i}"))
        await factory.started()
    assert len(app.state.service.worker_tasks) == 4
    assert (await client.get(f"/api/runs/{waiting['run_id']}")).json()["status"] == "waiting_permission"
    assert not callback.done()
    assert (await client.post(f"/api/runs/{waiting['run_id']}/cancel")).json()["status"] == "canceled"
    assert (await callback).outcome.outcome == "cancelled"
    assert (await app.state.storage.get_interaction(permission["id"]))["status"] == "expired"
    for i in range(3):
        factory.releases[f"权限期间 Worker {i}"].set()
    await drain(app, [child, *others])
    for run in other_runs:
        assert (await terminal(client, run["run_id"]))["status"] == "completed"
    assert not factory.started_events["取消的同会话排队项"].is_set()
    assert app.state.service.permissions == {}


@pytest.mark.asyncio
async def test_shutdown_reaps_worker_execution_permissions_and_queued_runs(scheduler, monkeypatch):
    client, app, factory, parent = scheduler
    pause_auto_answers(app.state.service, monkeypatch)
    child = await child_session(app, parent)
    active = await submit(client, child, "关闭时活动项")
    agent, _ = await factory.started()
    callback, _ = agent.permission(options())
    await interactions(client, child, 1)
    queued = await submit(client, child, "关闭时排队项")
    service = app.state.service
    execution = service.sessions[child["id"]].active
    await asyncio.wait_for(service.close(), 3)
    assert service.worker_dispatcher.cancelled()
    assert service.worker_tasks == service.worker_waiters == service.permissions == {}
    assert execution.task.done() and execution.journal.task.done() and execution.prompt.done()
    assert (await callback).outcome.outcome == "cancelled"
    assert agent.closed
    assert not service.sessions[child["id"]].prompt_lock.locked()
    assert (await app.state.storage.get_run(active["run_id"]))["status"] == "canceled"
    assert (await app.state.storage.get_run(queued["run_id"]))["status"] == "canceled"
    assert not factory.started_events["关闭时排队项"].is_set()
    await asyncio.wait_for(service.worker_queue.join(), 3)


@pytest.mark.asyncio
async def test_shutdown_during_worker_connect_closes_journal_and_client(scheduler):
    client, app, factory, parent = scheduler
    child = await child_session(app, parent)
    factory.connect_release.clear()
    run = await submit(client, child, "关闭时连接中")
    await asyncio.wait_for(factory.connect_started.wait(), 3)
    service = app.state.service
    execution = service.sessions[child["id"]].active
    await asyncio.wait_for(service.close(), 3)
    assert execution.connection.done() and execution.task.done() and execution.journal.task.done()
    assert factory.clients[0].closed and factory.trace == []
    assert (await app.state.storage.get_run(run["run_id"]))["status"] == "interrupted"
    assert service.worker_tasks == service.worker_waiters == {}


@pytest.mark.asyncio
async def test_worker_dispatcher_propagates_cancellation(scheduler):
    _, app, _, _ = scheduler
    dispatcher = app.state.service.worker_dispatcher
    dispatcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatcher
