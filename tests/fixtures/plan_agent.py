"""本地 ACP wire 替身，不调用模型；校验能力协商和表单回传。"""
import json
import sys


def send(message):
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)


def fields(mode):
    return [{"id": "collaboration_mode", "name": "Mode", "type": "select", "currentValue": mode,
             "options": [{"value": value, "name": value} for value in ["default", "plan"]]}]


prompt_id = None
mode = "default"
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    params = request.get("params", {})
    result = {}
    if method == "initialize":
        assert params["clientCapabilities"]["elicitation"]["form"] == {}
        assert params["clientCapabilities"]["plan"] == {}
        result = {"protocolVersion": 1, "agentCapabilities": {}}
    elif method == "session/new":
        result = {"sessionId": "wire-plan", "configOptions": fields(mode)}
    elif method == "session/set_config_option":
        mode = params["value"]
        result = {"configOptions": fields(mode)}
    elif method == "session/prompt":
        prompt_id = request["id"]
        if mode != "plan":
            send({"method": "session/update", "params": {"sessionId": "wire-plan", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "执行模式"}}}})
            send({"id": prompt_id, "result": {"stopReason": "end_turn"}})
            continue
        send({"id": "question-1", "method": "elicitation/create", "params": {
            "sessionId": "wire-plan", "mode": "form", "message": "Codex needs your input to continue.",
            "requestedSchema": {"type": "object", "properties": {
                "choice": {"type": "string", "title": "选择方案", "oneOf": [{"const": "A", "title": "方案 A (Recommended)", "description": "沿用现有结构，先完成核心流程，再验证关键路径。"}, {"const": "B", "title": "方案 B", "description": "先拆分模块，再逐步实现与验证。"}]},
                "note": {"type": "string", "title": "补充说明"}}, "required": ["choice"]}}})
        continue
    elif method == "session/cancel":
        send({"id": prompt_id, "result": {"stopReason": "cancelled"}})
        continue
    elif request.get("id") == "question-1":
        answer = request.get("result", {})
        if answer.get("action") == "cancel":
            continue
        if answer.get("action") == "accept":
            assert answer["content"]["choice"] == "A"
            send({"method": "session/update", "params": {"sessionId": "wire-plan", "update": {
                "sessionUpdate": "plan_update", "plan": {"type": "markdown", "planId": "p1", "content": "# 方案 A\n\n1. 实现\n2. 验证"}}}})
        send({"id": prompt_id, "result": {"stopReason": "end_turn"}})
        continue
    if "id" in request:
        send({"id": request["id"], "result": result})
