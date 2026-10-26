"""从一轮已持久化的工具记录提取文件变更；不读取当前 Git 状态。

参照 Codeg session-files::extractReplyFileChanges 和
connection::synthesize_edit_input_from_diffs，直接适配 ACP 结构化内容。
"""

import ast
import difflib
import json
import os
import re
from pathlib import Path
from typing import Any

from app.delivery.git_delivery import PREVIEW_LIMIT


def canonical_path(cwd: Path, path: str) -> Path:
    absolute = Path(os.path.abspath(cwd / path.replace("\\", "/")))
    if os.name == "nt" and re.search(r"~\d", str(absolute)):
        return expand_windows_short_path(absolute)
    return absolute


def expand_windows_short_path(path: Path) -> Path:
    """展开 8.3 别名，保留符号链接身份；已删除文件按仍存在的祖先展开。"""
    import ctypes
    from ctypes import wintypes

    get_long_path = ctypes.WinDLL("kernel32", use_last_error=True).GetLongPathNameW
    get_long_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_long_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    candidate, suffix = path, []
    while True:
        size = get_long_path(str(candidate), buffer, len(buffer))
        if 0 < size < len(buffer):
            return Path(buffer.value).joinpath(*reversed(suffix))
        if candidate == candidate.parent:
            return path
        suffix.append(candidate.name)
        candidate = candidate.parent


def text_change(path: str, old: str | None, new: str) -> dict:
    before, after = (old or "").splitlines(keepends=True), new.splitlines(keepends=True)
    lines = difflib.unified_diff(before, after, fromfile="/dev/null" if old is None else f"a/{path}", tofile=f"b/{path}")
    patch = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
    return patch_change(path, patch, "added" if old is None else "modified")


def unknown_change(path: str, operation: str) -> dict:
    return {"path": path, "operation": operation, "additions": None, "deletions": None, "diff": None}


def patch_change(path: str, patch: str, operation: str) -> dict:
    additions = deletions = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            additions += line.startswith("+")
            deletions += line.startswith("-")
    return {"path": path, "operation": operation, "additions": additions,
            "deletions": deletions, "diff": patch or None}


def header_path(value: str) -> str:
    value = value.split("\t", 1)[0].strip()
    if value.startswith('"'):
        quoted = value
        value = ast.literal_eval(quoted)
        if re.search(r"\\[0-7]{3}", quoted):
            value = value.encode("latin1").decode("utf-8")
    return value[2:] if value.startswith(("a/", "b/")) else value


def unified_sections(patch: str) -> list[str]:
    if re.search(r"^diff --git ", patch, re.M):
        return re.split(r"(?=^diff --git )", patch, flags=re.M)
    lines = patch.splitlines(keepends=True)
    starts = [0]
    old = new = 0
    for index, line in enumerate(lines):
        if old or new:
            old -= line.startswith((" ", "-"))
            new -= line.startswith((" ", "+"))
            continue
        hunk = re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
        if hunk:
            old, new = (int(count) if count is not None else 1 for count in hunk.groups())
        elif line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            if index:
                starts.append(index)
    starts.append(len(lines))
    return ["".join(lines[start:end]) for start, end in zip(starts, starts[1:])]


def unified_changes(patch: str, fallback: str | None) -> list[dict]:
    changes = []
    for section in unified_sections(patch):
        headers = section.split("@@", 1)[0]
        old_match = re.search(r"^--- (.+)$", headers, re.M)
        new_match = re.search(r"^\+\+\+ (.+)$", headers, re.M)
        renamed = re.search(r"^rename to (.+)$", headers, re.M)
        old = header_path(old_match[1]) if old_match else None
        new = header_path(new_match[1]) if new_match else None
        path = new if new and new != "/dev/null" else old or fallback
        if renamed:
            path = header_path(renamed[1])
        if not path or not (old_match and new_match or renamed):
            continue
        operation = "deleted" if new == "/dev/null" else "added" if old == "/dev/null" else "modified"
        changes.append(patch_change(path, section.strip(), operation))
    return changes


