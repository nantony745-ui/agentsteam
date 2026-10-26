from uuid import uuid4
from pathlib import Path

import httpx
import pytest

from app.domain.employees import EmployeeInput, EmployeeUpdate
from app.main import create_app
from app.services.employees import Employees, atomic_json, filesystem_path
from app.db.storage import RequestConflictError
from test_harness_settings import ConfigClient, ConfigFactory
from test_http_runs import terminal
from test_storage import repository


def skill(service, employee, name="personal-unique", text="first"):
    directory = service.directory(employee["employee_id"]) / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Private skill\n---\n{text}", encoding="utf-8")
    (directory / "references").mkdir(exist_ok=True)
    (directory / "references/data.txt").write_text(text, encoding="utf-8")
    return directory


def test_default_skill_is_personal_and_restart_preserves_customization(tmp_path):
    service = Employees(tmp_path / "employees")
    first = service.create(EmployeeInput(name="待配置"))
    second = service.create(EmployeeInput(name="已配置", harness="codex"))
    first_scan = service.skills(first["employee_id"])
    second_scan = service.skills(second["employee_id"])
    assert len(first_scan["skills"]) == len(second_scan["skills"]) == 1
    one, two = first_scan["skills"][0], second_scan["skills"][0]
    assert one["name"] == two["name"] == "agentsteam-skill-stewardship"
    assert not one["error"] and not two["error"]
    assert one["path"] != two["path"]
    original = service.read_skill(first["employee_id"], one["id"])["content"]
    Path(one["path"]).write_bytes((original + "\n个人补充\n").encode("utf-8"))
    restarted = Employees(service.root)
    assert restarted.read_skill(first["employee_id"], one["id"])["content"] == original + "\n个人补充\n"
    assert restarted.read_skill(second["employee_id"], two["id"])["content"] == original
    assert restarted.get(first["employee_id"]) == first
    assert not list(service.root.rglob(".seed-*"))


def test_startup_seeds_legacy_employee_without_changing_session_binding(tmp_path):
    root = tmp_path / "employees"
    employee_id, session_id = str(uuid4()), str(uuid4())
    profile = {**EmployeeInput(name="旧员工", harness="codex").model_dump(),
               "employee_id": employee_id, "version": 1}
    atomic_json(root / employee_id / "profile.json", profile)
    binding = {"session_id": session_id, "employee_id": employee_id, "digest": "legacy-frozen"}
    atomic_json(root / employee_id / "sessions" / session_id / "binding.json", binding)
    service = Employees(root)
    assert service.get(employee_id) == profile
    assert service.binding(session_id) == binding
    assert service.skills(employee_id)["skills"][0]["name"] == "agentsteam-skill-stewardship"
    assert len(Employees(root).skills(employee_id)["skills"]) == 1


@pytest.mark.parametrize("harness", ["codex", "opencode", "codebuddy", "deepseek"])
def test_default_skill_bundle_and_learned_source_flow_into_new_snapshots(tmp_path, harness):
    service = Employees(tmp_path / "employees")
    employee = service.create(EmployeeInput(name="甲", harness=harness))
    settings = {"revision": 0, "config_values": {}}
    first = service.prepare(employee["employee_id"], str(uuid4()), settings, tmp_path)
    source = service.directory(employee["employee_id"]) / "skills"
    frozen = filesystem_path(Path(first["skills_path"]))
    assert not first["warnings"]
    identity = filesystem_path(Path(first["identity_file"])).read_text(encoding="utf-8")
    assert source.as_posix() in identity
    assert "agentsteam-skill-stewardship" in identity
    for path in source.rglob("*"):
        if path.is_file():
            assert (frozen / path.relative_to(source)).read_bytes() == path.read_bytes()
    reference = source / "agentsteam-skill-stewardship/references/authoring.md"
    original = reference.read_bytes()
    reference.write_bytes(original + "\n补充规则\n".encode("utf-8"))
    skill(service, employee, "workflow-guidance", "verified workflow")
    second = service.prepare(employee["employee_id"], str(uuid4()), settings, tmp_path)
    updated = filesystem_path(Path(second["skills_path"]))
    assert second["digest"] != first["digest"]
    assert (frozen / reference.relative_to(source)).read_bytes() == original
    assert (updated / reference.relative_to(source)).read_bytes() == reference.read_bytes()
    assert not (frozen / "workflow-guidance").exists()
    assert (updated / "workflow-guidance/references/data.txt").read_text() == "verified workflow"
    assert service.binding(first["session_id"])["digest"] == first["digest"]


