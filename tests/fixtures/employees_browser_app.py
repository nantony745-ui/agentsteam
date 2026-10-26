import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_employees import EmployeeClient, EmployeeFactory, skill
from app.domain.models import ProjectCreate
from app.main import create_app
from test_employee_memory import BankEngine


class BrowserEmployeeClient(EmployeeClient):
    async def connect(self, cwd, servers):
        native_id = await super().connect(cwd, servers)
        model = next(item for item in self.config_options if item["id"] == "model")
        model["options"] = [
            {"value": "m1", "name": "6.1 Sol"}, {"value": "fast", "name": "6 Luna"},
            *[{"value": f"provider/model-{number}", "name": f"Provider / Model {number:02d}"} for number in range(60)],
        ]
        return native_id


class BrowserEmployeeFactory(EmployeeFactory):
    def __call__(self, *args):
        client = BrowserEmployeeClient(self, *args)
        self.clients.append(client)
        return client


directory = Path(sys.argv[1])
factory = BrowserEmployeeFactory()
app = create_app(directory / "browser.db", factory)
static = app.router.routes.pop()
original = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original(application):
        project = await app.state.storage.create_project(ProjectCreate(path=str(directory / "repo")))
        app.state.fixture = {"project_id": project["id"]}
        yield


app.router.lifespan_context = lifespan


@app.get("/fixture")
async def fixture():
    return app.state.fixture


@app.post("/fixture/skill/{employee_id}")
async def add_skill(employee_id: str):
    employee = app.state.employees.get(employee_id)
    skill(app.state.employees, employee)
    return {"ok": True}


@app.post("/fixture/finish")
async def finish():
    for client in factory.clients:
        if client.pending is not None and not client.pending.done():
            client.finish()
    return {"ok": True}


class BrowserMemoryEngine(BankEngine):
    async def facts(self, kind, offset, member_id=None):
        rows = await app.state.memory.store.sources(member_id)
        return {'items': [{'text': f'个人记忆 {member_id}', 'document_id': row['id'],
                           'created_at': row['created_at']} for row in rows if not row['deleted']]}

    async def recall(self, query, member_id=None):
        return await self.facts('world', 0, member_id)

    async def retry(self, operation_id, member_id=None):
        self.states[operation_id] = 'pending'


@app.post("/fixture/memory")
async def enable_memory():
    memory = app.state.memory
    memory.engine_factory = BrowserMemoryEngine
    memory.engine = BrowserMemoryEngine(memory.config)
    memory.config['enabled'] = True
    memory.config['database_url'] = 'postgresql://fixture:fixture@127.0.0.1/agentsteam_memory_test_browser'
    for name in ('llm', 'embedding', 'rerank'):
        memory.config[name].update(base_url='http://127.0.0.1:1', model='fixture', api_key='fixture')
    await memory.store.save_config(memory.config)
    return {'ok': True}


app.router.routes.append(static)
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[2]), log_level="error")
