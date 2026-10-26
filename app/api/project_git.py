"""项目地址旁的 Git 状态及显式初始化。"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.api.routes import require_record
from app.db.storage import validate_project_directory
from app.domain.models import UuidString
from app.services.project_git import initialize_project_git, project_git_status


async def git_directory(request: Request, project_id: str, session_id: str | None) -> Path:
    storage = request.app.state.storage
    project = await require_record(await storage.get_project(project_id))
    path = project["path"]
    if session_id:
        session = await require_record(await storage.get_session(session_id))
        if session["project_id"] != project_id:
            raise HTTPException(404, "会话不属于当前项目")
        path = session["cwd"]
    return await asyncio.to_thread(validate_project_directory, Path(path))


def create_project_git_router() -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/git")

    async def execute(request: Request, project_id: str, session_id: str | None,
                      operation: Callable[[Path], Awaitable[dict]]) -> dict:
        try:
            return await operation(await git_directory(request, project_id, session_id))
        except FileNotFoundError as error:
            raise HTTPException(422, "目录不存在，或未找到 Git，请检查目录及 Git 安装") from error
        except (ValueError, OSError) as error:
            raise HTTPException(422, str(error)) from error

    @router.get("")
    async def status(project_id: UuidString, request: Request, session_id: UuidString | None = None):
        return await execute(request, project_id, session_id, project_git_status)

    @router.post("/init")
    async def initialize(project_id: UuidString, request: Request, session_id: UuidString | None = None):
        return await execute(request, project_id, session_id, initialize_project_git)

    return router
