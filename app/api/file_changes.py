"""回合文件变更和只读内容接口。"""

import asyncio
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.domain.models import UuidString
from app.delivery.git_delivery import read_preview
from app.services.file_changes import extract_file_changes, file_changes_summary, file_diff


async def run_files(request: Request, run_id: str, cache: OrderedDict,
                    cache_lock: asyncio.Lock) -> tuple[Path, list[dict]]:
    storage = request.app.state.storage
    run = await storage.get_run_summary(run_id)
    if run is None:
        raise HTTPException(404, "回合不存在")
    session = await storage.get_session_summary(run["session_id"])
    if session is None:
        raise HTTPException(404, "会话不存在")
    cwd = Path(session["cwd"])
    high = await storage.tool_event_high(run_id)
    key = (run_id, str(cwd), high)
    async with cache_lock:
        if key in cache:
            cache.move_to_end(key)
            return cwd, cache[key]
        events = await storage.run_tool_events(run_id, high)
        files = await asyncio.to_thread(extract_file_changes, cwd, events)
        if sum(len(item["diff"] or "") for item in files) <= 2 * 1024 * 1024:
            for previous in list(cache):
                if previous[0] == run_id:
                    del cache[previous]
            cache[key] = files
            if len(cache) > 8:
                cache.popitem(last=False)
        return cwd, files


def require_file(files: list[dict], path: str) -> dict:
    for item in files:
        if item["path"] == path:
            return item
    raise HTTPException(404, "文件不在该轮变更列表中")


def create_file_changes_router() -> APIRouter:
    router = APIRouter(prefix="/api/runs/{run_id}/file-changes")
    cache = OrderedDict()
    cache_lock = asyncio.Lock()

    @router.get("")
    async def changes(run_id: UuidString, request: Request):
        _, files = await run_files(request, run_id, cache, cache_lock)
        return file_changes_summary(run_id, files)

    @router.get("/diff")
    async def diff(run_id: UuidString, request: Request, path: str):
        _, files = await run_files(request, run_id, cache, cache_lock)
        return file_diff(require_file(files, path))

    @router.get("/preview")
    async def preview(run_id: UuidString, request: Request, path: str):
        cwd, files = await run_files(request, run_id, cache, cache_lock)
        item = require_file(files, path)
        if not item["previewable"]:
            raise HTTPException(422, "该文件已删除或不在可预览的会话目录内")
        try:
            return await asyncio.to_thread(read_preview, cwd, path)
        except FileNotFoundError as error:
            raise HTTPException(404, "文件当前不存在") from error
        except (ValueError, OSError) as error:
            raise HTTPException(422, str(error)) from error

    return router
