"""全局 MCP 预览仅使用临时文件，不启动 Harness 或 MCP。"""

import json
from pathlib import Path

import pytest

from app.services.harness_mcp import MAX_CONFIG_BYTES, config_paths, parse_jsonc
from test_harness_settings import settings_api


@pytest.fixture
def mcp_home(tmp_path, monkeypatch):
    for name in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(name, str(tmp_path))
    for name in ("CODEX_HOME", "XDG_CONFIG_HOME", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8-sig")
    return path


@pytest.mark.asyncio
async def test_codex_native_and_legacy_mcp_only_readonly(settings_api, mcp_home, monkeypatch):
    client, app, factory = settings_api
    monkeypatch.setenv("CODEX_HOME", str(mcp_home / "custom"))
    path = write(mcp_home / "custom/config.toml", '''api_key = "unrelated-secret"
[mcp_servers.files]
command = "node"
args = ["files.js"]
enabled = false
[mcp_servers.files.env]
TOKEN = "private-token"
[mcp.servers.files]
command = "legacy-ignored"
[mcp.servers.remote]
url = "https://example.invalid/mcp"
[mcp.servers.remote.http_headers]
Authorization = "private-header"
''')
    before = path.read_bytes(), path.stat().st_mtime_ns
    url = "/api/settings/harnesses/codex/mcp"
    response = await client.get(url)
    assert response.status_code == 200
    files, remote = response.json()["servers"]
    assert files["name"] == "files" and files["transport"] == "stdio" and files["enabled"] is False
    assert files["config"]["command"] == "node" and remote["transport"] == "http"
    assert files["config"]["env"] == {"TOKEN": "••••"}
    assert not any(value in response.text for value in ("unrelated-secret", "private-token", "private-header", "legacy-ignored"))
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not factory.clients and not await app.state.storage.list_sessions(None)
    for method in ("POST", "PUT", "DELETE"):
        assert (await client.request(method, url, json={})).status_code == 405
    assert (await client.get("/api/settings/harnesses/unknown/mcp")).status_code == 422


@pytest.mark.asyncio
async def test_opencode_sources_jsonc_and_distinct_source_ids(settings_api, mcp_home, monkeypatch):
    client, _, _ = settings_api
    monkeypatch.setenv("XDG_CONFIG_HOME", str(mcp_home / "xdg"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(mcp_home / "extra.json"))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(mcp_home / "env-dir"))
    write(mcp_home / "xdg/opencode/opencode.jsonc", '''{
      // native format
      "mcp": {"files": {"type":"local", "command":["node","files.js"], "enabled":true,},
              "remote": {"type":"remote", "url":"https://example.invalid/mcp",},},
    }''')
    write(mcp_home / "extra.json", '{/* JSONC in .json */"mcpServers":{"files":{"command":"python",},},}')
    write(mcp_home / "env-dir/opencode.json", '{"mcp":{"sse":{"type":"remote","transport":"sse","url":"http://localhost/sse"}}}')
    data = (await client.get("/api/settings/harnesses/opencode/mcp")).json()
    assert len(data["servers"]) == 4
    files = [item for item in data["servers"] if item["name"] == "files"]
    assert len({item["id"] for item in files}) == 2
    assert files[0]["transport"] == "stdio" and files[0]["enabled"] is True
    assert next(item for item in data["servers"] if item["name"] == "sse")["transport"] == "sse"
    assert "不代表当前会话已连接" in data["note"]


@pytest.mark.asyncio
async def test_codebuddy_activation_flags_and_deepseek_unsupported(settings_api, mcp_home):
    client, _, factory = settings_api
    write(mcp_home / ".codebuddy.json", json.dumps({"mcpServers": {
        "yes": {"command": "node"}, "no": {"url": "https://example.invalid", "enabled": True},
        "default": {"command": "python"}}}))
    write(mcp_home / ".codebuddy/settings.json", '{"enabledPlugins":{"yes@local":true,"no@local":false}}')
    items = (await client.get("/api/settings/harnesses/codebuddy/mcp")).json()["servers"]
    assert {item["name"]: item["enabled"] for item in items} == {"yes": True, "no": False, "default": None}
    # Codeg 自有记录不应被当作 DeepSeek 原生配置。
    write(mcp_home / ".dsh/mcp.json", '{"mcpServers":{"not-native":{"command":"node"}}}')
    data = (await client.get("/api/settings/harnesses/deepseek/mcp")).json()
    assert not data["servers"] and not data["locations"] and "ACP 会话参数" in data["note"]
    assert not factory.clients


@pytest.mark.asyncio
async def test_missing_invalid_and_oversize_sources_are_visible(settings_api, mcp_home):
    client, _, _ = settings_api
    url = "/api/settings/harnesses/codex/mcp"
    data = (await client.get(url)).json()
    assert not data["servers"] and data["locations"][0]["exists"] is False
    path = mcp_home / ".codex/config.toml"
    for content in ('[broken', 'mcp_servers = []', 'a="' + 'x' * MAX_CONFIG_BYTES):
        write(path, content)
        data = (await client.get(url)).json()
        assert data["locations"][0]["error"] and not data["servers"]
    path.write_bytes(b"\xff\xfe")
    assert (await client.get(url)).json()["locations"][0]["error"]
    write(path, '[mcp_servers]\ninvalid = 5\n[mcp_servers.transport]\ntype = []')
    data = (await client.get(url)).json()
    assert len(data["servers"]) == 2 and all(item["error"] for item in data["servers"])
    assert (await client.get(url)).headers["content-type"].startswith("application/json")


def test_jsonc_comments_trailing_commas_and_strings():
    data = parse_jsonc('''{/*comment*/ "url":"https://host/path//literal", "text":"escaped \\\" /*literal*/ ,}",
      "items":[1,2,/*two*/], "nested":{"a":true,}, // end
    }''')
    assert data == {"url": "https://host/path//literal", "text": 'escaped " /*literal*/ ,}',
                    "items": [1, 2], "nested": {"a": True}}
    with pytest.raises(json.JSONDecodeError):
        parse_jsonc('{/*unclosed')


def test_global_paths_do_not_depend_on_project(mcp_home):
    env = {"HOME": str(mcp_home), "USERPROFILE": str(mcp_home), "CODEX_HOME": "~/custom"}
    assert config_paths("codex", env) == [mcp_home / "custom/config.toml"]
    assert all(mcp_home in path.parents for path in config_paths("opencode", env))
    assert config_paths("deepseek", env) == []
