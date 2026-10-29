"""HTTP 路由与数据库事件流。"""

import asyncio
import base64
import binascii
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.agents.acp_client import load_argv
from app.domain.models import FeedbackCreate, MessageCreate, PermissionAnswer, QuestionAnswer, ProjectCreate, SessionCreate, UuidString
from app.db.storage import PermissionConflictError, RequestConflictError
from app.delivery.git_delivery import diff_task, read_preview, validate_worktree
from app.services.project_directories import list_project_directories
from app.services.orchestrator import SessionUnavailableError


@dataclass(frozen=True)
class StreamSettings:
    poll_seconds: float = 0.25
    heartbeat_seconds: float = 15


def event_data(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "payload": json.loads(row["payload"])}


async def require_record(record: Any) -> Any:
    if record is None:
        raise HTTPException(404, "记录不存在")
    return record


def create_router(stream_settings: StreamSettings) -> APIRouter:
    router = APIRouter()
    sidebar_lock = asyncio.Lock()
    sidebar_snapshot = None

    @router.post("/api/projects", status_code=201)
    async def create_project(body: ProjectCreate, request: Request):
        async with request.app.state.employees.admission:
            try:
                snapshot = await request.app.state.teams.snapshot(body.team_id) if body.team_id else None
                return await request.app.state.storage.create_project(body, snapshot)
            except KeyError as error:
                raise HTTPException(404, "团队或员工不存在，请刷新") from error
            except RequestConflictError:
                raise
            except sqlite3.IntegrityError as error:
                raise HTTPException(409, "项目已存在") from error
            except (ValueError, OSError) as error:
                raise HTTPException(422, str(error)) from error

    @router.get("/api/projects")
    async def projects(request: Request):
        return await request.app.state.storage.list_projects()

    @router.get("/api/project-directories")
    async def project_directories(path: str | None = None):
        try:
            return await asyncio.to_thread(list_project_directories, path)
        except FileNotFoundError as error:
            raise HTTPException(404, "目录不存在，请检查路径") from error
        except PermissionError as error:
            raise HTTPException(403, "没有权限读取该目录") from error
        except (ValueError, OSError) as error:
            raise HTTPException(422, f"无法读取目录：{error}") from error

    @router.post("/api/sessions", status_code=201)
    async def create_session(body: SessionCreate, request: Request):
        async with request.app.state.employees.admission:
            storage = request.app.state.storage
            employees = request.app.state.employees
            project = await require_record(await storage.get_project(body.project_id))
            if project.get("team") and not body.employee_id:
                raise HTTPException(422, "团队项目请选择团队员工")
            if project.get("team") and body.employee_id:
                try:
                    return await request.app.state.teams.create_session(body)
                except KeyError as error:
                    raise HTTPException(404, "员工或队长会话不存在") from error
                except (ValueError, OSError) as error:
                    raise HTTPException(409, str(error)) from error
            if body.leader_session_id:
                raise HTTPException(422, "仅团队成员会话可指定队长")
            session_id = str(uuid4())
            try:
                if body.employee_id:
                    profile = employees.get(body.employee_id)
                    if not profile["harness"]:
                        raise ValueError("请先配置队员的 Harness")
                    load_argv(profile["harness"])
                    settings = await storage.get_harness_settings(profile["harness"])
                    await employees.prepare_async(body.employee_id, session_id, settings, Path(project["path"]))
                    body = SessionCreate(project_id=body.project_id, harness=profile["harness"])
                load_argv(body.harness)
                result = await storage.create_session(body, session_id)
            except KeyError as error:
                raise HTTPException(404, "队员不存在") from error
            except (ValueError, OSError) as error:
                employees.discard(session_id)
                raise HTTPException(409, str(error)) from error
            except BaseException:
                if await storage.get_session(session_id) is None:
                    employees.discard(session_id)
                raise
            return employees.decorate(result)

    @router.get("/api/sessions")
    async def all_sessions(request: Request):
        nonlocal sidebar_snapshot
        storage = request.app.state.storage
        async with sidebar_lock:
            changed = storage.changed
            if sidebar_snapshot is None or sidebar_snapshot[0] is not changed:
                rows = await storage.list_sessions(None)
                body = await asyncio.to_thread(lambda: JSONResponse([
                    request.app.state.employees.decorate(row) for row in rows
                ]).body)
                etag = '"' + hashlib.sha256(body).hexdigest() + '"'
                sidebar_snapshot = (changed, body, etag)
            _, body, etag = sidebar_snapshot
        headers = {"ETag": "W/" + etag, "Cache-Control": "private, no-cache"}
        matches = {tag.strip().removeprefix("W/")
                   for tag in request.headers.get("if-none-match", "").split(",")}
        if etag in matches or "*" in matches:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)

    @router.get("/api/projects/{project_id}/sessions")
    async def sessions(project_id: UuidString, request: Request):
        storage = request.app.state.storage
        await require_record(await storage.get_project(project_id))
        return [request.app.state.employees.decorate(row) for row in await storage.list_sessions(project_id)]

    @router.get("/api/sessions/{session_id}")
    async def session(session_id: UuidString, request: Request, summary: bool = False):
        storage = request.app.state.storage
        record = await require_record(await storage.get_session_summary(session_id))
        record = request.app.state.employees.decorate(record)
        task = await storage.get_session_task(session_id)
        fields = ("id", "status", "base_sha", "base_branch", "branch", "worktree")
        task_detail = {key: task[key] for key in fields} if task is not None else None
        if task_detail is not None:
            task_detail["working_dir"] = await storage.get_task_working_dir(task["id"])
        return {"session": record, "runs": [] if summary else await storage.list_runs(session_id),
                "interactions": await storage.list_interactions(session_id),
                "task": task_detail}

    @router.get("/api/tasks/{task_id}/diff")
    async def task_diff(task_id: UuidString, request: Request):
        storage = request.app.state.storage
        task = await require_record(await storage.get_task(task_id))
        project = await storage.get_project(task["project_id"])
        return await diff_task(task, Path(project["path"]))

    @router.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: UuidString, request: Request):
        try:
            return await request.app.state.service.delete_session(session_id)
        except KeyError as error:
            raise HTTPException(404, "会话不存在或已删除") from error

    @router.get("/api/tasks/{task_id}/preview")
    async def task_preview(task_id: UuidString, request: Request, path: str):
        storage = request.app.state.storage
        task = await require_record(await storage.get_task(task_id))
        project = await storage.get_project(task["project_id"])
        root = await validate_worktree(task, Path(project["path"]))
        try:
            return await asyncio.to_thread(read_preview, root, path)
        except FileNotFoundError as error:
            raise HTTPException(404, "文件不存在") from error
        except (ValueError, OSError) as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/api/sessions/{session_id}/messages", status_code=202)
    async def submit(session_id: UuidString, body: MessageCreate, request: Request):
        await require_record(await request.app.state.storage.get_session(session_id))
        try:
            run = await request.app.state.service.submit(session_id, body)
        except KeyError as error:
            raise HTTPException(404, "会话不存在或已删除") from error
        except (RequestConflictError, SessionUnavailableError):
            raise
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"run_id": run["id"], "status": run["status"]}

    @router.get("/api/runs/{run_id}")
    async def run(run_id: UuidString, request: Request, summary: bool = False):
        storage = request.app.state.storage
        return await require_record(await (storage.get_run_summary(run_id) if summary else storage.get_run(run_id)))

    @router.get("/api/sessions/{session_id}/history")
    async def history(session_id: UuidString, request: Request,
                      before: Annotated[int | None, Query(ge=1)] = None):
        storage = request.app.state.storage
        await require_record(await storage.get_session(session_id))
        page = await storage.history(session_id, before)
        page["metadata"] = [event_data(row) for row in page["metadata"]]
        return page

    @router.get("/api/events/{event_id}/details")
    async def details(event_id: int, request: Request,
                      after_part: Annotated[int, Query(ge=-1)] = -1):
        return await require_record(await request.app.state.storage.detail_page(event_id, after_part))

    @router.get("/api/runs/{run_id}/tools/{tool_id}/details")
    async def tool_details(run_id: UuidString, tool_id: str, request: Request,
                           high_watermark: Annotated[int | None, Query(ge=0)] = None,
                           cursor: str | None = None):
        storage = request.app.state.storage
        await require_record(await storage.get_run_summary(run_id))
        after_event, after_part = 0, -1
        if cursor is not None:
            try:
                owner_run, owner_tool, bound_high, after_event, after_part = json.loads(base64.urlsafe_b64decode(cursor))
                if (owner_run != run_id or owner_tool != tool_id
                        or high_watermark is not None and bound_high != high_watermark
                        or not all(type(value) is int for value in (bound_high, after_event, after_part))
                        or not 0 <= after_event <= bound_high or after_part < -1):
                    raise ValueError("详情游标不匹配")
                high_watermark = bound_high
            except (ValueError, TypeError, binascii.Error) as error:
                raise HTTPException(422, "详情游标无效或不属于该工具和高水位") from error
        page = await storage.tool_detail_page(run_id, tool_id, high_watermark, after_event, after_part)
        if page["next"] is not None:
            values = [run_id, tool_id, page["high_watermark"], page["next"]["event"], page["next"]["part"]]
            page["next"] = base64.urlsafe_b64encode(json.dumps(values).encode()).decode()
        return page

    @router.post("/api/sessions/{session_id}/feedback", status_code=201)
    async def feedback(session_id: UuidString, body: FeedbackCreate, request: Request):
        storage = request.app.state.storage
        try:
            return event_data(await storage.create_feedback(session_id, body))
        except KeyError as error:
            raise HTTPException(404, "会话不存在或已删除") from error

    @router.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: UuidString, request: Request):
        try:
            return await request.app.state.service.cancel(run_id)
        except KeyError as error:
            raise HTTPException(404, "执行不存在或已删除") from error

    @router.post("/api/interactions/{question_id}/question-answer")
    async def answer_question(question_id: UuidString, body: QuestionAnswer, request: Request):
        try:
            return await request.app.state.service.answer_question(question_id, body.model_dump())
        except PermissionConflictError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/api/interactions/{permission_id}/answer")
    async def answer(permission_id: UuidString, body: PermissionAnswer, request: Request):
        try:
            return await request.app.state.service.answer_permission(permission_id, body.option_id)
        except KeyError as error:
            raise HTTPException(404, "权限请求不存在") from error
        except PermissionConflictError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    # 2.5 的历史与 SSE 路由复用同一条 Storage 查询。
    @router.get("/api/events")
    async def events(request: Request, project_id: UuidString, session_id: UuidString | None = None,
                     after_event_id: Annotated[int, Query(ge=0)] = 0,
                     limit: Annotated[int, Query(ge=1, le=100)] = 100,
                     run_id: Annotated[list[str] | None, Query()] = None,
                     through_event_id: Annotated[int | None, Query(ge=0)] = None,
                     include_context: bool = False):
        rows = await request.app.state.storage.list_events(
            project_id, session_id, after_event_id, limit, run_id, through_event_id, include_context)
        return [event_data(row) for row in rows]

    @router.get("/api/stream")
    async def stream(request: Request, project_id: UuidString, session_id: UuidString | None = None,
                     after_event_id: Annotated[int, Query(ge=0)] = 0,
                     limit: Annotated[int, Query(ge=1, le=100)] = 100,
                     last_event_id: Annotated[str | None, Header()] = None):
        cursor = after_event_id
        if last_event_id is not None and last_event_id.isascii() and last_event_id.isdecimal():
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError:
                cursor = after_event_id
        return StreamingResponse(
            stream_events(request, project_id, session_id, cursor, limit, stream_settings),
            media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


async def stream_events(request: Request, project_id: str, session_id: str | None,
                        cursor: int, limit: int, settings: StreamSettings):
    loop = asyncio.get_running_loop()
    heartbeat = loop.time() + settings.heartbeat_seconds
    storage = request.app.state.storage
    while not await request.is_disconnected():
        changed = storage.changed
        rows = await storage.list_events(project_id, session_id, cursor, limit)
        for row in rows:
            data = json.dumps(event_data(row), ensure_ascii=False, separators=(",", ":"))
            yield f"id: {row['id']}\nevent: lab\ndata: {data}\n\n"
            cursor = row["id"]
        if rows:
            heartbeat = loop.time() + settings.heartbeat_seconds
            continue
        if loop.time() >= heartbeat:
            yield ": ping\n\n"
            heartbeat = loop.time() + settings.heartbeat_seconds
        try:
            await asyncio.wait_for(changed.wait(), max(settings.poll_seconds, heartbeat - loop.time()))
        except TimeoutError:
            pass
