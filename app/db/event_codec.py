"""业务事件的轻量索引与可逆详情编码；实时写入和离线整理共用。"""

import json
import hashlib
import zlib
from typing import Any

TEXT_LIMIT = 4096
DETAIL_LIMIT = 8192
PART_LIMIT = 65536


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def utf8_parts(text: str, limit: int):
    data = text.encode("utf-8")
    start = 0
    while start < len(data):
        end = min(start + limit, len(data))
        while end < len(data) and data[end] & 0xC0 == 0x80:
            end -= 1
        yield data[start:end]
        start = end


def pack_payload(kind: str, payload: dict) -> tuple[dict, list[tuple]]:
    parts = []

    def visit(value, path, force=False, operation="replace"):
        if isinstance(value, dict) and "$detail" in value:
            return value
        encoded = value if isinstance(value, str) else compact(value)
        size = len(encoded.encode("utf-8"))
        if force or size > DETAIL_LIMIT:
            for chunk in utf8_parts(encoded, PART_LIMIT):
                compressed = zlib.compress(chunk, 3)
                codec, blob = ("zlib", compressed) if len(compressed) < len(chunk) else ("utf8", chunk)
                parts.append((path, len(parts), codec, len(chunk), blob))
            return {"$detail": path, "bytes": size, "preview": encoded[:160],
                    "format": "text" if isinstance(value, str) else "json", "operation": operation}
        return value

    result = {}
    for key, value in payload.items():
        if key == "raw" and kind != "raw":
            continue
        if kind in {"run.status", "task.status"} and key == "result":
            continue
        if kind == "agent.tool" and key == "_meta" and isinstance(value, dict):
            result[key] = {name: visit(item, f"{key}/{name}", name == "terminal_output_delta",
                                       "append" if name == "terminal_output_delta" else "replace")
                           for name, item in value.items()}
        else:
            result[key] = visit(value, key, kind == "agent.tool" and key in {"rawOutput", "content"})
    return result, parts


def business_payload(kind: str, payload: dict) -> dict:
    raw = payload.get("raw")
    if isinstance(raw, dict) and ("jsonrpc" in raw or "method" in raw):
        from app.agents.acp_client import normalize_update
        normalized = normalize_update(raw)["payload"]
        if kind == "raw":
            return normalized
        payload = {**normalized, **{key: value for key, value in payload.items() if key != "raw"}}
    result = {key: value for key, value in payload.items() if kind == "raw" or key != "raw"}
    if isinstance(result.get("_meta"), dict):
        result["_meta"] = {key: value for key, value in result["_meta"].items()
                           if key not in {"traceId", "spanId", "trace_id", "span_id"}}
    if kind in {"run.status", "task.status"}:
        result.pop("result", None)
    return result


class ToolOutputTracker:
    def __init__(self):
        self.hashes = {}

    def normalize(self, payload: dict) -> dict:
        tool = payload.get("toolCallId")
        delta = (payload.get("_meta") or {}).get("terminal_output_delta")
        text = delta.get("data") if isinstance(delta, dict) else delta
        if isinstance(text, str):
            self.hashes.setdefault(tool, hashlib.sha256()).update(text.encode("utf-8"))
        output = payload.get("rawOutput")
        text = output.get("formatted_output") if isinstance(output, dict) else output
        if isinstance(text, str) and tool in self.hashes:
            if hashlib.sha256(text.encode("utf-8")).digest() == self.hashes[tool].digest():
                payload = dict(payload)
                if isinstance(output, dict):
                    payload["rawOutput"] = {key: value for key, value in output.items() if key != "formatted_output"}
                else:
                    payload.pop("rawOutput")
                payload["output_repeated"] = True
        if payload.get("status") in {"completed", "failed"}:
            self.hashes.pop(tool, None)
        return payload


def unpack_part(codec: str, blob: bytes) -> str:
    return (zlib.decompress(blob) if codec == "zlib" else blob).decode("utf-8")


def restore_payload(payload: dict, parts: list[dict]) -> dict:
    fields = {}
    for part in parts:
        fields.setdefault(part["field"], []).append(unpack_part(part["codec"], part["data"]))

    def restore(value):
        if isinstance(value, dict) and "$detail" in value:
            text = "".join(fields.get(value["$detail"], []))
            return json.loads(text) if value["format"] == "json" else text
        if isinstance(value, dict):
            return {key: restore(item) for key, item in value.items()}
        return value

    return restore(payload)
