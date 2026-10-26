"""应用装配与生命周期；启动入口为 app.main:app。"""

from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from app.agents.acp_client import AcpClient
from app.agents.diagnostics import ProtocolDiagnostics
from app.api.internal import create_internal_router
from app.api.file_changes import create_file_changes_router
from app.api.project_git import create_project_git_router
from app.api.routes import StreamSettings, create_router
from app.api.settings import create_settings_router
from app.api.employees import create_employees_router
from app.services.employees import Employees
from app.services.teams import Teams
from app.api.teams import create_teams_router
from app.memory.api import create_memory_router
from app.memory.service import MemoryService
from app.db.storage import RequestConflictError, Storage
from app.delivery.git_delivery import DeliveryConflictError
from app.services.orchestrator import ClientFactory, Orchestrator, SessionUnavailableError
from app.services.harness_settings import HarnessSettings
from app.services.harness_skills import HarnessSkills
from app.services.harness_mcp import HarnessMcp


def create_app(database_path: Path = Path(__file__).resolve().parents[1] / "data/lab.db",
               client_factory: ClientFactory = AcpClient,
               stream_settings: StreamSettings = StreamSettings(), *,
               internal_base_url: str = "http://127.0.0.1:8765",
               diagnostic_path: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        with ExitStack() as resources:
            diagnostics = ProtocolDiagnostics(diagnostic_path) if diagnostic_path is not None else None
            if diagnostics is not None:
                resources.callback(diagnostics.close)
            storage = Storage(database_path, exclusive=True)
            await storage.open()
            service = Orchestrator(storage, client_factory, internal_base_url)
            employees = Employees(database_path.parent / "employees")
            service.employees = employees
            application.state.employees = employees
            memory = MemoryService(storage)
            memory.employees = employees
            storage.memory_owner = memory.owner
            service.memory = memory
            application.state.memory = memory
            service.diagnostics = diagnostics
            application.state.storage = storage
            application.state.service = service
            settings = HarnessSettings(storage, client_factory, Path(__file__).resolve().parents[1])
            application.state.harness_settings = settings
            application.state.harness_skills = HarnessSkills()
            application.state.harness_mcp = HarnessMcp()
            teams = Teams(storage, employees, application.state.harness_skills, application.state.harness_mcp, service.sessions)
            service.teams = teams
            application.state.teams = teams
            try:
                await service.start()
                await memory.start()
                yield
            finally:
                try:
                    await settings.close()
                    await service.close()
                finally:
                    try:
                        await memory.close()
                    finally:
                        await storage.close()

    application = FastAPI(lifespan=lifespan)
    application.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=3)

    @application.exception_handler(RequestValidationError)
    async def validation(request: Request, error: RequestValidationError):
        if request.url.path.startswith(("/api/settings/memory", "/api/memory", "/internal/memory")):
            return JSONResponse(status_code=422, content={"detail": "记忆请求字段或配置不合法，请检查输入"})
        return await request_validation_exception_handler(request, error)

    @application.exception_handler(RequestConflictError)
    @application.exception_handler(DeliveryConflictError)
    @application.exception_handler(SessionUnavailableError)
    async def conflict(request: Request, error: ValueError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    application.include_router(create_router(stream_settings))
    application.include_router(create_file_changes_router())
    application.include_router(create_project_git_router())
    application.include_router(create_settings_router())
    application.include_router(create_employees_router())
    application.include_router(create_teams_router())
    application.include_router(create_memory_router())
    application.include_router(create_internal_router(stream_settings.poll_seconds))
    application.mount(
        "/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True),
        name="workbench",
    )
    return application


app = create_app()
