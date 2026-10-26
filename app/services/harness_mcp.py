"""只读预览 Harness 的全局 MCP 配置文件，不连接服务。"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib

from app.agents.acp_client import load_env
from app.services.harness_skills import skill_roots


MAX_CONFIG_BYTES = 2 * 1024 * 1024
JSONC_TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*.*?\*/', re.DOTALL)
TRAILING_COMMAS = re.compile(r'("(?:\\.|[^"\\])*")|,(\s*[}\]])')


def parse_jsonc(content: str) -> dict:
    content = JSONC_TOKENS.sub(lambda match: match[0] if match[0].startswith('"') else
                               re.sub(r"[^\r\n]", " ", match[0]), content)
    content = TRAILING_COMMAS.sub(lambda match: match[1] or match[2], content)
    return json.loads(content)


def config_paths(harness: str, env: dict[str, str]) -> list[Path]:
    home = Path(env.get("USERPROFILE" if os.name == "nt" else "HOME") or Path.home())
    if harness == "codex":
        return [skill_roots(harness, env)[0].parent / "config.toml"]
    if harness == "codebuddy":
        return [home / ".codebuddy.json", home / ".codebuddy/settings.json"]
    if harness == "opencode":
        directory = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"
        paths = [directory / name for name in ("config.json", "opencode.json", "opencode.jsonc")]
        if env.get("OPENCODE_CONFIG"):
            paths.append(Path(env["OPENCODE_CONFIG"]))
        for directory in [home / ".opencode", *([Path(env["OPENCODE_CONFIG_DIR"])] if env.get("OPENCODE_CONFIG_DIR") else [])]:
            paths.extend(directory / name for name in ("opencode.json", "opencode.jsonc"))
        return list(dict.fromkeys(paths))
    return []


def read_config(path: Path, harness: str) -> dict:
    with path.open("rb") as stream:
        data = stream.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("配置文件超过 2 MiB，无法预览")
    content = data.decode("utf-8-sig")
    try:
        if harness == "codex":
            value = tomllib.loads(content)
        elif harness == "opencode":
            value = parse_jsonc(content)
        else:
            value = json.loads(content)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("配置格式错误，请检查源文件") from error
    if not isinstance(value, dict):
        raise ValueError("配置根节点必须是对象")
    return value


def preview_config(value):
    if isinstance(value, dict):
        return {key: ({name: "••••" for name in item} if isinstance(item, dict) and key.lower() in
                      {"env", "environment", "headers", "http_headers"} else
                      "••••" if any(word in key.lower() for word in ("token", "secret", "password", "api_key", "apikey"))
                      else preview_config(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [preview_config(item) for item in value]
    return value


def server_item(name: str, spec, path: Path, plugins: dict) -> dict:
    item = {"id": hashlib.sha256(f"{path.resolve()}\0{name}".encode()).hexdigest(), "name": name,
            "path": str(path), "transport": "未声明", "enabled": None, "error": None,
            "config": preview_config(spec)}
    if not isinstance(spec, dict):
        return {**item, "error": "MCP 配置项必须是对象"}
    transport = spec.get("type")
    if transport is not None and not isinstance(transport, str):
        return {**item, "error": "MCP 传输类型必须是字符串"}
    item["transport"] = {"local": "stdio", "remote": spec.get("transport", "http")}.get(transport, transport) or (
        "stdio" if "command" in spec else "http" if "url" in spec else "未声明")
    if not isinstance(item["transport"], str):
        item["transport"] = "未声明"
        item["error"] = "MCP 传输类型必须是字符串"
    if isinstance(spec.get("enabled"), bool):
        item["enabled"] = spec["enabled"]
    elif isinstance(spec.get("disabled"), bool):
        item["enabled"] = not spec["disabled"]
    plugin = plugins.get(f"{name}@local")
    if isinstance(plugin, bool):
        item["enabled"] = plugin
    return item


def discover_mcp(harness: str, env: dict[str, str]) -> dict:
    paths = config_paths(harness, env)
    locations, documents, servers = [], {}, []
    for path in paths:
        location = {"path": str(path), "exists": path.is_file(), "error": None}
        locations.append(location)
        if not location["exists"]:
            continue
        try:
            documents[path] = read_config(path, harness)
        except (OSError, UnicodeError, ValueError) as error:
            location["error"] = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeError) else "无法读取配置文件"
    plugins = documents.get(paths[-1], {}).get("enabledPlugins", {}) if harness == "codebuddy" else {}
    for path, root in documents.items():
        if harness == "codebuddy" and path == paths[-1]:
            continue
        groups = [root.get("mcpServers", {})]
        if harness == "opencode":
            groups.append(root.get("mcp", {}))
        if harness == "codex":
            legacy = root.get("mcp", {})
            groups = [root.get("mcp_servers", {}), legacy.get("servers", {}) if isinstance(legacy, dict) else {}]
        seen = set()
        for group in groups:
            if not isinstance(group, dict):
                next(location for location in locations if location["path"] == str(path))["error"] = "MCP 节点必须是对象"
                continue
            for name, spec in group.items():
                if name not in seen:
                    seen.add(name)
                    servers.append(server_item(name, spec, path, plugins if isinstance(plugins, dict) else {}))
    note = "DeepSeek 通过 ACP 会话参数接收 MCP，当前没有可预览的原生全局配置文件。" if harness == "deepseek" else (
        "配置文件记录不代表当前会话已连接；多个来源分别展示。环境变量和请求头的值已隐藏。")
    return {"harness": harness, "locations": locations, "servers": sorted(servers, key=lambda item: item["name"].casefold()), "note": note}


class HarnessMcp:
    async def list(self, harness: str) -> dict:
        return await asyncio.to_thread(discover_mcp, harness, load_env(harness))
