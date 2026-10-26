from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from app.domain.employees import EmployeeInput, EmployeeUpdate
from app.domain.models import UuidString


def create_employees_router() -> APIRouter:
    router = APIRouter(prefix="/api/employees")

    def invoke(request, action, *args):
        try:
            return getattr(request.app.state.employees, action)(*args)
        except (KeyError, FileNotFoundError) as error:
            raise HTTPException(404, "队员或技能不存在，请刷新") from error
        except (OSError, UnicodeError, ValueError) as error:
            from app.db.storage import RequestConflictError
            if isinstance(error, RequestConflictError):
                raise
            raise HTTPException(422, str(error)) from error

    @router.get("")
    async def employees(request: Request):
        return invoke(request, "list")

    @router.post("", status_code=201)
    async def create(body: EmployeeInput, request: Request):
        return invoke(request, "create", body)

    @router.get("/{employee_id}")
    async def detail(employee_id: UuidString, request: Request):
        return invoke(request, "get", employee_id)

    @router.put("/{employee_id}")
    async def update(employee_id: UuidString, body: EmployeeUpdate, request: Request):
        return invoke(request, "update", employee_id, body)

    @router.delete("/{employee_id}")
    async def delete(employee_id: UuidString, version: Annotated[int, Query(ge=1)], request: Request):
        async with request.app.state.employees.admission:
            async with request.app.state.storage.transaction() as db:
                result = invoke(request, "delete", employee_id, version)
                await db.execute("INSERT INTO employee_memory_settings VALUES (?,0) "
                                 "ON CONFLICT(member_id) DO UPDATE SET enabled=0", (employee_id,))
            return result

    @router.get("/{employee_id}/skills")
    async def skills(employee_id: UuidString, request: Request):
        return invoke(request, "skills", employee_id)

    @router.get("/{employee_id}/skills/{skill_id}")
    async def skill(employee_id: UuidString, skill_id: str, request: Request):
        return invoke(request, "read_skill", employee_id, skill_id)

    return router
