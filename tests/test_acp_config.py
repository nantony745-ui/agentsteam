import asyncio
import json
import os
import sys
import tomllib
from types import SimpleNamespace

import pytest

from app.agents import acp_client


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "agents.toml"
    monkeypatch.setattr(acp_client, "CONFIG_PATH", path)
    return path


def test_bom_and_argv_order(config_path):
    config_path.write_text(
        "[agents.codex]\nexecutable = 'C:\\Program Files\\nodejs\\node.exe'\n"
        "args = ['C:\\agent bridge\\index.js', '--flag', 'a b']\nenabled = true\n",
        encoding="utf-8-sig",
    )
    assert acp_client.load_argv("codex") == [
        r"C:\Program Files\nodejs\node.exe", r"C:\agent bridge\index.js", "--flag", "a b",
    ]


def test_disabled_agent(config_path):
    config_path.write_text("[agents.codebuddy]\nenabled = false\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Harness 未启用: codebuddy"):
        acp_client.load_argv("codebuddy")


def test_unknown_agent(config_path):
    config_path.write_text("[agents]\n", encoding="utf-8")
    with pytest.raises(KeyError, match="missing"):
        acp_client.load_argv("missing")


def test_missing_config(config_path):
    with pytest.raises(FileNotFoundError):
        acp_client.load_argv("codex")


def test_invalid_toml(config_path):
    config_path.write_text("[agents", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        acp_client.load_argv("codex")


@pytest.mark.parametrize("name,key,value", [
    ("codex", "INITIAL_AGENT_MODE", "agent-full-access"),
    ("opencode", "OPENCODE_PERMISSION", '{"*":"allow"}'),
    ("codebuddy", "CODEBUDDY_IS_SANDBOX", "1"),
])
def test_native_full_access_defaults(name, key, value):
    spec = acp_client.load_agents()[name]
    assert spec["enabled"] and spec["env"][key] == value
    if name == "codebuddy":
        assert spec["args"][-3:] == ["--acp", "--permission-mode", "bypassPermissions"]


def test_environment_copy_overrides_only_selected_agent(config_path, monkeypatch):
    config_path.write_text(
        "[agents.codex]\nenv = { AGENTSTEAM_TEST_POLICY = 'full' }\n"
        "[agents.opencode]\nenv = { AGENTSTEAM_TEST_POLICY = 'other' }\n"
        "[agents.codebuddy]\n", encoding="utf-8",
    )
    monkeypatch.setenv("AGENTSTEAM_TEST_POLICY", "host")
    monkeypatch.setenv("AGENTSTEAM_TEST_INHERITED", "inherited")
    codex = acp_client.load_env("codex")
    assert codex["AGENTSTEAM_TEST_POLICY"] == "full"
    assert codex["AGENTSTEAM_TEST_INHERITED"] == "inherited"
    assert acp_client.load_env("opencode")["AGENTSTEAM_TEST_POLICY"] == "other"
    assert acp_client.load_env("codebuddy")["AGENTSTEAM_TEST_POLICY"] == "host"
    codex["AGENTSTEAM_TEST_POLICY"] = "modified"
    assert os.environ["AGENTSTEAM_TEST_POLICY"] == "host"
    assert acp_client.load_env("codex")["AGENTSTEAM_TEST_POLICY"] == "full"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["codex", "opencode", "codebuddy"])
@pytest.mark.parametrize("launcher", ["running_process", "acp_client"])
async def test_real_subprocess_receives_scoped_environment(tmp_path, monkeypatch, name, launcher):
    spec = acp_client.load_agents()[name]
    key, value = next(iter(spec["env"].items()))
    monkeypatch.setenv(key, "host-value")
    monkeypatch.setenv("AGENTSTEAM_TEST_INHERITED", "inherited")
    script = (
        "import json,os,sys; "
        f"print(json.dumps([os.environ[{key!r}],os.environ['AGENTSTEAM_TEST_INHERITED']]),flush=True); "
        "sys.stdin.read()"
    )
    argv = [sys.executable, "-u", "-c", script]
    monkeypatch.setattr(acp_client, "load_argv", lambda name: argv)
    rows = []

    def record(kind, data):
        rows.append((kind, data))

    async def check(proc):
        line = await asyncio.wait_for(proc.stdout.readline(), 3)
        assert json.loads(line) == [value, "inherited"]
        assert os.environ[key] == "host-value"

    if launcher == "running_process":
        async with acp_client.running_process(name, tmp_path, record) as proc:
            await check(proc)
    else:
        async def initialize(**kwargs):
            return SimpleNamespace(model_dump=lambda **kwargs: {})

        async def new_session(**kwargs):
            return SimpleNamespace(session_id="native-probe", model_dump=lambda **kwargs: {})

        async def close():
            return None

        connection = SimpleNamespace(initialize=initialize, new_session=new_session, close=close)
        monkeypatch.setattr(acp_client, "connect_to_agent", lambda *args, **kwargs: connection)
        instance = acp_client.AcpClient(name, SimpleNamespace(), record, lambda event: None, lambda reason: None)
        try:
            await instance.connect(tmp_path, [])
            proc = instance.proc
            await check(instance.proc)
        finally:
            await instance.close()
    assert proc.returncode is not None
    assert all("env" not in data for _, data in rows)
