import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.db.storage import RequestConflictError
from app.domain.employees import EmployeeInput, EmployeeUpdate
from app.main import create_app
from app.services.employees import Employees
from app.services.orchestrator import SessionUnavailableError
from test_employees import EmployeeFactory, skill
from test_http_runs import terminal
from test_storage import repository
from test_teams import task, team_api


def test_soft_delete_preserves_files_bindings_and_restart(tmp_path):
    service = Employees(tmp_path / "employees")
    employee = service.create(EmployeeInput(name="研发", harness="codex"))
    employee_id = employee["employee_id"]
    skill(service, employee)
    binding = service.prepare(employee_id, str(uuid4()), {"config_values": {}, "revision": 0}, tmp_path)
    root = service.directory(employee_id)
    original = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(RequestConflictError, match="已更新"):
        service.delete(employee_id, 2)
    assert service.get(employee_id) == employee
    assert service.delete(employee_id, 1) == {"deleted_employee_id": employee_id}
    assert service.delete(employee_id, 1) == {"deleted_employee_id": employee_id}
    restarted = Employees(service.root)
    assert restarted.list() == []
    assert restarted.binding(binding["session_id"]) == binding
    assert restarted.read_profile(employee_id) == {**employee, "deleted": True, "version": 2}
    assert set(original) == {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
    for name, contents in original.items():
        if name != Path("profile.json"):
            assert (root / name).read_bytes() == contents
    with pytest.raises(RequestConflictError, match="已被删除"):
        restarted.get(employee_id)
    with pytest.raises(RequestConflictError, match="已被删除"):
        restarted.update(employee_id, EmployeeUpdate(name="复活", version=2))
    with pytest.raises(RequestConflictError, match="已被删除"):
        restarted.prepare(employee_id, str(uuid4()), {"config_values": {}}, tmp_path)


def test_failed_delete_keeps_profile_available(tmp_path, monkeypatch):
    service = Employees(tmp_path / "employees")
    employee = service.create(EmployeeInput(name="保留"))
    def fail_replace(*args):
        raise OSError("受控写入失败")
    monkeypatch.setattr("app.services.employees.os.replace", fail_replace)
    with pytest.raises(OSError, match="受控写入失败"):
        service.delete(employee["employee_id"], 1)
    assert service.get(employee["employee_id"]) == employee
    assert service.list() == [employee]


@pytest.mark.asyncio
async def test_delete_http_keeps_history_and_rejects_new_session(tmp_path, repository):
    factory = EmployeeFactory()
    app = create_app(tmp_path / "delete.db", factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as http:
            employee = (await http.post("/api/employees", json={"name": "历史队员", "harness": "codex"})).json()
            employee_id = employee["employee_id"]
            project = (await http.post("/api/projects", json={"path": str(repository)})).json()
            body = {"project_id": project["id"], "employee_id": employee_id}
            session = (await http.post("/api/sessions", json=body)).json()
            message = {"message": "历史任务", "request_id": str(uuid4())}
            run = (await http.post(f"/api/sessions/{session['id']}/messages", json=message)).json()
            client, _ = await factory.started()
            endpoint = f"/api/employees/{employee_id}"
            assert (await http.delete(endpoint)).status_code == 422
            assert (await http.delete(endpoint + "?version=0")).status_code == 422
            assert (await http.delete(endpoint + "?version=2")).status_code == 409
            assert (await http.delete(endpoint + "?version=1")).status_code == 200
            client.finish("历史结果")
            await terminal(http, run["run_id"])
            assert (await http.get("/api/employees")).json() == []
            for suffix in ("", "/skills"):
                response = await http.get(endpoint + suffix)
                assert response.status_code == 409 and "已被删除" in response.json()["detail"]
            response = await http.post("/api/sessions", json=body)
            assert response.status_code == 409 and "已被删除" in response.json()["detail"]
            history = (await http.get(f"/api/sessions/{session['id']}/history")).json()
            assert history["runs"][0]["id"] == run["run_id"]
            detail = (await http.get(f"/api/sessions/{session['id']}")).json()
            assert detail["session"]["employee_name"] == "历史队员"
            assert (await http.delete(endpoint + "?version=1")).status_code == 200
            assert (await http.delete(f"/api/employees/{uuid4()}?version=1")).status_code == 404


@pytest.mark.asyncio
async def test_deleted_team_member_rejects_new_references_and_keeps_roster(team_api):
    http, app, factory, employees, team, project, leader = team_api
    member = employees[1]
    handle = await app.state.service.delegate_task(leader["id"], task(member))
    client, _ = await factory.started()
    client.finish("历史委派结果")
    await terminal(http, handle["run_id"])
    response = await http.delete(f"/api/employees/{member['employee_id']}?version=1")
    assert response.status_code == 200
    roster = (await http.get(f"/api/sessions/{leader['id']}/team")).json()
    deleted = next(item for item in roster["members"] if item["profile"]["employee_id"] == member["employee_id"])
    assert deleted["state"] == "deleted" and deleted["profile"]["name"] == "队员已被删除"
    assert set(deleted) == {"profile", "state"}
    assert deleted["profile"]["deleted"] and deleted["profile"]["instructions"] == ""
    assert any(item["id"] == handle["child_session_id"] for item in roster["sessions"])
    body = {key: team[key] for key in ("name", "member_ids", "leader_id")}
    requests = [
        http.post("/api/teams", json=body),
        http.put(f"/api/teams/{team['id']}", json={**body, "version": team["version"]}),
        http.post("/api/projects", json={"path": project["path"], "team_id": team["id"]}),
        http.post("/api/sessions", json={"project_id": project["id"], "employee_id": member["employee_id"],
                                         "leader_session_id": leader["id"]}),
    ]
    for response in await asyncio.gather(*requests):
        assert response.status_code == 409 and "已被删除" in response.json()["detail"]
    for mode in ("new", "continue"):
        with pytest.raises(SessionUnavailableError, match="已被删除"):
            await app.state.service.delegate_task(leader["id"], task(member, context_mode=mode))
    remaining = [item for item in team["member_ids"] if item != member["employee_id"]]
    response = await http.put(f"/api/teams/{team['id']}", json={**body, "member_ids": remaining, "version": team["version"]})
    assert response.status_code == 200
    assert (await http.get("/api/projects")).json()[0]["team"]["member_ids"] == team["member_ids"]
    await http.delete(f"/api/employees/{employees[0]['employee_id']}?version=1")
    response = await http.post("/api/sessions", json={"project_id": project["id"], "employee_id": employees[0]["employee_id"]})
    assert response.status_code == 409 and "已被删除" in response.json()["detail"]
    assert (await http.get(f"/api/sessions/{leader['id']}/team")).status_code == 200
    with pytest.raises(SessionUnavailableError, match="已被删除"):
        await app.state.service.delegate_task(leader["id"], task(employees[2]))


@pytest.mark.asyncio
async def test_project_reference_and_delete_are_serialized(team_api, tmp_path, monkeypatch):
    http, app, _, employees, team, project, _ = team_api
    directory = tmp_path / "new-project"
    shutil.copytree(project["path"], directory)
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = app.state.teams.snapshot
    async def paused(team_id):
        value = await snapshot(team_id)
        entered.set()
        await release.wait()
        return value
    monkeypatch.setattr(app.state.teams, "snapshot", paused)
    creating = asyncio.create_task(http.post("/api/projects", json={"path": str(directory), "team_id": team["id"]}))
    await asyncio.wait_for(entered.wait(), 5)
    deleting = asyncio.create_task(http.delete(f"/api/employees/{employees[1]['employee_id']}?version=1"))
    try:
        await asyncio.sleep(0)
        assert not deleting.done()
    finally:
        release.set()
    assert (await creating).status_code == 201
    assert (await deleting).status_code == 200
    response = await http.post("/api/projects", json={"path": str(directory), "team_id": team["id"]})
    assert response.status_code == 409 and "已被删除" in response.json()["detail"]
