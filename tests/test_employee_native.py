import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agents.employee_native import configure_employee, package_root
from app.domain.employees import EmployeeInput
from app.services.employees import Employees


def package(tmp_path, name, version, entry):
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")
    target = root / entry
    target.parent.mkdir(exist_ok=True)
    target.write_text("fixture", encoding="utf-8")
    return target


@pytest.mark.asyncio
async def test_codebuddy_keeps_original_append_and_all_plugin_directories(tmp_path):
    entry = package(tmp_path, "@tencent-ai/codebuddy-code", "2.167.0", "bin/codebuddy")
    employees = Employees(tmp_path / "employees")
    employee = employees.create(EmployeeInput(name="甲", harness="codebuddy"))
    binding = employees.prepare(employee["employee_id"], str(uuid4()), {"revision": 0, "config_values": {}}, tmp_path)
    client = SimpleNamespace(harness="codebuddy", argv=["node", str(entry), "--acp", "--append-system-prompt", "original",
        "--plugin-dir", "first", "second", "--permission-mode", "default"], env={"CODEBUDDY_PLUGIN_DIRS": "environment"})
    await configure_employee(client, binding, tmp_path)
    assert client.argv[client.argv.index("--append-system-prompt") + 1].startswith("original\n\n<agentsteam_employee>")
    assert client.argv[-5:] == ["--plugin-dir", "environment", "first", "second", binding["runtime"]]
    assert client.argv.count("--plugin-dir") == 1
    assert client.argv[client.argv.index("--permission-mode") + 1] == "default"


def test_version_and_entry_are_checked_before_native_launch(tmp_path):
    entry = package(tmp_path, "@agentclientprotocol/codex-acp", "0.0.0", "dist/index.js")
    with pytest.raises(ValueError, match="2.0.1"):
        package_root(["node", str(entry)], "codex")
    (entry.parents[1] / "package.json").write_text(json.dumps({"name": "@agentclientprotocol/codex-acp", "version": "2.0.1"}))
    wrong = entry.with_name("other.js")
    wrong.write_text("fixture")
    with pytest.raises(ValueError, match="指定入口"):
        package_root(["node", str(wrong)], "codex")
