"""只映射当前四种 Harness 的设置词汇，候选项仍以 ACP 返回为准。"""

import json
from copy import deepcopy

from acp.exceptions import RequestError


HARNESS_LABELS = {"codex": "Codex", "opencode": "OpenCode", "codebuddy": "CodeBuddy", "deepseek": "DeepSeek"}
FIELD_SPECS = {
    "codex": [("model", "模型"), ("reasoning_effort", "思考强度"), ("mode", "权限"), ("collaboration_mode", "工作模式")],
    "opencode": [("model", "模型"), ("effort", "思考强度"), ("permission", "权限"), ("mode", "会话模式")],
    "codebuddy": [("model", "模型"), ("thought_level", "思考强度"), ("mode", "权限模式"), ("sandbox", "沙箱")],
    "deepseek": [("model", "模型"), ("reasoning", "思考强度"), ("sandbox", "文件权限"), ("session_mode", "会话模式")],
}
PERMISSION_KEYS = {"codex": {"mode"}, "opencode": {"permission"},
                   "codebuddy": {"mode", "sandbox"}, "deepseek": {"sandbox"}}


def select_options(entries: list[dict]) -> list[dict]:
    result = []
    for entry in entries:
        if "options" in entry:
            result.extend({**item, "group": entry.get("name", entry.get("group", ""))}
                          for item in entry["options"])
        else:
            result.append(deepcopy(entry))
    return result


def launch_settings(client, values: dict[str, str]) -> None:
    if client.harness == "opencode":
        permission = json.loads(client.env.get("OPENCODE_PERMISSION", "{}")).get("*")
        client.launch_defaults["permission"] = permission or "native"
        client.launch_values["permission"] = values.get("permission", permission or "native")
        if "permission" in values:
            client.env["OPENCODE_PERMISSION"] = json.dumps({"*": values["permission"]})
    if client.harness == "codebuddy":
        if client.env.get("CODEBUDDY_IS_SANDBOX") == "1":
            client.launch_defaults["mode"] = "fullAccess"
        mode = values.get("mode")
        if mode is not None:
            if "--permission-mode" in client.argv:
                index = client.argv.index("--permission-mode")
                del client.argv[index:index + 2]
            client.argv.extend(["--permission-mode", "bypassPermissions" if mode == "fullAccess" else "default"])
            if mode == "fullAccess":
                client.env["CODEBUDDY_IS_SANDBOX"] = "1"
            else:
                client.env.pop("CODEBUDDY_IS_SANDBOX", None)
        client.launch_values["full_access_process"] = str(client.env.get("CODEBUDDY_IS_SANDBOX") == "1").lower()


def configuration_fields(client) -> list[dict]:
    native = {option["id"]: option for option in client.config_options}
    if client.harness == "opencode":
        native["permission"] = {
            "id": "permission", "type": "select", "currentValue": client.launch_values.get("permission", "native"),
            "description": "原生 permission 规则；修改此项需要新建会话，已有会话保留旧权限。",
            "options": [{"value": value, "name": label} for value, label in
                        [("ask", "询问确认"), ("allow", "允许所有操作"), ("deny", "拒绝所有操作")]],
        }
    if client.harness == "deepseek" and client.modes:
        native["session_mode"] = {
            "id": "session_mode", "type": "select", "currentValue": client.modes["currentModeId"],
            "description": "会话模式控制工作方式，文件访问边界由文件权限决定。",
            "options": [{"value": item["id"], "name": item["name"], "description": item.get("description")}
                        for item in client.modes["availableModes"]],
        }
    fields = []
    for config_id, name in FIELD_SPECS[client.harness]:
        option = native.get(config_id)
        supported = option is not None and option.get("type") == "select"
        fields.append({"id": config_id, "name": name, "supported": supported,
                       "description": option.get("description") if supported else "此 Harness 未提供此配置项。",
                       "current_value": option.get("currentValue") if supported else None,
                       "options": select_options(option["options"]) if supported else [],
                       "hot_change": config_id != "permission",
                       "limitation": "从全访问切换为受限权限可能需要新建会话。"
                       if client.harness == "codebuddy" and config_id == "mode" else None})
    return fields


async def apply_value(client, config_id: str, value: str) -> None:
    field = next((field for field in configuration_fields(client) if field["id"] == config_id), None)
    if field is None or not field["supported"]:
        raise ValueError(f"不支持配置项：{config_id}")
    if not any(item["value"] == value for item in field["options"]):
        raise ValueError(f"{field['name']} 不支持取值：{value}")
    if field["current_value"] == value:
        return
    if config_id == "permission":
        raise ValueError("权限规则需要新建会话，当前会话继续使用原权限")
    if (client.harness == "codebuddy" and config_id == "mode" and value != "fullAccess"
            and client.launch_values.get("full_access_process") == "true"):
        raise ValueError("当前 CodeBuddy 以全访问进程启动，收紧权限需要新建会话")
    if config_id == "session_mode":
        await client.configure_mode(value)
    else:
        await client.configure(config_id, value)
    if config_id == "model":
        for option in client.config_options:
            if option["id"] in {"reasoning_effort", "effort", "thought_level", "reasoning"}:
                client.config_defaults[option["id"]] = option["currentValue"]
    actual = next(field["current_value"] for field in configuration_fields(client) if field["id"] == config_id)
    if actual != value:
        raise ValueError(f"Harness 未确认配置生效：{config_id}={value}，实际为 {actual}")


async def apply_values(client, values: dict[str, str], strict: bool) -> list[dict]:
    warnings = []
    for config_id in sorted(values, key=lambda key: (key != "model", key)):
        try:
            await apply_value(client, config_id, values[config_id])
        except (ValueError, RequestError, TimeoutError) as error:
            if strict or not client.reusable:
                raise
            warnings.append({"id": config_id, "requested": values[config_id], "reason": str(error)})
    return warnings


def actual_values(client) -> dict[str, str]:
    return {field["id"]: field["current_value"] for field in configuration_fields(client)
            if field["supported"]}


def automatic_permission(client, legacy: bool) -> bool:
    if legacy:
        return True
    values = actual_values(client)
    return {"codex": values.get("mode") == "agent-full-access",
            "opencode": values.get("permission") == "allow",
            "codebuddy": values.get("mode") == "fullAccess" or (
                values.get("mode") == "bypassPermissions" and
                client.launch_values.get("full_access_process") == "true"),
            "deepseek": values.get("sandbox") == "danger-full-access"}[client.harness]
