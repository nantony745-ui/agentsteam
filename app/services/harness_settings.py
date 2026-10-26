"""独立的无 prompt 能力探测；不会创建业务会话或注入 MCP。"""

import asyncio
import json
import shutil
import time
from copy import deepcopy
from pathlib import Path

from acp import Client
from acp.schema import DeniedOutcome, RequestPermissionResponse

from app.agents.acp_client import load_agents
from app.agents.harness_config import HARNESS_LABELS, PERMISSION_KEYS, actual_values, apply_values, configuration_fields, launch_settings


class ProbeHost(Client):
    async def session_update(self, session_id, update, **kwargs):
        return None

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


class HarnessSettings:
    def __init__(self, storage, client_factory, root: Path):
        self.storage = storage
        self.client_factory = client_factory
        self.root = root
        self.locks = {name: asyncio.Lock() for name in HARNESS_LABELS}
        self.cache = {}
        self.statuses = {}
        self.active = set()
        self.closing = False

    async def list(self) -> list[dict]:
        agents = load_agents()
        result = []
        for harness, name in HARNESS_LABELS.items():
            spec = agents.get(harness)
            installed = spec is not None and shutil.which(spec["executable"]) is not None
            if installed and spec["args"] and Path(spec["args"][0]).is_absolute():
                installed = Path(spec["args"][0]).is_file()
            result.append({"id": harness, "name": name, "installed": installed,
                           "enabled": bool(spec and spec["enabled"]),
                           "status": self.statuses.get(harness, {"state": "unverified"}),
                           "settings": await self.storage.get_harness_settings(harness)})
        return result

    async def directory(self, project_id) -> Path:
        if project_id is None:
            return self.root
        project = await self.storage.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        return Path(project["path"])

    async def probe(self, harness: str, project_id, values: dict[str, str], force: bool) -> dict:
        cwd = await self.directory(project_id)
        key = (harness, str(cwd), json.dumps(values, sort_keys=True))
        if self.closing:
            raise RuntimeError("设置服务正在关闭")
        task = asyncio.current_task()
        self.active.add(task)
        try:
            async with self.locks[harness]:
                entry = self.cache.get(key)
                if not force and entry and time.monotonic() - entry[0] < 30:
                    self.statuses[harness] = {"state": "ready", "checked_at": entry[1]["checked_at"]}
                    return deepcopy(entry[1])
                self.statuses[harness] = {"state": "probing"}
                snapshot = await self._probe(harness, cwd, values)
                self.cache[key] = (time.monotonic(), snapshot)
                self.statuses[harness] = {"state": "ready", "checked_at": snapshot["checked_at"]}
                return deepcopy(snapshot)
        except Exception as error:
            self.statuses[harness] = {"state": "failed", "error": str(error)}
            raise
        finally:
            self.active.discard(task)

    async def _probe(self, harness, cwd, values) -> dict:
        from datetime import UTC, datetime

        client = self.client_factory(harness, ProbeHost(), lambda *_: None, lambda _: None, lambda _: None)
        launch_settings(client, values)
        try:
            async with asyncio.timeout(120):
                await client.connect(cwd, [])
                baseline = {**actual_values(client), **client.config_defaults}
                invalid = await apply_values(client, {key: value for key, value in values.items() if key == "model"}, False)
                baseline.update({key: value for key, value in actual_values(client).items()
                                 if key != "model" and key not in PERMISSION_KEYS[harness]})
                invalid += await apply_values(client, {key: value for key, value in values.items() if key != "model"}, False)
                fields = configuration_fields(client)
                for field in fields:
                    field["default_value"] = baseline.get(field["id"])
                return {"harness": harness, "cwd": str(cwd), "fields": fields,
                        "invalid": invalid, "checked_at": datetime.now(UTC).isoformat()}
        finally:
            await client.close()

    async def save(self, harness, project_id, values) -> dict:
        snapshot = await self.probe(harness, project_id, values, False)
        if snapshot["invalid"]:
            raise ValueError("；".join(item["reason"] for item in snapshot["invalid"]))
        return await self.storage.save_harness_settings(harness, values)

    async def close(self) -> None:
        self.closing = True
        tasks = list(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