def apply_patch_changes(patch: str) -> list[dict]:
    changes = []
    sections = re.split(r"^\*\*\* (Add|Update|Delete) File: (.+)\r?$", patch, flags=re.M)
    for index in range(1, len(sections), 3):
        action, path, body = sections[index:index + 3]
        lines = []
        moved = re.search(r"^\*\*\* Move to: (.+)\r?$", body, re.M)
        if moved:
            path = moved[1]
        for line in body.splitlines():
            if line.startswith("***"):
                continue
            lines.append(line)
        if action == "Delete":
            changes.append(unknown_change(path, "deleted"))
            continue
        if action == "Add":
            change = text_change(path, None, "\n".join(line[1:] for line in lines if line.startswith("+")))
        else:
            # apply_patch 的 @@ 可以没有行号；保留工具原文，不编造位置。
            text = f"--- a/{path}\n+++ b/{path}\n" + "\n".join(lines)
            change = patch_change(path, text, "modified")
        changes.append(change)
    return changes


def parse_patch(patch: str, fallback: str | None) -> list[dict]:
    if "*** Begin Patch" in patch:
        return apply_patch_changes(patch)
    changes = unified_changes(patch, fallback)
    if not changes and fallback and re.search(r"^@@", patch, re.M):
        changes = [patch_change(fallback, patch, "modified")]
    return changes


def input_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def edit_change(path: str, value: dict) -> list[dict]:
    patch = value.get("diff") or value.get("unified_diff")
    if isinstance(patch, str):
        return parse_patch(patch, path)
    old_key = next((key for key in ("oldText", "old_text", "old_string") if key in value), None)
    new_key = next((key for key in ("newText", "new_text", "new_string") if key in value), None)
    if old_key is not None and new_key is not None and isinstance(value[new_key], str):
        old = value[old_key]
        if old is None or isinstance(old, str):
            return [text_change(path, old, value[new_key])]
    return []


def structured_changes(content: list) -> list[dict]:
    changes, seen = [], set()
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "diff" or not block.get("path"):
            continue
        identity = json.dumps(block, sort_keys=True, ensure_ascii=False)
        if identity in seen:
            continue
        seen.add(identity)
        meta = block.get("_meta") or {}
        air = (meta.get("jetbrains") or {}).get("air") or {}
        patch = air.get("diffPatch", meta.get("diffPatch"))
        if patch is not None:
            parsed = []
            if (isinstance(patch, dict) and patch.get("version") == 1
                    and patch.get("format") == "git_patch" and isinstance(patch.get("text"), str)
                    and re.search(r"^@@", patch["text"], re.M)):
                parsed = parse_patch(patch["text"], block["path"])
                for change in parsed:
                    change["path"] = block["path"]
            changes.extend(parsed or [unknown_change(block["path"], "modified")])
            continue
        changes.extend(edit_change(block["path"], {"oldText": block.get("oldText"), "newText": block.get("newText")}))
    return changes


def tool_operation(tool: dict, value: dict) -> str | None:
    name = re.split(r"[\s(/]", tool.get("title", "").lower())[0].rsplit(".", 1)[-1]
    if tool.get("kind") in {"read", "execute", "search", "fetch", "think"}:
        return None
    if name in {"edit", "write", "apply_patch", "multiedit", "multi_edit", "delete"}:
        return name
    if tool.get("kind") in {"edit", "delete"}:
        return tool["kind"]
    if "changes" in value or "old_string" in value or "old_text" in value:
        return "edit"
    if isinstance(value.get("content"), str) and (value.get("file_path") or value.get("path")):
        return "write"
    return None


