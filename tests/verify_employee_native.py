"""显式运行的真实 Harness 验收；模型服务仅使用本机 HTTP 替身。"""

import argparse
import asyncio
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from acp import Client
from acp.schema import DeniedOutcome, McpServerStdio, RequestPermissionResponse

from app.agents.acp_client import AcpClient
from app.agents.employee_native import configure_employee
from app.domain.employees import EmployeeInput
from app.services.employees import Employees, atomic_json


class Host(Client):
    async def session_update(self, session_id, update, **kwargs):
        return None

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


class FakeModel(BaseHTTPRequestHandler):
    captures = []

    def log_message(self, *_):
        return None

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"object": "list", "data": [{"id": "gpt-4.1", "object": "model"}]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "model" in body:
            self.captures.append({"path": self.path, "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        def event(value, name=None):
            wire = (f"event: {name}\n" if name else "") + "data: " + json.dumps(value) + "\n\n"
            self.wfile.write(wire.encode())
        if "responses" in self.path:
            message = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "Fixture reply", "annotations": []}]}
            response = {"id": "resp_fixture", "object": "response", "created_at": 1, "status": "in_progress",
                        "model": body.get("model"), "output": []}
            for value in [
                {"type": "response.created", "response": response},
                {"type": "response.output_item.added", "output_index": 0, "item": {**message, "status": "in_progress", "content": []}},
                {"type": "response.content_part.added", "output_index": 0, "content_index": 0, "item_id": message["id"], "part": {"type": "output_text", "text": "", "annotations": []}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": message["id"], "delta": "Fixture reply"},
                {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "item_id": message["id"], "text": "Fixture reply"},
                {"type": "response.content_part.done", "output_index": 0, "content_index": 0, "item_id": message["id"], "part": message["content"][0]},
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": {**response, "status": "completed", "output": [message],
                    "usage": {"input_tokens": 100, "output_tokens": 3, "total_tokens": 103}}},
            ]:
                event(value, value["type"])
        else:
            for delta, reason in [({"role": "assistant", "reasoning_content": "fixture"}, None),
                                  ({"content": "Fixture reply"}, None), ({}, "stop")]:
                event({"id": "chat_fixture", "object": "chat.completion.chunk", "created": 1,
                       "model": body.get("model"), "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                       "usage": {"prompt_tokens": 100, "completion_tokens": 3, "total_tokens": 103}})
            self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def configure_fixture(client, base, directory):
    if client.harness == "codex":
        client.env["CODEX_CONFIG"] = json.dumps({"model": "gpt-5.4", "model_provider": "fixture",
            "developer_instructions": "EXISTING_DEVELOPER_SENTINEL",
            "model_providers": {"fixture": {"name": "fixture", "base_url": base + "/v1", "wire_api": "responses", "requires_openai_auth": False}}})
    elif client.harness == "deepseek":
        client.env.update(DEEPSEEK_BASE_URL=base + "/v1", DEEPSEEK_API_KEY="fixture",
                          DEEPSEEK_ACP_SESSIONS_ROOT=str(directory / "native-sessions"))
    elif client.harness == "opencode":
        client.env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"model": "openai/gpt-4.1",
            "provider": {"openai": {"options": {"apiKey": "fixture", "baseURL": base + "/v1"}}}})
        client.env["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
    elif client.harness == "codebuddy":
        client.env.update(CODEBUDDY_BASE_URL=base, CODEBUDDY_API_KEY="fixture", CODEBUDDY_MODEL="gpt-4.1")


def wire_text(body):
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def main_requests(rows):
    return [item for item in rows if item["body"].get("tools")]


def instruction_text(body):
    return body.get("instructions", "") + "\n" + "\n".join(wire_text({"role": message["role"], "content": message.get("content")}) for message in
        body.get("input", body.get("messages", [])) if message.get("role") in {"system", "developer"})


async def verify(harness, directory, base):
    directory.mkdir(parents=True, exist_ok=True)
    project = directory / "project"
    project.mkdir(exist_ok=True)
    (project / "AGENTS.md").write_text("PROJECT_RULE_SENTINEL", encoding="utf-8")
    service = Employees(directory / "employees")
    profile = service.create(EmployeeInput(name="EMPLOYEE_NATIVE_SENTINEL", harness=harness))
    source = service.directory(profile["employee_id"]) / "skills" / "employee-native-fixture"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: employee-native-fixture\ndescription: EMPLOYEE_SKILL_SENTINEL\n---\nUse fixture skill.", encoding="utf-8")
    binding = service.prepare(profile["employee_id"], str(uuid4()), {"revision": 0, "config_values": {}}, project)
    records, clients = [], []
    async def start(bound):
        native = AcpClient(harness, Host(), lambda kind, data: records.append((kind, data)),
                           lambda event: records.append(("protocol", event.message)), lambda _: None)
        clients.append(native)
        configure_fixture(native, base, directory)
        if bound:
            await configure_employee(native, bound, project)
        ready = directory / f"mcp-ready-{len(clients)}"
        await native.connect(project, [McpServerStdio(name="employee_fixture", command=sys.executable,
            args=[str(Path(__file__).resolve().parent / "fixtures/employee_mcp.py"), str(ready)], env=[])])
        async with asyncio.timeout(20):
            while not ready.exists():
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.15)
        return native
    client = None
    first = len(FakeModel.captures)
    result = {"harness": harness, "binding_digest": binding["digest"]}
    try:
        async with asyncio.timeout(90):
            client = await start(binding)
            for turn in range(3):
                await client.prompt(f"fixture task {turn}")
        captures = FakeModel.captures[first:]
        primary = main_requests(captures)
        strings = [wire_text(item["body"]) for item in primary]
        systems = [instruction_text(item["body"]) for item in primary]
        result.update(requests=len(primary), auxiliary_requests=len(captures) - len(primary),
                      identity_counts=[text.count("EMPLOYEE_NATIVE_SENTINEL") for text in strings],
                      skills=["EMPLOYEE_SKILL_SENTINEL" in text for text in strings],
                      project_rules=["PROJECT_RULE_SENTINEL" in text for text in strings])
        result["instruction_stable"] = len(set(systems)) == 1
        result["tools_stable"] = len({wire_text(item["body"].get("tools")) for item in primary}) == 1
        result["mcp_preserved"] = all("fixture_sentinel" in text for text in strings)
        result["memory_tools_preserved"] = all(all(tool in text for tool in ("memory_retain", "memory_recall", "memory_reflect")) for text in strings)
        result["mcp_delivery"] = "tool-schema" if all("MCP_PRESERVATION_SENTINEL" in text for text in strings) else "native-lazy-tool-catalog"
        result["employee_section_stable"] = len({text[text.index("<agentsteam_employee>"):text.index("</agentsteam_employee>")] for text in systems}) == 1
        result["identity_locations"] = [[{"index": index, "role": message.get("role")}
            for index, message in enumerate(row["body"].get("input", row["body"].get("messages", [])))
            if "EMPLOYEE_NATIVE_SENTINEL" in wire_text(message)] for row in primary]
        prefixes = []
        for left, right in zip(strings, strings[1:]):
            prefix = next((index for index, (a, b) in enumerate(zip(left, right)) if a != b), min(len(left), len(right)))
            prefixes.append({"common_characters": prefix, "common_utf8_bytes": len(left[:prefix].encode()),
                             "left_next": left[prefix:prefix + 100], "right_next": right[prefix:prefix + 100]})
        result["prefixes"] = prefixes
        async with asyncio.timeout(150):
            baseline_start = len(FakeModel.captures)
            baseline = await start(None)
            await baseline.prompt("baseline task")
            baseline_rows = main_requests(FakeModel.captures[baseline_start:])
            result["baseline_identity_absent"] = all("agentsteam_employee" not in wire_text(row) for row in baseline_rows)
            result["baseline_tool_names_preserved"] = {tool.get("name", tool.get("function", {}).get("name")) for tool in baseline_rows[-1]["body"]["tools"]} <= {
                tool.get("name", tool.get("function", {}).get("name")) for tool in primary[0]["body"]["tools"]}
            await baseline.close()
            repeated_binding = service.prepare(profile["employee_id"], str(uuid4()), {"revision": 0, "config_values": {}}, project)
            repeat_start = len(FakeModel.captures)
            repeated = await start(repeated_binding)
            await repeated.prompt("fixture task 0")
            repeat_rows = main_requests(FakeModel.captures[repeat_start:])
            result["repeat_snapshot_path_stable"] = binding["runtime"] == repeated_binding["runtime"]
            result["repeat_instructions_stable"] = instruction_text(repeat_rows[-1]["body"]) == systems[0]
            result["repeat_employee_section_stable"] = all(binding["profile"]["name"] in instruction_text(row["body"]) and
                instruction_text(row["body"]).count("EMPLOYEE_NATIVE_SENTINEL") == 1 for row in repeat_rows)
            await repeated.close()
            other_profile = service.create(EmployeeInput(name="EMPLOYEE_OTHER_SENTINEL", harness=harness))
            other_source = service.directory(other_profile["employee_id"]) / "skills/employee-other-fixture"
            other_source.mkdir(parents=True)
            (other_source / "SKILL.md").write_text("---\nname: employee-other-fixture\ndescription: OTHER_SKILL_SENTINEL\n---\nOther employee skill.", encoding="utf-8")
            other_binding = service.prepare(other_profile["employee_id"], str(uuid4()), {"revision": 0, "config_values": {}}, project)
            other = await start(other_binding)
            concurrent_start = len(FakeModel.captures)
            await asyncio.gather(client.prompt("parallel first"), other.prompt("parallel second"))
            parallel = [wire_text(row) for row in main_requests(FakeModel.captures[concurrent_start:])]
            result["concurrent_isolation"] = len(parallel) == 2 and all(
                ("EMPLOYEE_NATIVE_SENTINEL" in text) != ("EMPLOYEE_OTHER_SENTINEL" in text) and
                ("EMPLOYEE_SKILL_SENTINEL" in text) != ("OTHER_SKILL_SENTINEL" in text) for text in parallel)
            await other.close()
            (source / "SKILL.md").write_text("---\nname: employee-native-fixture\ndescription: UPDATED_SKILL_SENTINEL\n---\nUpdated skill.", encoding="utf-8")
            changed = service.prepare(profile["employee_id"], str(uuid4()), {"revision": 0, "config_values": {}}, project)
            newer = await start(changed)
            freeze_start = len(FakeModel.captures)
            await asyncio.gather(client.prompt("old snapshot"), newer.prompt("new snapshot"))
            frozen = [wire_text(row) for row in main_requests(FakeModel.captures[freeze_start:])]
            result["skill_versions_frozen"] = changed["digest"] != binding["digest"] and len(frozen) == 2 and sum("UPDATED_SKILL_SENTINEL" in text for text in frozen) == 1 and sum("EMPLOYEE_SKILL_SENTINEL" in text for text in frozen) == 1
            await newer.close()
            compact_start = len(FakeModel.captures)
            await client.prompt("/compact")
            # CLI 的压缩完成通知可能早于原生 turn 资源的释放。
            await asyncio.sleep(0.5)
            await client.prompt("after compaction")
            compact = main_requests(FakeModel.captures[compact_start:])
            after = compact[-1]["body"]
            result["post_compaction_identity_count"] = wire_text(after).count("EMPLOYEE_NATIVE_SENTINEL")
            result["post_compaction_identity_location"] = [{"index": index, "role": message.get("role")}
                for index, message in enumerate(after.get("input", after.get("messages", [])))
                if "EMPLOYEE_NATIVE_SENTINEL" in wire_text(message)]
            result["compact_command_in_model_history"] = '"/compact"' in wire_text(after)
            result["pre_compact_history_items"] = len(primary[-1]["body"].get("input", primary[-1]["body"].get("messages", [])))
            result["post_compact_history_items"] = len(after.get("input", after.get("messages", [])))
        result["passed"] = len(primary) == 3 and all(count == 1 for count in result["identity_counts"]) and all(result["skills"]) and all(result["project_rules"]) and result["instruction_stable"] and result["tools_stable"] and result["mcp_preserved"] and result["memory_tools_preserved"] and result["concurrent_isolation"] and result["skill_versions_frozen"] and result["post_compaction_identity_count"] == 1 and not result["compact_command_in_model_history"]
    except Exception as error:
        result.update(passed=False, error=repr(error), stderr=client.stderr_tail[-3000:] if client else "")
    finally:
        for native in clients:
            try:
                await native.close()
            except Exception as error:
                result["cleanup_error"] = str(error)
    atomic_json(directory / "requests.json", FakeModel.captures[first:])
    commands = [data for kind, data in records if kind == "protocol" and data.get("method") == "session/update"
                and data.get("params", {}).get("update", {}).get("sessionUpdate") == "available_commands_update"]
    atomic_json(directory / "commands.json", commands)
    atomic_json(directory / "result.json", result)
    print(json.dumps(result, ensure_ascii=True), flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("harness", choices=["codex", "deepseek", "opencode", "codebuddy"])
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        await verify(args.harness, args.directory.resolve(), f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
