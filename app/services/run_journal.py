"""按接收顺序合并流式片段，并批量提交会话事件。"""

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.db.event_codec import TEXT_LIMIT, ToolOutputTracker, compact, utf8_parts
from app.db.storage import Storage


class JournalOverloadError(RuntimeError):
    pass


@dataclass
class JournalBudget:
    bytes: int = 0
    items: int = 0
    max_bytes: int = 64 * 1024 * 1024
    max_items: int = 16384


def _text_key(payload: dict) -> dict:
    key = {name: value for name, value in payload.items() if name != "text"}
    metadata = key.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("_meta"), dict):
        key["metadata"] = {**metadata, "_meta": {
            name: value for name, value in metadata["_meta"].items()
            if name not in {"timestamp", "timestamp_start"}
        }}
    return key


def _merge_text(previous: dict, incoming: dict) -> dict:
    result = {**previous, "text": previous["text"] + incoming["text"]}
    old_metadata, new_metadata = previous.get("metadata"), incoming.get("metadata")
    if isinstance(old_metadata, dict) and isinstance(new_metadata, dict):
        old_meta, new_meta = old_metadata.get("_meta"), new_metadata.get("_meta")
        if isinstance(old_meta, dict) and isinstance(new_meta, dict) and "timestamp" in new_meta:
            merged_meta = dict(old_meta)
            if "timestamp" in old_meta:
                merged_meta.setdefault("timestamp_start", old_meta["timestamp"])
            merged_meta["timestamp"] = new_meta["timestamp"]
            result["metadata"] = {**old_metadata, "_meta": merged_meta}
    return result


class RunJournal:
    def __init__(self, storage: Storage, run_id: str, budget: JournalBudget | None = None,
                 max_bytes: int = 8 * 1024 * 1024, max_items: int = 2048) -> None:
        self.storage, self.run_id = storage, run_id
        self.budget = budget if budget is not None else JournalBudget()
        self.max_bytes, self.max_items = max_bytes, max_items
        self.bytes = self.items = 0
        self.queue = asyncio.Queue()
        self.pending: list[tuple[dict, int]] = []
        self.timer: asyncio.TimerHandle | None = None
        self.error: Exception | None = None
        self.write_error: Exception | None = None
        self.failed = asyncio.Event()
        self.accepting = True
        self.closing = False
        self.output_tracker = ToolOutputTracker()
        self.task = asyncio.create_task(self._write(), name=f"events:{run_id}")

    def _admit(self, size: int, count: int) -> bool:
        if (self.bytes + size > self.max_bytes or self.items + count > self.max_items
                or self.budget.bytes + size > self.budget.max_bytes
                or self.budget.items + count > self.budget.max_items):
            self.error = JournalOverloadError("事件写入过载：待写预算已用尽，执行已停止")
            self.failed.set()
            self.accepting = False
            return False
        self.bytes += size
        self.items += count
        self.budget.bytes += size
        self.budget.items += count
        return True

    def _release(self, size: int, count: int) -> None:
        self.bytes -= size
        self.items -= count
        self.budget.bytes -= size
        self.budget.items -= count

    def _schedule(self) -> None:
        if self.timer is None:
            self.timer = asyncio.get_running_loop().call_later(0.1, self._seal_batch)

    def _seal_batch(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.pending:
            self.queue.put_nowait(("batch", self.pending, None))
            self.pending = []

    def _append(self, event: dict) -> None:
        size = len(compact(event).encode("utf-8"))
        if self._admit(size, 1):
            self.pending.append((event, size))
            self._schedule()

    def _receive_text(self, event: dict) -> None:
        payload = event["payload"]
        if self.pending:
            previous, old_size = self.pending[-1]
            if (previous["kind"] == "agent.text"
                    and _text_key(previous["payload"]) == _text_key(payload)
                    and len((previous["payload"]["text"] + payload["text"]).encode("utf-8")) <= TEXT_LIMIT):
                merged = {"kind": "agent.text", "payload": _merge_text(previous["payload"], payload)}
                size = len(compact(merged).encode("utf-8"))
                if self._admit(size - old_size, 0):
                    self.pending[-1] = (merged, size)
                return
        self._append(event)

    def receive(self, event: dict[str, Any]) -> None:
        if not self.accepting:
            return
        if event["kind"] != "agent.text":
            self._append(event)
            return
        for part in utf8_parts(event["payload"]["text"], TEXT_LIMIT):
            payload = {**event["payload"], "text": part.decode("utf-8")}
            self._receive_text({"kind": "agent.text", "payload": payload})
            if not self.accepting:
                break

    async def _write(self) -> None:
        next_command = None
        while True:
            command, value, extra = next_command or await self.queue.get()
            next_command = None
            commands = 1
            if command == "batch":
                while not self.queue.empty():
                    following = self.queue.get_nowait()
                    if following[0] != "batch":
                        next_command = following
                        break
                    value.extend(following[1])
                    commands += 1
            try:
                if command == "batch":
                    if self.write_error is None:
                        events = []
                        for event, _ in value:
                            payload = event["payload"]
                            if event["kind"] == "agent.tool":
                                payload = self.output_tracker.normalize(payload)
                            events.append((event["kind"], payload))
                        await self.storage.append_events(self.run_id, events)
                elif command == "close":
                    return
                elif self.write_error is not None:
                    extra.set_exception(self.write_error)
                elif value is None:
                    extra.set_result(None)
                else:
                    extra.set_result(await value())
            except Exception as error:
                if command == "batch" or isinstance(error, (sqlite3.OperationalError, OSError)):
                    self._fail(error)
                if command != "batch" and not extra.done():
                    extra.set_exception(error)
            finally:
                if command == "batch":
                    self._release(sum(size for _, size in value), len(value))
                for _ in range(commands):
                    self.queue.task_done()

    def _fail(self, error: Exception) -> None:
        self.write_error = self.error = error
        self.accepting = False
        self.failed.set()
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self._release(sum(size for _, size in self.pending), len(self.pending))
        self.pending = []

    async def call(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        if self.closing or self.task.done():
            raise RuntimeError("写入器已关闭")
        self._seal_batch()
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait(("control", operation, future))
        return await asyncio.shield(future)

    async def flush(self) -> None:
        if self.closing or self.task.done():
            await self.task
            if self.error is not None:
                raise self.error
            return
        self._seal_batch()
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait(("flush", None, future))
        await asyncio.shield(future)

    async def close(self) -> None:
        self.accepting = False
        if not self.closing and not self.task.done():
            self.closing = True
            self._seal_batch()
            self.queue.put_nowait(("close", None, None))
        await self.task
        if self.error is not None:
            raise self.error
