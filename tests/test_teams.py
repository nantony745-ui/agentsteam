import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.main import create_app
from app.domain.models import MessageCreate, ProjectCreate, TaskCreate
from app.services.orchestrator import SessionUnavailableError
from app.services.employees import filesystem_path
from app.db.storage import RequestConflictError
from test_employees import EmployeeFactory
from test_http_runs import terminal
from test_storage import repository


@pytest_asyncio.fixture
async def team_api(tmp_path, repository):
    factory = EmployeeFactory()
    app = create_app(tmp_path / "team.db", factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as http:
            employees = [(await http.post("/api/employees", json={"name":name,"role":"工程师","instructions":"先核验证据","harness":"codex"})).json() for name in ("队长","研发","核验","设计")]
            body = {"name":"研发团队","member_ids":[item["employee_id"] for item in employees],"leader_id":employees[0]["employee_id"]}
            response = await http.post("/api/teams", json=body)
            assert response.status_code == 201, response.text
            team = response.json()
            project = (await http.post("/api/projects", json={"path":str(repository),"team_id":team["id"]})).json()
            leader = (await http.post("/api/sessions", json={"project_id":project["id"],"employee_id":employees[0]["employee_id"]})).json()
            yield http, app, factory, employees, team, project, leader


def task(employee, **values):
    return TaskCreate(employee_id=employee["employee_id"], goal="检查实现", acceptance="返回证据", request_id=str(uuid4()), **values)


@pytest.mark.asyncio
async def test_template_validation_version_and_project_snapshot(team_api):
    http, app, factory, employees, team, project, leader = team_api
    assert not factory.clients
    assert len(project["team"]["members"]) == 4
    body = {key:team[key] for key in ("name","member_ids","leader_id","version")}
    invalid = {**body, "member_ids":[employees[0]["employee_id"]]*2}
    assert (await http.post("/api/teams", json={k:v for k,v in invalid.items() if k != "version"})).status_code == 422
    assert (await http.put(f"/api/teams/{team['id']}", json={**body,"name":"新版","member_ids":[employees[0]["employee_id"]]})).status_code == 200
    assert (await http.put(f"/api/teams/{team['id']}", json=body)).status_code == 409
    assert (await http.delete(f"/api/teams/{team['id']}")).status_code == 200
    stored = (await http.get("/api/projects")).json()[0]
    assert stored["team"]["name"] == "研发团队" and len(stored["team"]["member_ids"]) == 4
    roster = (await http.get(f"/api/sessions/{leader['id']}/team")).json()
    assert len(roster["members"]) == 4 and isinstance(roster["high_watermark"],int)
    identity = filesystem_path(Path(app.state.employees.binding(leader["id"])["identity_file"])).read_text(encoding="utf-8")
    assert "<agentsteam_team>" in identity and "研发" in identity and "agentsteam-skill-stewardship" in identity


@pytest.mark.asyncio
async def test_leader_context_and_mcp_roster_use_member_profiles_without_discovery(team_api, monkeypatch):
    http, app, factory, employees, _, project, _ = team_api

    async def unexpected_discovery(*args):
        pytest.fail("队长资料和名单查询不应扫描成员能力目录")

    monkeypatch.setattr(app.state.teams, "capabilities", unexpected_discovery)
    response = await http.post("/api/sessions", json={"project_id": project["id"], "employee_id": employees[0]["employee_id"]})
    assert response.status_code == 201, response.text
    leader = response.json()
    binding = app.state.employees.binding(leader["id"])
    identity = filesystem_path(Path(binding["identity_file"])).read_text(encoding="utf-8")
    profiles = [json.loads(line) for line in identity.splitlines() if line.startswith("{")]
    expected_keys = {"employee_id", "name", "role", "instructions"}
    assert len(profiles) == len(employees) - 1
    assert all(set(profile) == expected_keys for profile in profiles)
    assert {profile["employee_id"] for profile in profiles} == {item["employee_id"] for item in employees[1:]}
    assert all(profile["instructions"] == "先核验证据" for profile in profiles)

    roster = (await http.get(f"/api/sessions/{leader['id']}/team")).json()
    assert "members" not in roster["team"]
    assert all(member["profile"]["harness"] == "codex" for member in roster["members"])
    assert all(set(member) == {"profile", "state"} for member in roster["members"])
    run = await app.state.service.submit(leader["id"], MessageCreate(message="核对队员资料", request_id=str(uuid4())))
    client, _ = await factory.started()
    token = app.state.service.sessions[leader["id"]].mcp_token
    response = await http.post("/internal/team/members", json={}, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200, response.text
    members = response.json()["members"]
    assert len(members) == len(employees)
    assert all(set(member["profile"]) == expected_keys for member in members)
    client.finish()
    await terminal(http, run["id"])


def test_member_profile_uses_the_same_legacy_guidance_as_employee_editor():
    from app.services.teams import member_profile

    summary = member_profile({"employee_id": "writer", "name": "写作员", "role": "文档",
                              "instructions": "核对来源", "responsibilities": "编写文档",
                              "principles": "依据事实", "growth": "改进表达", "config_values": {"model": "private"}})
    assert summary == {"employee_id": "writer", "name": "写作员", "role": "文档",
                       "instructions": "核对来源\n\n专业职责：\n编写文档\n\n工作准则：\n依据事实\n\n成长课题：\n改进表达"}


@pytest.mark.asyncio
async def test_unconfigured_employee_and_invalid_project_leave_no_rows(team_api, tmp_path):
    http, app, _, _, _, _, _ = team_api
    empty = (await http.post("/api/employees", json={"name":"未配置"})).json()
    assert (await http.post("/api/teams", json={"name":"失败","member_ids":[empty["employee_id"]],"leader_id":empty["employee_id"]})).status_code == 422
    response = await http.post("/api/projects", json={"path":str(tmp_path / "missing"),"team_id":str(uuid4())})
    assert response.status_code == 404
    assert len((await http.get("/api/projects")).json()) == 1


@pytest.mark.asyncio
async def test_continue_fifo_new_context_and_task_results(team_api):
    http, app, factory, employees, _, _, leader = team_api
    first_data = task(employees[1])
    first = await app.state.service.delegate_task(leader["id"],first_data)
    worker, text = await factory.started()
    second = await app.state.service.delegate_task(leader["id"],task(employees[1]))
    third = await app.state.service.delegate_task(leader["id"],task(employees[2]))
    parallel, _ = await factory.started()
    assert first["child_session_id"] == second["child_session_id"] != third["child_session_id"]
    assert factory.starts.empty()
    assert (await app.state.storage.get_run(second["run_id"]))["status"] == "queued"
    assert "队长" not in text
    worker.finish("第一次结果")
    await terminal(http, first["run_id"])
    reused, _ = await factory.started()
    assert reused is worker
    parallel.finish("并行结果"); reused.finish("第二次结果")
    await terminal(http, second["run_id"]); await terminal(http, third["run_id"])
    rows = await app.state.storage.get_task_snapshots(leader["id"],[first["task_id"],second["task_id"]])
    assert [row["result"] for row in rows] == ["第一次结果","第二次结果"]
    assert all(row["status"] == "review" for row in rows)
    assert (await app.state.service.delegate_task(leader["id"],first_data))["task_id"] == first["task_id"]
    fresh = await app.state.service.delegate_task(leader["id"],task(employees[1],context_mode="new"))
    agent, _ = await factory.started()
    assert fresh["child_session_id"] != first["child_session_id"]
    agent.finish(); await terminal(http,fresh["run_id"])
    with pytest.raises(ValueError):
        await app.state.service.delegate_task(leader["id"],first_data.model_copy(update={"context_mode":"new"}))


@pytest.mark.asyncio
async def test_scope_configuration_native_identity_and_unavailable_history(team_api):
    http, app, factory, employees, _, project, leader = team_api
    outsider = (await http.post("/api/employees",json={"name":"外部","harness":"codex"})).json()
    with pytest.raises(PermissionError):
        await app.state.service.delegate_task(leader["id"],task(outsider))
    direct = (await http.post("/api/sessions",json={"project_id":project["id"],"employee_id":employees[1]["employee_id"],"leader_session_id":leader["id"]})).json()
    assert direct["parent_id"] == leader["id"]
    with pytest.raises(PermissionError):
        await app.state.service.delegate_task(direct["id"],task(employees[2]))
    handle = await app.state.service.delegate_task(leader["id"],task(employees[1]))
    assert handle["child_session_id"] == direct["id"]
    agent, _ = await factory.started()
    identity = filesystem_path(Path(agent.employee_binding["identity_file"])).read_text(encoding="utf-8")
    assert "你是团队员工" in identity and "先核验证据" in identity
    bridge = agent.servers[0]
    assert any(item.name == "HARNESS_LAB_TEAM" and item.value == "1" for item in bridge.env)
    agent.finish(); await terminal(http,handle["run_id"])
    app.state.service.sessions[direct["id"]].unavailable = True
    with pytest.raises(SessionUnavailableError,match="context_mode=new"):
        await app.state.service.delegate_task(leader["id"],task(employees[1]))
    other = (await http.post("/api/sessions",json={"project_id":project["id"],"employee_id":employees[0]["employee_id"]})).json()
    isolated = await app.state.service.delegate_task(other["id"],task(employees[1]))
    next_agent, _ = await factory.started()
    assert isolated["child_session_id"] != direct["id"]
    next_agent.finish(); await terminal(http,isolated["run_id"])


@pytest.mark.asyncio
async def test_team_creation_rolls_back_and_different_directory_isolated(team_api, tmp_path):
    http, app, factory, employees, _, _, leader = team_api
    original = await app.state.service.delegate_task(leader["id"],task(employees[1]))
    agent, _ = await factory.started(); agent.finish(); await terminal(http,original["run_id"])
    directory = tmp_path / "other"; directory.mkdir()
    different = await app.state.service.delegate_task(leader["id"],task(employees[1],working_dir=str(directory)))
    second, _ = await factory.started(); second.finish(); await terminal(http,different["run_id"])
    assert original["child_session_id"] != different["child_session_id"]
    before = len((await http.get("/api/sessions")).json())
    with pytest.raises(ValueError):
        await app.state.service.delegate_task(leader["id"],task(employees[2],working_dir=str(tmp_path/"missing")))
    assert len((await http.get("/api/sessions")).json()) == before
    bindings = list(app.state.employees.root.glob("*/sessions/*/binding.json"))
    assert len(bindings) == before


@pytest.mark.asyncio
async def test_cancel_and_direct_chat_do_not_rewrite_other_tasks(team_api):
    http, app, factory, employees, _, _, leader = team_api
    first = await app.state.service.delegate_task(leader["id"], task(employees[1]))
    agent, _ = await factory.started()
    queued = await app.state.service.delegate_task(leader["id"], task(employees[1]))
    await app.state.service.cancel(queued["run_id"])
    assert (await app.state.storage.get_task(first["task_id"]))["status"] == "running"
    assert (await app.state.storage.get_task(queued["task_id"]))["status"] == "canceled"
    agent.finish("独立交付"); await terminal(http, first["run_id"])
    direct = await app.state.service.submit(first["child_session_id"], MessageCreate(message="用户直接交流", request_id=str(uuid4())))
    reused, prompt = await factory.started()
    assert reused is agent and prompt == "用户直接交流"
    reused.finish(); await terminal(http, direct["id"])
    assert (await app.state.storage.get_task(first["task_id"]))["status"] == "review"
    assert (await app.state.storage.get_task(queued["task_id"]))["status"] == "canceled"
    assert (await app.state.storage.get_run(direct["id"]))["task_id"] is None


@pytest.mark.asyncio
async def test_continue_queues_while_first_native_handshake_is_in_progress(team_api):
    http, app, factory, employees, _, _, leader = team_api
    factory.connect_release.clear()
    first = await app.state.service.delegate_task(leader['id'],task(employees[1]))
    await asyncio.wait_for(factory.connect_started.wait(),3)
    second = await app.state.service.delegate_task(leader['id'],task(employees[1]))
    assert first['child_session_id'] == second['child_session_id']
    assert (await app.state.storage.get_run(second['run_id']))['status'] == 'queued'
    factory.connect_release.set()
    agent, _ = await factory.started(); agent.finish(); await terminal(http,first['run_id'])
    reused, _ = await factory.started(); assert reused is agent
    reused.finish(); await terminal(http,second['run_id'])


@pytest.mark.asyncio
async def test_default_new_and_member_capability_state(team_api):
    http, app, factory, employees, _, project, leader = team_api
    async with app.state.storage.transaction() as db:
        await db.execute("UPDATE project_teams SET context_mode='new' WHERE project_id=?", (project["id"],))
    initial = (await http.get(f"/api/sessions/{leader['id']}/team")).json()
    assert initial["sessions"][0]["capability_state"]["connection"] == "not_started"
    handles = []
    for _ in range(2):
        handle = await app.state.service.delegate_task(leader["id"], task(employees[1]))
        agent, _ = await factory.started()
        handles.append(handle)
        roster = (await http.get(f"/api/sessions/{leader['id']}/team")).json()
        state = next(item for item in roster["sessions"] if item["id"] == handle["child_session_id"])["capability_state"]
        assert state["connection"] == "active" and state["loaded_skills"] == "not_reported"
        agent.finish(); await terminal(http, handle["run_id"])
    assert handles[0]["child_session_id"] != handles[1]["child_session_id"]
    assert (await http.post('/api/sessions', json={"project_id":project["id"],"harness":"codex"})).status_code == 422


@pytest.mark.asyncio
async def test_project_transaction_and_binding_failure_cleanup(team_api):
    http, app, _, employees, team, project, leader = team_api
    snapshot = await app.state.teams.snapshot(team["id"])
    async with app.state.storage.transaction() as db:
        await db.execute("UPDATE teams SET version=version+1 WHERE id=?", (team["id"],))
    with pytest.raises(RequestConflictError):
        await app.state.storage.create_project(ProjectCreate(path=project["path"],team_id=team["id"]),snapshot)
    assert len(await app.state.storage.list_projects()) == 1
    from test_git_delivery import worktree
    root = await worktree(Path(project["path"]))
    async with app.state.storage.transaction() as db:
        await db.execute("CREATE TRIGGER reject_project_team BEFORE INSERT ON project_teams BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    response = await http.post('/api/projects',json={"path":str(root),"team_id":team["id"]})
    assert response.status_code == 409
    assert len(await app.state.storage.list_projects()) == 1
    async with app.state.storage.transaction() as db:
        await db.execute("CREATE TRIGGER reject_team_session BEFORE INSERT ON team_sessions BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await app.state.service.delegate_task(leader["id"], task(employees[1]))
    assert len(await app.state.storage.list_sessions(project["id"])) == 1
    assert len(list(app.state.employees.root.glob('*/sessions/*/binding.json'))) == 1


@pytest.mark.asyncio
async def test_project_snapshot_version_conflict_returns_409(team_api, monkeypatch):
    http, app, _, _, team, project, _ = team_api
    original = app.state.teams.snapshot
    async def changed(team_id):
        snapshot = await original(team_id)
        async with app.state.storage.transaction() as db:
            await db.execute('UPDATE teams SET version=version+1 WHERE id=?',(team_id,))
        return snapshot
    monkeypatch.setattr(app.state.teams,'snapshot',changed)
    response = await http.post('/api/projects',json={"path":project['path'],"team_id":team['id']})
    assert response.status_code == 409 and '团队配置已变更' in response.json()['detail']


@pytest.mark.asyncio
async def test_reused_worktree_keeps_separate_task_baselines(team_api):
    from test_git_delivery import worktree
    from app.delivery.git_delivery import git
    http, app, factory, employees, _, project, leader = team_api
    root = await worktree(Path(project["path"]))
    first = await app.state.service.delegate_task(leader["id"], task(employees[1], working_dir=str(root)))
    agent, _ = await factory.started(); agent.finish(); await terminal(http, first["run_id"])
    first_task = await app.state.storage.get_task(first["task_id"])
    await git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'next baseline')
    second = await app.state.service.delegate_task(leader["id"], task(employees[1], working_dir=str(root)))
    agent, _ = await factory.started(); agent.finish(); await terminal(http, second["run_id"])
    second_task = await app.state.storage.get_task(second["task_id"])
    assert first["child_session_id"] == second["child_session_id"]
    assert first_task["base_sha"] != second_task["base_sha"]
    for handle, record in ((first,first_task),(second,second_task)):
        diff = (await http.get(f"/api/tasks/{handle['task_id']}/diff")).json()
        assert diff["task_id"] == handle["task_id"] and diff["base_sha"] == record["base_sha"]


@pytest.mark.asyncio
async def test_restart_keeps_team_and_frozen_context_without_replacing_native_identity(tmp_path, repository):
    from app.domain.employees import EmployeeInput
    from app.domain.teams import TeamInput
    from app.domain.models import SessionCreate
    factory = EmployeeFactory()
    db_path = tmp_path / 'restart.db'
    app = create_app(db_path, factory)
    async with app.router.lifespan_context(app):
        people = [app.state.employees.create(EmployeeInput(name=name,harness='codex')) for name in ('队长','员工')]
        team = await app.state.teams.save(TeamInput(name='重启团队',member_ids=[item['employee_id'] for item in people],leader_id=people[0]['employee_id']))
        snapshot = await app.state.teams.snapshot(team['id'])
        project = await app.state.storage.create_project(ProjectCreate(path=str(repository),team_id=team['id']),snapshot)
        leader = await app.state.teams.create_session(SessionCreate(project_id=project['id'],employee_id=people[0]['employee_id']))
        handle = await app.state.service.delegate_task(leader['id'],task(people[1]))
        agent, _ = await factory.started(); agent.finish()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as http:
            await terminal(http,handle['run_id'])
        binding = app.state.employees.binding(handle['child_session_id'])
        await app.state.teams.delete(team['id'])
    next_factory = EmployeeFactory()
    restarted = create_app(db_path,next_factory)
    async with restarted.router.lifespan_context(restarted):
        assert not next_factory.clients
        assert (await restarted.state.teams.project(project['id']))['member_ids'] == team['member_ids']
        assert restarted.state.employees.binding(handle['child_session_id'])['digest'] == binding['digest']
        roster = await restarted.state.teams.roster(leader['id'])
        worker = next(item for item in roster['sessions'] if item['id'] == handle['child_session_id'])
        assert worker['capability_state']['connection'] == 'unavailable'
        with pytest.raises(SessionUnavailableError,match='context_mode=new'):
            await restarted.state.service.delegate_task(leader['id'],task(people[1]))
