"""本地 ACP 测试进程：记录进程/协议 cwd，不连接任何模型。"""

import json
import os
import subprocess
import sys
from pathlib import Path


def send(value):
    print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)


session_cwd = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if "id" not in message:
        continue
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {},
                  "agentInfo": {"name": "cwd-test-agent", "version": "1"}}
    elif method == "session/new":
        session_cwd = message["params"]["cwd"]
        result = {"sessionId": "local-cwd-session"}
    elif method == "session/prompt":
        git_root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                  capture_output=True, encoding="utf-8")
        evidence = {"process_cwd": os.getcwd(), "acp_cwd": session_cwd,
                    "git_root": git_root.stdout.strip() if git_root.returncode == 0 else None}
        Path("worker evidence.txt").write_text(json.dumps(evidence), encoding="utf-8")
        send({"method": "session/update", "params": {"sessionId": "local-cwd-session", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": json.dumps(evidence)},
        }}})
        result = {"stopReason": "end_turn"}
    else:
        send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported method"}})
        continue
    send({"id": message["id"], "result": result})
