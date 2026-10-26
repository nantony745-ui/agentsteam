"""回合文件卡片：协议提取、持久化和 HTTP，只使用临时仓库。"""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.db.storage import Storage
from app.domain.models import MessageCreate
from app.services.file_changes import extract_file_changes, file_diff, file_changes_summary
from app.delivery.git_delivery import PREVIEW_LIMIT
from test_http_runs import api, http_records, setup_app, repository


def tool(tool_id, **values):
    return {"toolCallId": tool_id, "kind": "edit", "status": "completed", **values}


def edit(path, old, new):
    return {"type": "diff", "path": str(path), "oldText": old, "newText": new}


def test_structured_priority_partial_updates_repeated_snapshots_and_multiple_operations(tmp_path):
    first = edit(tmp_path / "中文 space.txt", "a\nb\nc\n", "a\nB\nc\n")
    events = [tool("a", status="in_progress", rawInput={"path": "中文 space.txt", "old_string": "a", "new_string": "wrong"}),
              {"toolCallId": "a", "content": [first, first]},
              {"toolCallId": "a", "content": [first], "status": "completed"},
              {"toolCallId": "a", "content": [], "status": "completed"},
              tool("b", content=[edit("中文 space.txt", "B\n", "B\nextra\n")]),
              tool("failed", status="failed", content=[edit("bad.txt", None, "no")]),
              tool("pending", status="in_progress", content=[edit("pending.txt", None, "no")])]
    files = extract_file_changes(tmp_path, events)
    assert len(files) == 1
    assert (files[0]["path"], files[0]["additions"], files[0]["deletions"]) == ("中文 space.txt", 2, 1)
    assert files[0]["diff"].count("--- a/") == 2
    assert "wrong" not in files[0]["diff"]


def test_new_empty_cleared_and_unknown_files(tmp_path):
    files = extract_file_changes(tmp_path, [
        tool("new", content=[edit("new.txt", None, "hello\n")]),
        tool("empty", content=[edit("empty.txt", None, "")]),
        tool("clear", content=[edit("clear.txt", "old\n", "")]),
        tool("unknown", rawInput={"file_path": "unknown.txt"}),
        tool("read", kind="read", rawInput={"file_path": "read.txt", "old_string": "a", "new_string": "b"}),
        tool("shell", kind="execute", rawInput={"command": "write", "path": "shell.txt"}),
    ])
    assert [item["path"] for item in files] == ["new.txt", "empty.txt", "clear.txt", "unknown.txt"]
    assert [(item["additions"], item["deletions"]) for item in files] == [(1, 0), (0, 0), (0, 1), (None, None)]
    assert files[2]["operation"] == "modified" and files[2]["previewable"]
    summary = file_changes_summary("r", files)
    assert summary["additions"] is None and summary["deletions"] is None
    assert "diff" not in summary["files"][0] and summary["files"][0]["has_diff"]