def test_versions_restart_snapshots_and_boundaries(tmp_path):
    service = Employees(tmp_path / "employees")
    employee = service.create(EmployeeInput(name="甲", harness="codex"))
    directory = skill(service, employee)
    settings = {"revision": 1, "config_values": {"model": "original"}}
    first = service.prepare(employee["employee_id"], str(uuid4()), settings, tmp_path)
    again = service.prepare(employee["employee_id"], str(uuid4()), settings, tmp_path)
    assert first["runtime"] == again["runtime"]
    assert (directory / "references/data.txt").read_text() == "first"
    assert filesystem_path(tmp_path / "employees" / employee["employee_id"] / "runtime" / first["digest"] / "skills/personal-unique/references/data.txt").read_text() == "first"
    (directory / "references/data.txt").write_text("second")
    changed = service.prepare(employee["employee_id"], str(uuid4()), settings, tmp_path)
    assert changed["digest"] != first["digest"]
    update = EmployeeUpdate(**{key: value for key, value in employee.items() if key != "employee_id"})
    update.name = "改名"
    assert service.update(employee["employee_id"], update)["version"] == 2
    with pytest.raises(RequestConflictError):
        service.update(employee["employee_id"], update)
    restarted = Employees(service.root)
    assert restarted.get(employee["employee_id"])["name"] == "改名"
    assert restarted.binding(first["session_id"])["profile"]["name"] == "甲"
    with pytest.raises(ValueError):
        service.get("../../escape")
    with pytest.raises(KeyError):
        service.read_skill(employee["employee_id"], "../../escape")
    assert not list(service.root.rglob(".write-*"))


def test_work_instructions_preserve_legacy_and_freeze_new_sessions(tmp_path):
    service = Employees(tmp_path / "employees")
    employee = service.create(EmployeeInput(name="研发", harness="codex", responsibilities="开发", principles="先验证", growth="总结方法"))
    employee.pop("instructions")
    atomic_json(service.directory(employee["employee_id"]) / "profile.json", employee)
    settings = {"revision": 0, "config_values": {}}
    old = service.prepare(employee["employee_id"], str(uuid4()), settings, tmp_path)
    old_identity = filesystem_path(Path(old["identity_file"])).read_text(encoding="utf-8")
    assert "专业职责：开发" in old_identity and "工作准则：先验证" in old_identity
    updated = service.update(employee["employee_id"], EmployeeUpdate(
        name="研发", harness="codex", version=1, instructions="负责研发；先验证，再沉淀经验。",
    ))
    restarted = Employees(service.root)
    assert restarted.get(employee["employee_id"]) == updated
    new = restarted.prepare(employee["employee_id"], str(uuid4()), settings, tmp_path)
    identity = filesystem_path(Path(new["identity_file"])).read_text(encoding="utf-8")
    assert identity.count("工作指引：负责研发；先验证，再沉淀经验。") == 1
    assert "专业职责：" not in identity and "工作准则：" not in identity and "成长课题：" not in identity
    assert filesystem_path(Path(old["identity_file"])).read_text(encoding="utf-8") == old_identity
    assert new["digest"] != old["digest"]


def test_atomic_update_failure_keeps_previous_profile(tmp_path, monkeypatch):
    service = Employees(tmp_path / "employees")
    employee = service.create(EmployeeInput(name="原始姓名"))
    from app.services import employees
    def fail_replace(*args):
        raise OSError("受控写入失败")
    monkeypatch.setattr(employees.os, "replace", fail_replace)
    with pytest.raises(OSError):
        service.update(employee["employee_id"], EmployeeUpdate(name="新姓名", version=1))
    assert service.get(employee["employee_id"])["name"] == "原始姓名"
    assert not list(service.root.rglob(".write-*"))


def test_invalid_skills_and_global_conflicts_are_excluded(tmp_path, monkeypatch):
    service = Employees(tmp_path / "employees")
    employee = service.create(EmployeeInput(name="甲", harness="codex"))
    skill(service, employee, "same-name")
    invalid = skill(service, employee, "invalid-name")
    (invalid / "SKILL.md").write_text("No frontmatter", encoding="utf-8")
    global_root = tmp_path / "global-skills"
    (global_root / "same-name").mkdir(parents=True)
    (global_root / "same-name/SKILL.md").write_text("---\nname: same-name\ndescription: Existing global skill\n---\nOriginal", encoding="utf-8")
    monkeypatch.setattr("app.services.employees.skill_roots", lambda *_: [global_root])
    scan = service.skills(employee["employee_id"])
    assert next(item for item in scan["skills"] if item["name"] == "invalid-name")["error"]
    binding = service.prepare(employee["employee_id"], str(uuid4()), {"revision": 0, "config_values": {}}, tmp_path)
    assert len(binding["warnings"]) == 2
    assert [path.name for path in filesystem_path(Path(binding["skills_path"])).iterdir()] == ["agentsteam-skill-stewardship"]


