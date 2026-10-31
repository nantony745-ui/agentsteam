import httpx
import pytest

from app.main import create_app
from test_employees import EmployeeFactory
from test_storage import repository


@pytest.mark.asyncio
async def test_configured_models_available_before_prompt_and_after_restart(tmp_path, repository):
    factory = EmployeeFactory()
    database = tmp_path / "composer.db"
    app = create_app(database, factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            await app.state.storage.save_harness_settings("codex", {"model": "m1"})
            project = (await client.post("/api/projects", json={"path": str(repository)})).json()
            employee = (await client.post("/api/employees", json={"name": "员工", "harness": "codex", "config_values": {"model": "fast"}})).json()
            ordinary = (await client.post("/api/sessions", json={"project_id": project["id"], "harness": "codex"})).json()
            bound = (await client.post("/api/sessions", json={"project_id": project["id"], "employee_id": employee["employee_id"]})).json()
            assert bound["configured_model"] == "fast"
            for session, model in ((ordinary, "m1"), (bound, "fast")):
                response = await client.get(f"/api/sessions/{session['id']}?summary=true")
                assert response.status_code == 200
                assert response.json()["session"]["configured_model"] == model
                assert response.json()["runs"] == []
            body = {key: value for key, value in employee.items() if key != "employee_id"}
            body["config_values"] = {"model": "new-profile-model"}
            assert (await client.put(f"/api/employees/{employee['employee_id']}", json=body)).status_code == 200
            await app.state.storage.save_harness_settings("codex", {"model": "new-global-model"})
            assert factory.clients == []
    restarted = create_app(database, factory)
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(restarted), base_url="http://test") as client:
            for session, model in ((ordinary, "new-global-model"), (bound, "fast")):
                response = await client.get(f"/api/sessions/{session['id']}?summary=true")
                assert response.json()["session"]["configured_model"] == model
            assert factory.clients == []