def test_edit_write_changes_and_multiedit(tmp_path):
    files = extract_file_changes(tmp_path, [
        tool("edit", title="Edit", rawInput={"file_path": "one.txt", "old_string": "a", "new_string": "b"}),
        tool("write", title="Write", rawInput={"path": "two.txt", "content": "first\nsecond\n"}),
        tool("many", rawInput={"changes": {"one.txt": [{"old_text": "b", "new_text": "c"},
                                                       {"oldText": "c", "newText": "d"}],
                                                    "three.txt": {"diff": "@@ -1 +1 @@\n-a\n+b\n"}}}),
        tool("multi", title="MultiEdit", rawInput={"file_path": "multi.txt", "edits": [
            {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"}]}),
    ])
    assert [item["path"] for item in files] == ["one.txt", "two.txt", "three.txt", "multi.txt"]
    assert [(item["additions"], item["deletions"]) for item in files] == [(3, 3), (2, 0), (1, 1), (2, 2)]


def test_apply_patch_multi_file_delete_and_move(tmp_path):
    patch = """*** Begin Patch
*** Add File: new 中文.txt
+one
+two
*** Update File: old.txt
*** Move to: moved.txt
@@
-old
+new
*** Delete File: deleted.txt
*** End Patch
"""
    files = extract_file_changes(tmp_path, [tool("patch", title="apply_patch", rawInput=patch)])
    assert [item["path"] for item in files] == ["new 中文.txt", "moved.txt", "deleted.txt"]
    assert [(item["additions"], item["deletions"]) for item in files] == [(2, 0), (1, 1), (None, None)]
    assert files[2]["operation"] == "deleted" and not files[2]["previewable"]


def test_git_patch_multi_file_headers_and_content_prefixes(tmp_path):
    patch = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
--- content
+++ content
 context
diff --git a/delete.txt b/delete.txt
deleted file mode 100644
--- a/delete.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""
    files = extract_file_changes(tmp_path, [tool("patch", rawInput={"patch": patch})])
    assert [(item["path"], item["additions"], item["deletions"]) for item in files] == [("a.txt", 1, 1), ("delete.txt", 0, 1)]
    assert files[1]["operation"] == "deleted" and files[1]["diff"]


def test_multiple_hunks_for_one_path_outside_boundary_and_diff_limit(tmp_path):
    files = extract_file_changes(tmp_path, [tool("x", content=[
        edit("in.txt", "a\nb", "A\nb"), edit("in.txt", "c\nd", "c\nD"),
        edit(tmp_path.parent / "outside.txt", None, "outside"), edit(".git/config", None, "no"),
    ])])
    assert (files[0]["additions"], files[0]["deletions"]) == (2, 2)
    assert not files[1]["previewable"] and not files[2]["previewable"]
    item = {"path": "huge", "diff": "中文\n" * PREVIEW_LIMIT}
    response = file_diff(item)
    assert response["truncated"] and len(response["diff"].encode()) <= PREVIEW_LIMIT
    assert "截断" in response["reason"]


def test_air_patch_placeholders_invalid_patch_and_no_newline_change(tmp_path):
    patch = "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n"
    valid = edit("file.txt", None, "")
    valid["_meta"] = {"jetbrains": {"air": {"diffPatch": {"version": 1, "format": "git_patch", "text": patch}}}}
    invalid = {**valid, "path": "unavailable.txt", "_meta": {"jetbrains": {"air": {"diffPatch": {"version": 9}}}}}
    files = extract_file_changes(tmp_path, [tool("air", content=[valid, invalid]),
                                          tool("newline", content=[edit("newline.txt", "a", "a\n")])])
    assert [(item["additions"], item["deletions"]) for item in files] == [(1, 1), (None, None), (1, 1)]
    assert files[1]["operation"] == "modified" and files[1]["diff"] is None
    assert "No newline at end of file" in files[2]["diff"]


def test_unified_patch_does_not_treat_content_as_file_headers(tmp_path):
    patch = '--- a/one.txt\n+++ b/one.txt\n@@ -1,2 +1,2 @@\n--- content\n+++ content\n context\n'
    patch += '--- a/two.txt\n+++ b/two.txt\n@@ -1 +1 @@\n-a\n+b\n'
    files = extract_file_changes(tmp_path, [tool("p", rawInput={"patch": patch})])
    assert [(item["path"], item["additions"], item["deletions"]) for item in files] == [("one.txt", 1, 1), ("two.txt", 1, 1)]


def test_git_quoted_utf8_filename(tmp_path):
    name = '"a/\\344\\270\\255 space.txt"'
    patch = f'--- {name}\n+++ {name.replace("a/", "b/")}\n@@ -1 +1 @@\n-a\n+b\n'
    files = extract_file_changes(tmp_path, [tool("p", rawInput={"patch": patch})])
    assert files[0]["path"] == "中 space.txt"


@pytest.mark.parametrize("source", ["raw", "formatted", "content"])
def test_explicit_patch_output_and_input_shape_write(tmp_path, source):
    patch = "--- a/out.txt\n+++ b/out.txt\n@@ -1 +1 @@\n-old\n+new\n"
    fields = {"rawOutput": patch} if source == "raw" else {"rawOutput": {"formatted_output": patch}} if source == "formatted" else {
        "content": [{"type": "content", "content": {"type": "text", "text": patch}}]}
    files = extract_file_changes(tmp_path, [tool("o", rawInput={"path": "out.txt"}, **fields),
        {"toolCallId": "w", "status": "completed", "rawInput": {"path": "write.txt", "content": "new\n"}},
        tool("u", rawInput={"changes": {"unknown.txt": {"new_text": "missing old text"}}})])
    assert [(item["path"], item["additions"], item["deletions"]) for item in files] == [
        ("out.txt", 1, 1), ("write.txt", 1, 0), ("unknown.txt", None, None)]


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 路径别名")
def test_windows_short_path_and_deleted_file_remain_relative(tmp_path):
    import ctypes
    from ctypes import wintypes

    get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get_short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_short_path.restype = wintypes.DWORD
    root = tmp_path / "long folder name"
    root.mkdir()
    buffer = ctypes.create_unicode_buffer(32768)
    assert get_short_path(str(root), buffer, len(buffer))
    files = extract_file_changes(root.resolve(), [tool("s", content=[edit(Path(buffer.value) / "deleted.txt", "old", "new")])])
    assert files[0]["path"] == "deleted.txt" and files[0]["previewable"]


@pytest.mark.asyncio
async def test_tool_event_projection_skips_superseded_input_and_respects_high(api, http_records, monkeypatch):
    _, app, _ = api
    _, session = http_records
    storage = app.state.storage
    run, _ = await storage.create_run(session["id"], MessageCreate(message="snapshots", request_id=str(uuid4())))
    first = await storage.append_event(run["id"], "agent.tool", {
        "toolCallId": "edit", "kind": "edit", "status": "in_progress",
        "rawInput": {"file_path": "old.txt", "content": "x" * 12000},
    })
    await storage.append_event(run["id"], "agent.tool", {
        "toolCallId": "edit", "rawInput": {"file_path": "new.txt", "content": "new"},
        "status": "completed",
    })
    high = await storage.tool_event_high(run["id"])
    await storage.append_event(run["id"], "agent.tool", tool("later", rawInput={"path": "later.txt", "content": "later"}))
    old_part = await storage._fetch_one("SELECT data FROM event_details WHERE event_id=? AND field='rawInput'", (first["id"],))
    import app.db.storage as storage_module
    original = storage_module.unpack_part

    def unpack(codec, data):
        assert data != old_part["data"]
        return original(codec, data)

    monkeypatch.setattr(storage_module, "unpack_part", unpack)
    events = await storage.run_tool_events(run["id"], high)
    assert [item["toolCallId"] for item in events] == ["edit", "edit"]
    assert [item["path"] for item in extract_file_changes(Path(session["cwd"]), events)] == ["new.txt"]


@pytest.mark.asyncio
async def test_api_history_current_preview_run_isolation_and_reopen(api, http_records, monkeypatch):
    client, app, _ = api
    project, session = http_records
    storage = app.state.storage
    run, _ = await storage.create_run(session["id"], MessageCreate(message="change", request_id=str(uuid4())))
    root = Path(project["path"])
    (root / "文件.txt").write_text("当前版本\n", encoding="utf-8", newline="")
    content = edit(root / "文件.txt", "旧" * 12000, "新" * 12000)
    await storage.append_event(run["id"], "agent.tool", tool("edit", content=[content]))
    base = f"/api/runs/{run['id']}/file-changes"
    loads = []
    original = storage.run_tool_events

    async def counted(run_id, high):
        loads.append((run_id, high))
        return await original(run_id, high)

    monkeypatch.setattr(storage, "run_tool_events", counted)
    summary = (await client.get(base)).json()
    assert summary["files"][0]["path"] == "文件.txt" and summary["additions"] == summary["deletions"] == 1
    response = await client.get(base + "/diff", params={"path": "文件.txt"})
    historical = response.json()
    assert "-" + "旧" * 12000 in historical["diff"]
    assert (await client.get(base + "/preview", params={"path": "文件.txt"})).json()["content"] == "当前版本\n"
    assert len(loads) == 1
    (root / "文件.txt").write_text("另一版本", encoding="utf-8")
    assert (await client.get(base + "/diff", params={"path": "文件.txt"})).json() == historical
    assert len(loads) == 1
    await storage.append_event(run["id"], "agent.tool", tool("added", content=[edit("later.txt", None, "later")]))
    assert [item["path"] for item in (await client.get(base)).json()["files"]] == ["文件.txt", "later.txt"]
    assert len(loads) == 2 and loads[0][1] < loads[1][1]
    other, _ = await storage.create_run(session["id"], MessageCreate(message="other", request_id=str(uuid4())))
    assert (await client.get(base.replace(run["id"], other["id"]))).json()["files"] == []
    assert (await client.get(base.replace(run["id"], str(uuid4())))).status_code == 404
    assert (await client.get(base + "/preview", params={"path": "../outside"})).status_code == 404
    assert (await client.get(base + "/diff", params={"path": "unrelated"})).status_code == 404
    await storage.close()
    await storage.open()
    assert [item["path"] for item in (await client.get(base)).json()["files"]] == ["文件.txt", "later.txt"]
    assert (await client.get(base + "/diff", params={"path": "文件.txt"})).json() == historical
    (root / "文件.txt").unlink()
    assert (await client.get(base + "/preview", params={"path": "文件.txt"})).status_code == 404


@pytest.mark.asyncio
async def test_api_preview_binary_empty_truncated_deleted_outside_and_symlink(api, http_records):
    client, app, _ = api
    project, session = http_records
    root = Path(project["path"])
    run, _ = await app.state.storage.create_run(session["id"], MessageCreate(message="preview", request_id=str(uuid4())))
    outside = root.parent / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    contents = {"binary.bin": b"a\0b", "empty.txt": b"", "big.txt": b"a" * (PREVIEW_LIMIT + 1)}
    for name, value in contents.items():
        (root / name).write_bytes(value)
    blocks = [edit(name, None, "new") for name in [*contents, str(outside), ".git/config", "link.txt"]]
    deletion = "--- a/deleted.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone"
    await app.state.storage.append_event(run["id"], "agent.tool", tool("x", content=blocks))
    await app.state.storage.append_event(run["id"], "agent.tool", tool("d", rawInput={"patch": deletion}))
    base = f"/api/runs/{run['id']}/file-changes"
    files = (await client.get(base)).json()["files"]
    for item in files:
        if not item["previewable"]:
            assert (await client.get(base + "/preview", params={"path": item["path"]})).status_code == 422
    async def preview(path):
        return (await client.get(base + "/preview", params={"path": path})).json()
    assert (await preview("binary.bin"))["kind"] == "binary"
    assert (await preview("empty.txt"))["content"] == ""
    assert (await preview("big.txt"))["truncated"]
    try:
        (root / "link.txt").symlink_to(outside)
    except OSError:
        return
    assert (await client.get(base + "/preview", params={"path": "link.txt"})).status_code == 422
