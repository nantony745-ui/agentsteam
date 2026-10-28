import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.agents.acp_client import AcpClient
from app.main import create_app
from test_storage import repository
from test_http_runs import terminal
from test_harness_settings import ConfigClient, ConfigFactory, select
from app.domain.employees import EmployeeInput


def wire_client(*args):
    client = AcpClient(*args)
    client.argv = [sys.executable, str(Path(__file__).parent / "fixtures/plan_agent.py")]
    return client


async def question(client, session_id):
    async with asyncio.timeout(8):
        while True:
            detail = (await client.get(f"/api/sessions/{session_id}")).json()
            pending = [item for item in detail["interactions"] if item["kind"] == "question"]
            if pending:
                return pending[0]
            await asyncio.sleep(.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_native_plan_wire_question_recovery_and_execute(tmp_path, repository, cancel):
    app = create_app(tmp_path / "plan.db", wire_client)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            project = (await client.post("/api/projects", json={"path": str(repository)})).json()
            session = (await client.post("/api/sessions", json={"project_id": project["id"], "harness": "codex"})).json()
            url = f"/api/sessions/{session['id']}/messages"
            body = {"message": "先规划", "request_id": str(uuid4()), "collaboration_mode": "plan"}
            response = await client.post(url, json=body)
            assert response.status_code == 202, response.text
            run = response.json()["run_id"]
            pending = await question(client, session["id"])
            assert json.loads(pending["payload"])["schema"]["properties"]["choice"]["title"] == "选择方案"
            assert (await client.post(url, json=body)).json()["run_id"] == run
            assert (await client.post(url, json={**body, "collaboration_mode": "default"})).status_code == 409
            answer_url = f"/api/interactions/{pending['id']}/question-answer"
            if cancel:
                await client.post(f"/api/runs/{run}/cancel")
                assert (await terminal(client, run))["status"] == "canceled"
                assert (await client.post(answer_url, json={"action": "accept", "content": {"choice": "A"}})).status_code == 409
                return
            invalid = await client.post(answer_url, json={"action": "accept", "content": {"choice": "invalid"}})
            assert invalid.status_code == 422
            answered = await client.post(answer_url, json={"action": "accept", "content": {"choice": "A", "note": "保留说明"}})
            assert answered.status_code == 200, answered.text
            finished = await terminal(client, run)
            assert finished["status"] == "completed"
            assert "方案 A" in finished["result"]
            assert (await client.post(answer_url, json={"action": "accept", "content": {"choice": "A"}})).status_code == 409
            events = await app.state.storage._fetch_all("SELECT kind,payload FROM events WHERE run_id=? ORDER BY id", (run,))
            assert any(row["kind"] == "agent.plan" and "方案 A" in row["payload"] for row in events)
            assert [row["kind"] for row in events].count("question.requested") == 1
            next_run = (await client.post(url, json={"message": "按计划执行", "request_id": str(uuid4()), "collaboration_mode": "default"})).json()["run_id"]
            assert (await terminal(client, next_run))["status"] == "completed"
            assert app.state.service.sessions[session["id"]].client.config_options[0]["currentValue"] == "default"


class EmployeePlanClient(ConfigClient):
    async def configure_employee(self, binding):
        self.binding = binding

    async def connect(self, cwd, servers):
        result = await super().connect(cwd, servers)
        self.config_options.append(select("collaboration_mode", ["default", "plan"], "default"))
        self.config_defaults["collaboration_mode"] = "default"
        return result


@pytest.mark.asyncio
async def test_employee_plan_preserves_frozen_identity_and_permission(tmp_path, repository):
    factory = ConfigFactory()
    app = create_app(tmp_path / "employee-plan.db", lambda *args: EmployeePlanClient(factory, *args))
    async with app.router.lifespan_context(app):
        employee = app.state.employees.create(EmployeeInput(name="规划员工", harness="codex", instructions="保持职责", config_values={"mode": "read-only"}))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            project = (await client.post("/api/projects", json={"path": str(repository)})).json()
            session = (await client.post("/api/sessions", json={"project_id": project["id"], "employee_id": employee["employee_id"]})).json()
            original = app.state.employees.binding(session["id"])
            native = None
            for mode in ["plan", "default"]:
                run = (await client.post(f"/api/sessions/{session['id']}/messages", json={"message": "工作", "request_id": str(uuid4()), "collaboration_mode": mode})).json()["run_id"]
                agent, _ = await factory.started()
                assert native is None or native is agent
                native = agent
                assert agent.binding["digest"] == original["digest"]
                assert next(item["currentValue"] for item in agent.config_options if item["id"] == "collaboration_mode") == mode
                assert next(item["currentValue"] for item in agent.config_options if item["id"] == "mode") == "read-only"
                agent.finish()
                await terminal(client, run)
            assert app.state.employees.binding(session["id"]) == original


@pytest.mark.asyncio
async def test_orphan_questions_expire_on_restart(tmp_path, repository):
    from app.db.storage import Storage
    from app.domain.models import ProjectCreate, SessionCreate, MessageCreate, RunStatus
    path = tmp_path / "restart.db"
    storage = Storage(path)
    await storage.open()
    project = await storage.create_project(ProjectCreate(path=str(repository)))
    session = await storage.create_session(SessionCreate(project_id=project["id"], harness="codex"))
    run, _ = await storage.create_run(session["id"], MessageCreate(message="规划", request_id=str(uuid4()), collaboration_mode="plan"))
    await storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
    key = str(uuid4())
    await storage.create_permission(run["id"], key, {"schema": {"type": "object", "properties": {}}}, "question")
    await storage.close()
    app = create_app(path, wire_client)
    async with app.router.lifespan_context(app):
        assert (await app.state.storage.get_interaction(key))["status"] == "expired"
        assert await app.state.storage.list_interactions(session["id"]) == []