class EmployeeClient(ConfigClient):
    async def configure_employee(self, binding):
        self.employee_binding = binding


class EmployeeFactory(ConfigFactory):
    def __call__(self, *args):
        client = EmployeeClient(self, *args)
        self.clients.append(client)
        return client


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex", "opencode", "codebuddy", "deepseek"])
async def test_http_frozen_identity_configuration_concurrency_and_idempotence(tmp_path, repository, harness):
    factory = EmployeeFactory()
    app = create_app(tmp_path / "employees.db", factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as api:
            project = (await api.post("/api/projects", json={"path": str(repository)})).json()
            first = (await api.post("/api/employees", json={"name": "甲", "harness": harness, "config_values": {"model": "fast"}})).json()
            second = (await api.post("/api/employees", json={"name": "乙", "harness": harness})).json()
            empty = (await api.post("/api/employees", json={"name": "待配置"})).json()
            assert (await api.post("/api/sessions", json={"project_id": project["id"], "employee_id": empty["employee_id"]})).status_code == 409
            directory = skill(app.state.employees, first)
            sessions = []
            for employee in (first, second):
                response = await api.post("/api/sessions", json={"project_id": project["id"], "employee_id": employee["employee_id"]})
                assert response.status_code == 201, response.text
                sessions.append(response.json())
            update = {key: value for key, value in first.items() if key != "employee_id"}
            update.update(name="甲新版", config_values={"model": "m1"})
            assert (await api.put(f"/api/employees/{first['employee_id']}", json=update)).status_code == 200
            assert (await api.put(f"/api/employees/{first['employee_id']}", json=update)).status_code == 409
            (directory / "references/data.txt").write_text("new source")
            await app.state.storage.save_harness_settings(harness, {"model": "m1"})
            body = {"message": "原始任务，不注入身份", "request_id": str(uuid4())}
            one = (await api.post(f"/api/sessions/{sessions[0]['id']}/messages", json=body)).json()
            duplicate = (await api.post(f"/api/sessions/{sessions[0]['id']}/messages", json=body)).json()
            assert one["run_id"] == duplicate["run_id"]
            await api.post(f"/api/sessions/{sessions[1]['id']}/messages", json={**body, "request_id": str(uuid4())})
            running = [await factory.started(), await factory.started()]
            assert {client.employee_binding["name"] for client, _ in running} == {"甲", "乙"}
            assert all(text == body["message"] for _, text in running)
            assert len({client.employee_binding["skills_path"] for client, _ in running}) == 2
            for client, _ in running:
                if client.employee_binding["name"] == "甲":
                    assert client.employee_binding["settings"]["config_values"]["model"] == "fast"
                    assert filesystem_path(Path(client.employee_binding["skills_path"]) / "personal-unique/references/data.txt").read_text() == "first"
                client.finish()
            await terminal(api, one["run_id"])
            detail = (await api.get(f"/api/sessions/{sessions[0]['id']}?summary=true")).json()
            assert detail["session"]["employee_name"] == "甲"
            for _ in range(2):
                run = (await api.post(f"/api/sessions/{sessions[0]['id']}/messages", json={"message": "续聊", "request_id": str(uuid4())})).json()
                client, text = await factory.started()
                assert client.employee_binding["name"] == "甲" and text == "续聊"
                client.finish()
                await terminal(api, run["run_id"])
            assert len(factory.clients) == 2
            assert len(list(app.state.employees.root.glob(f"*/sessions/{sessions[0]['id']}/runs/*.json"))) == 3
            newer = (await api.post("/api/sessions", json={"project_id": project["id"], "employee_id": first["employee_id"]})).json()
            assert newer["employee_name"] == "甲新版"
            assert app.state.employees.binding(newer["id"])["digest"] != app.state.employees.binding(sessions[0]["id"])["digest"]


@pytest.mark.asyncio
async def test_failed_session_creation_removes_binding(tmp_path, repository, monkeypatch):
    app = create_app(tmp_path / "employees.db", EmployeeFactory())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as api:
            project = (await api.post("/api/projects", json={"path": str(repository)})).json()
            employee = (await api.post("/api/employees", json={"name": "甲", "harness": "codex"})).json()
            async def fail(*args):
                raise ValueError("数据库创建失败")
            monkeypatch.setattr(app.state.storage, "create_session", fail)
            response = await api.post("/api/sessions", json={"project_id": project["id"], "employee_id": employee["employee_id"]})
            assert response.status_code == 409
            assert not list(app.state.employees.root.glob("*/sessions/*/binding.json"))
