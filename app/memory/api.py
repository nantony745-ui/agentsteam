"""记忆设置、管理与会话 MCP 使用同一个应用服务。"""

from typing import Annotated, Literal
from uuid import UUID

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.internal import require_caller
from app.db.storage import RequestConflictError
from app.memory.models import CloudModel, ConnectionProbe, EmployeeMemorySettings, MemorySettings, QueryInput, RetainInput, RevisionInput, VersionInput
from app.memory.service import MemoryUnavailable, error_label
from app.memory.config import ConnectionProbeError, ModelDiscoveryError, list_models, merge_config, probe, resolve_model


def create_memory_router():
    router = APIRouter()

    async def invoke(action):
        try:
            return await action
        except RequestConflictError as error:
            raise HTTPException(409, str(error)) from error
        except KeyError as error:
            raise HTTPException(404, "记忆来源或操作不存在") from error
        except MemoryUnavailable as error:
            raise HTTPException(503, str(error)) from error
        except Exception as error:
            raise HTTPException(502, error_label(error)) from error

    @router.get("/api/settings/memory")
    async def settings(request: Request):
        return request.app.state.memory.settings()

    @router.put("/api/settings/memory")
    async def save(body: MemorySettings, request: Request):
        return await invoke(request.app.state.memory.save(body))

    @router.post('/api/settings/memory/models/{component}')
    async def models(component: Literal['llm', 'embedding', 'rerank'], body: CloudModel, request: Request):
        try:
            return {'models': await list_models(body, component, request.app.state.memory.config[component])}
        except ModelDiscoveryError as error:
            raise HTTPException(422, str(error)) from error
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            message = ('服务未提供模型列表，可手动填写模型' if status in {404, 405} else
                       '模型列表访问被拒绝，请检查 API Key 和权限' if status in {401, 403} else '模型列表请求失败，请稍后重试')
            raise HTTPException(502, f'{message}（HTTP {status}）') from error
        except httpx.TimeoutException as error:
            raise HTTPException(504, '检测模型超时，请重试或手动填写') from error
        except (httpx.RequestError, ValueError, TypeError, AttributeError) as error:
            raise HTTPException(502, '无法读取模型列表，请检查 API 地址或手动填写模型') from error

    @router.post("/api/settings/memory/probe/{component}")
    async def check(component: Literal["database", "llm", "embedding", "rerank"], body: ConnectionProbe | MemorySettings, request: Request):
        try:
            current = request.app.state.memory.config
            if isinstance(body, MemorySettings):
                config = merge_config(body.model_copy(update={'enabled': False}), current)
                if component != 'database':
                    config[component] = resolve_model(getattr(body, component), component, current[component])
            elif component == 'database':
                config = {'database_url': body.database_url.get_secret_value() if body.database_url else current['database_url']}
            else:
                if body.connection is None:
                    raise ModelDiscoveryError('请填写当前模型的连接配置')
                config = {component: resolve_model(body.connection, component, current[component]),
                          'embedding_dimensions': body.embedding_dimensions}
            return await probe(config, component)
        except (ModelDiscoveryError, ConnectionProbeError) as error:
            raise HTTPException(422, str(error)) from error
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            message = ('认证失败，请检查 API Key 和权限' if status in {401, 403} else
                       '地址或模型不支持此接口，请检查 API 地址、模型名称及用途' if status in {400, 404, 405, 422} else
                       '请求受限，请稍后重试或检查额度' if status == 429 else '模型服务请求失败，请稍后重试')
            raise HTTPException(502, f'连接失败：{message}（HTTP {status}）') from error
        except httpx.TimeoutException as error:
            raise HTTPException(504, '连接超时，请检查网络或稍后重试') from error
        except httpx.RequestError as error:
            raise HTTPException(502, '无法连接模型服务，请检查 API 地址和网络') from error
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise HTTPException(422, '连接配置或服务响应不合法，请检查地址、模型和返回格式') from error
        except Exception as error:
            raise HTTPException(502, error_label(error)) from error

    def memory_scope(request: Request):
        value = request.path_params.get("employee_id")
        if value is None:
            return None
        try:
            member_id = str(UUID(value))
            request.app.state.employees.read_profile(member_id)
            return member_id
        except (ValueError, KeyError, FileNotFoundError) as error:
            raise HTTPException(404, "队员不存在") from error

    scope = Annotated[str | None, Depends(memory_scope)]

    @router.get("/api/employees/{employee_id}/memory")
    async def employee_settings(request: Request, member_id: scope):
        return await request.app.state.memory.employee_settings(member_id)

    @router.put("/api/employees/{employee_id}/memory")
    async def employee_save(body: EmployeeMemorySettings, request: Request, member_id: scope):
        async with request.app.state.employees.admission:
            request.app.state.employees.get(member_id)
            await request.app.state.memory.store.set_enabled(member_id, body.enabled)
        return await request.app.state.memory.employee_settings(member_id)

    @router.post("/api/employees/{employee_id}/memory/retain", status_code=202)
    @router.post("/api/memory/retain", status_code=202)
    async def retain(body: RetainInput, request: Request, member_id: scope):
        return await invoke(request.app.state.memory.retain(body, member_id=member_id))

    @router.post("/api/employees/{employee_id}/memory/recall")
    @router.post("/api/memory/recall")
    async def recall(body: QueryInput, request: Request, member_id: scope):
        return await invoke(request.app.state.memory.query("recall", body.query, member_id))

    @router.post("/api/employees/{employee_id}/memory/reflect")
    @router.post("/api/memory/reflect")
    async def reflect(body: QueryInput, request: Request, member_id: scope):
        return await invoke(request.app.state.memory.query("reflect", body.query, member_id))

    @router.get("/api/employees/{employee_id}/memory/facts")
    @router.get("/api/memory/facts")
    async def facts(request: Request, member_id: scope, kind: Literal["world", "experience", "observation", "all"] = "world",
                    offset: Annotated[int, Query(ge=0)] = 0):
        return await invoke(request.app.state.memory.facts(kind, offset, member_id))

    @router.get("/api/employees/{employee_id}/memory/sources")
    @router.get("/api/memory/sources")
    async def sources(request: Request, member_id: scope):
        return await request.app.state.memory.store.sources(member_id)

    @router.get("/api/employees/{employee_id}/memory/sources/{source_id}")
    @router.get("/api/memory/sources/{source_id}")
    async def source(source_id: str, request: Request, member_id: scope):
        return await invoke(request.app.state.memory.store.source(source_id, member_id))

    @router.put("/api/employees/{employee_id}/memory/sources/{source_id}", status_code=202)
    @router.put("/api/memory/sources/{source_id}", status_code=202)
    async def revise(source_id: str, body: RevisionInput, request: Request, member_id: scope):
        return await invoke(request.app.state.memory.revise(source_id, body.version, body.content, False, member_id))

    @router.delete("/api/employees/{employee_id}/memory/sources/{source_id}", status_code=202)
    @router.delete("/api/memory/sources/{source_id}", status_code=202)
    async def delete(source_id: str, body: VersionInput, request: Request, member_id: scope):
        return await invoke(request.app.state.memory.revise(source_id, body.version, "", True, member_id))

    @router.get("/api/employees/{employee_id}/memory/operations")
    @router.get("/api/memory/operations")
    async def operations(request: Request, member_id: scope):
        return await request.app.state.memory.store.operations(member_id)

    @router.post("/api/employees/{employee_id}/memory/operations/{operation_id}/retry", status_code=202)
    @router.post("/api/memory/operations/{operation_id}/retry", status_code=202)
    async def retry(operation_id: str, request: Request, member_id: scope):
        await invoke(request.app.state.memory.retry(operation_id, member_id))
        return {"status": "queued"}

    @router.get("/api/employees/{employee_id}/memory/contexts/{run_id}")
    @router.get("/api/memory/contexts/{run_id}")
    async def context(run_id: str, request: Request, member_id: scope):
        row = await request.app.state.storage._fetch_one("SELECT * FROM memory_contexts WHERE run_id=? AND member_id IS ?", (run_id, member_id))
        if row is None:
            raise HTTPException(404, "本轮没有长期记忆快照")
        return row

    caller = Annotated[dict, Depends(require_caller)]

    @router.post("/internal/memory/retain", status_code=202)
    async def internal_retain(body: RetainInput, request: Request, session: caller):
        runtime = request.app.state.service.sessions.get(session["id"])
        run_id = runtime.active.run["id"] if runtime and runtime.active else None
        return await invoke(request.app.state.memory.retain(body, session["id"], run_id))

    @router.post("/internal/memory/recall")
    async def internal_recall(body: QueryInput, request: Request, session: caller):
        return await invoke(request.app.state.memory.session_query("recall", body.query, session["id"]))

    @router.post("/internal/memory/reflect")
    async def internal_reflect(body: QueryInput, request: Request, session: caller):
        return await invoke(request.app.state.memory.session_query("reflect", body.query, session["id"]))

    return router
