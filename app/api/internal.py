"""MCP 内部边界：连接凭据确定身份，委派提交后异步执行。"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.domain.models import EmptyInput, TaskCreate, TaskStatus, TaskStatusQuery, UuidString
from app.delivery.git_delivery import WorkingDirectoryError


TASK_SETTLED = frozenset({TaskStatus.REVIEW, TaskStatus.ACCEPTED, TaskStatus.FAILED, TaskStatus.CANCELED})


async def require_caller(request: Request, authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    scheme, _, token = (authorization or "").partition(" ")
    service = request.app.state.service
    identity = service.tokens.get(token) if scheme.lower() == "bearer" else None
    if service.closing or identity is None:
        raise HTTPException(401, "会话凭据无效或已失效", headers={"WWW-Authenticate": "Bearer"})
    session = await request.app.state.storage.get_session(identity.session_id)
    if session is None or service.tokens.get(token) != identity:
        raise HTTPException(401, "会话凭据无效或已失效", headers={"WWW-Authenticate": "Bearer"})
    role = "coordinator" if session["parent_id"] is None else "worker"
    if identity.role != role:
        raise HTTPException(403, "会话角色与连接身份不一致")
    return session


async def require_coordinator(session: Annotated[dict[str, Any], Depends(require_caller)]) -> dict[str, Any]:
    if session["parent_id"] is not None:
        raise HTTPException(403, "Worker 不能调用委派任务接口")
    return session


async def owned_tasks(request: Request, task_ids: list[str], parent_session_id: str) -> list[dict[str, Any]]:
    try:
        return await request.app.state.storage.get_task_snapshots(parent_session_id, task_ids)
    except KeyError as error:
        raise HTTPException(404, "任务不存在") from error


def task_status(task: dict[str, Any]) -> dict[str, Any]:
    return {key: task[key] for key in ("task_id", "child_session_id", "run_id", "status", "run_status")}


async def wait_for_tasks(
    request: Request, session: dict[str, Any], body: TaskStatusQuery, poll_seconds: float,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + body.wait_ms / 1000
    while True:
        current = await require_caller(request, request.headers.get("authorization"))
        await require_coordinator(current)
        tasks = await owned_tasks(request, body.task_ids, session["id"])
        remaining = deadline - loop.time()
        if (not tasks or remaining <= 0 or any(task["status"] in TASK_SETTLED for task in tasks)
                or await request.is_disconnected()):
            return {"tasks": [task_status(task) for task in tasks]}
        await asyncio.sleep(min(poll_seconds, remaining))


def create_internal_router(poll_seconds: float) -> APIRouter:
    router = APIRouter(prefix="/internal")
    caller = Annotated[dict[str, Any], Depends(require_caller)]
    coordinator = Annotated[dict[str, Any], Depends(require_coordinator)]

    @router.post("/tasks", status_code=202)
    async def delegate(body: TaskCreate, request: Request, session: coordinator):
        try:
            return await request.app.state.service.delegate_task(session["id"], body)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except KeyError as error:
            raise HTTPException(404, "会话不存在") from error
        except WorkingDirectoryError as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/tasks/status")
    async def status(body: TaskStatusQuery, request: Request, session: coordinator):
        return await wait_for_tasks(request, session, body, poll_seconds)

    @router.post("/tasks/{task_id}/result")
    async def result(task_id: UuidString, body: EmptyInput, request: Request, session: coordinator):
        return (await owned_tasks(request, [task_id], session["id"]))[0]

    @router.post("/tasks/{task_id}/cancel")
    async def cancel(task_id: UuidString, body: EmptyInput, request: Request, session: coordinator):
        task = (await owned_tasks(request, [task_id], session["id"]))[0]
        try:
            await request.app.state.service.cancel(task["run_id"])
        except KeyError as error:
            raise HTTPException(404, "执行不存在或已删除") from error
        return task_status((await owned_tasks(request, [task_id], session["id"]))[0])

    @router.post("/feedback/pull")
    async def feedback(body: EmptyInput, request: Request, session: caller):
        return {"messages": await request.app.state.storage.pull_feedback(session["id"])}

    @router.post("/team/members")
    async def members(body: EmptyInput, request: Request, session: caller):
        try:
            roster = await request.app.state.teams.roster(session["id"])
            for member in roster["members"]:
                member["profile"].pop("harness")
            return roster
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    return router
