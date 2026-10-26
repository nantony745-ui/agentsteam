from fastapi import APIRouter, HTTPException, Request

from app.domain.models import UuidString
from app.domain.teams import TeamInput, TeamUpdate
from app.db.storage import RequestConflictError


def create_teams_router():
    router = APIRouter()

    async def invoke(request, name, *args):
        try:
            return await getattr(request.app.state.teams, name)(*args)
        except KeyError as error:
            raise HTTPException(404, "团队或员工不存在，请刷新") from error
        except RequestConflictError:
            raise
        except (ValueError, OSError) as error:
            raise HTTPException(422, str(error)) from error

    @router.get("/api/teams")
    async def teams(request: Request):
        return await invoke(request, "list")

    @router.post("/api/teams", status_code=201)
    async def create(body: TeamInput, request: Request):
        return await invoke(request, "save", body)

    @router.get("/api/teams/{team_id}")
    async def detail(team_id: UuidString, request: Request):
        return await invoke(request, "get", team_id)

    @router.put("/api/teams/{team_id}")
    async def update(team_id: UuidString, body: TeamUpdate, request: Request):
        return await invoke(request, "save", body, team_id)

    @router.delete("/api/teams/{team_id}")
    async def delete(team_id: UuidString, request: Request):
        return await invoke(request, "delete", team_id)

    @router.get("/api/employees/{employee_id}/capabilities")
    async def capabilities(employee_id: UuidString, request: Request):
        return await invoke(request, "capabilities", employee_id)

    @router.get("/api/sessions/{session_id}/team")
    async def roster(session_id: UuidString, request: Request):
        return await invoke(request, "roster", session_id)

    return router
