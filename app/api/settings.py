"""Harness 默认配置与实时能力探测接口。"""

from acp.exceptions import RequestError
from fastapi import APIRouter, HTTPException, Request

from app.domain.models import Harness, HarnessProbeInput, HarnessSettingsInput


def create_settings_router() -> APIRouter:
    router = APIRouter(prefix="/api/settings/harnesses")

    @router.get("")
    async def list_harnesses(request: Request):
        return await request.app.state.harness_settings.list()

    async def skills_response(request, harness, skill_id):
        service = request.app.state.harness_skills
        try:
            if skill_id is None:
                return await service.list(harness)
            return await service.read(harness, skill_id)
        except (KeyError, FileNotFoundError) as error:
            raise HTTPException(404, "Skill 不存在，请刷新列表") from error
        except (ValueError, UnicodeError) as error:
            raise HTTPException(422, str(error)) from error
        except OSError as error:
            raise HTTPException(422, f"无法读取技能文档：{error.strerror or error}") from error

    @router.get("/{harness}/skills")
    async def skills(harness: Harness, request: Request):
        return await skills_response(request, harness, None)

    @router.get("/{harness}/skills/{skill_id}")
    async def skill(harness: Harness, skill_id: str, request: Request):
        return await skills_response(request, harness, skill_id)

    @router.get("/{harness}/mcp")
    async def mcp(harness: Harness, request: Request):
        return await request.app.state.harness_mcp.list(harness)

    async def invoke(request, harness, body, save):
        service = request.app.state.harness_settings
        try:
            if save:
                return await service.save(harness, body.project_id, body.config_values)
            return await service.probe(harness, body.project_id, body.config_values, body.force)
        except KeyError as error:
            raise HTTPException(404, "项目或 Harness 不存在") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except (OSError, RequestError, TimeoutError, ConnectionError, RuntimeError) as error:
            raise HTTPException(502, str(error) or "Harness 检测超时，请重试") from error

    @router.post("/{harness}/probe")
    async def probe(harness: Harness, body: HarnessProbeInput, request: Request):
        return await invoke(request, harness, body, False)

    @router.put("/{harness}")
    async def save(harness: Harness, body: HarnessSettingsInput, request: Request):
        return await invoke(request, harness, body, True)

    return router