def raw_changes(tool: dict) -> list[dict]:
    raw = tool.get("rawInput")
    value = input_object(raw)
    operation = tool_operation(tool, value)
    if operation is None:
        return []
    path = value.get("file_path") or value.get("path") or value.get("filePath")
    patch = raw if isinstance(raw, str) and "*** Begin Patch" in raw else value.get("patch") or value.get("input")
    if isinstance(patch, str):
        parsed = parse_patch(patch, path)
        if parsed:
            return parsed
    changes = []
    for name, item in (value.get("changes") or {}).items():
        for edit in item if isinstance(item, list) else [item]:
            if isinstance(edit, dict):
                changes.extend(edit_change(name, edit) or [unknown_change(name, "modified")])
    if changes:
        return changes
    output = tool.get("rawOutput")
    outputs = [output] if isinstance(output, str) else [output.get(key) for key in ("diff", "patch", "formatted_output")] if isinstance(output, dict) else []
    for output_text in [*outputs, tool.get("patch_output")]:
        if isinstance(output_text, str):
            changes = parse_patch(output_text, path)
            if changes:
                break
    if changes:
        return changes
    if path:
        edits = value.get("edits") or [value]
        for edit in edits:
            changes.extend(edit_change(path, edit))
        if not changes and isinstance(value.get("content"), str):
            changes = [text_change(path, None, value["content"])]
    if not changes:
        paths = [path] if path else [location["path"] for location in tool.get("locations") or [] if location.get("path")]
        changes = [unknown_change(name, "deleted" if operation == "delete" else "modified") for name in paths]
    return changes


def completed_tools(events: list[dict]) -> list[dict]:
    tools = {}
    for event in events:
        tool_id = event.get("toolCallId")
        if not tool_id:
            continue
        tool = tools.setdefault(tool_id, {})
        tool.update({key: value for key, value in event.items() if value is not None and key != "content"})
        # content 是替换快照；末尾状态通知的空 content 不应抹掉先前提供的文件差异。
        if any(item.get("type") == "diff" for item in event.get("content") or [] if isinstance(item, dict)):
            tool["content"] = event["content"]
        text = [item.get("content", {}).get("text") for item in event.get("content") or []
                if isinstance(item, dict) and item.get("type") == "content" and isinstance(item.get("content"), dict)]
        if any(isinstance(value, str) for value in text):
            tool["patch_output"] = "\n".join(value for value in text if isinstance(value, str))
    return [tool for tool in tools.values() if tool.get("status") == "completed"]


def extract_file_changes(cwd: Path, events: list[dict]) -> list[dict]:
    files = {}
    root = canonical_path(cwd, ".")
    for tool in completed_tools(events):
        structured = structured_changes(tool.get("content") or [])
        preferred = {canonical_path(cwd, change["path"]) for change in structured}
        changes = structured + [change for change in raw_changes(tool) if canonical_path(cwd, change["path"]) not in preferred]
        for change in changes:
            absolute = canonical_path(cwd, change["path"])
            inside = absolute.is_relative_to(root)
            path = absolute.relative_to(root).as_posix() if inside else absolute.as_posix()
            target = files.setdefault(absolute, {"path": path, "operation": change["operation"],
                                                "additions": 0, "deletions": 0, "diff": None, "previewable": inside})
            for field in ("additions", "deletions"):
                target[field] = target[field] + change[field] if target[field] is not None and change[field] is not None else None
            if change["diff"]:
                target["diff"] = "\n\n".join(filter(None, (target["diff"], change["diff"])))
            if change["operation"] == "deleted" or target["operation"] == "deleted":
                target["operation"] = change["operation"]
    for item in files.values():
        item["previewable"] = (item["previewable"] and item["operation"] != "deleted"
                               and not any(part.lower().rstrip(" .") == ".git" for part in Path(item["path"]).parts))
    return list(files.values())


def file_changes_summary(run_id: str, files: list[dict]) -> dict:
    totals = {field: sum(item[field] for item in files) if all(item[field] is not None for item in files) else None
              for field in ("additions", "deletions")}
    return {"run_id": run_id, **totals, "files": [{key: value for key, value in item.items() if key != "diff"}
                                                  | {"has_diff": bool(item["diff"])} for item in files]}


def file_diff(item: dict) -> dict:
    data = (item["diff"] or "").encode("utf-8")
    truncated = len(data) > PREVIEW_LIMIT
    return {"path": item["path"], "diff": data[:PREVIEW_LIMIT].decode("utf-8", "ignore") if item["diff"] else None,
            "truncated": truncated, "reason": "差异超过 1 MiB，已截断" if truncated else None}
