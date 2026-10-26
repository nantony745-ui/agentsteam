"""4.3：固定基线、NUL 路径与有界文件预览；真实 Git/HTTP/SQLite。"""

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from app.delivery.git_delivery import PREVIEW_LIMIT, git, read_preview
from test_git_delivery import commit, delegate, delivery, finish, task_input, worktree
from test_storage import repository


@pytest_asyncio.fixture
async def diff_context(delivery):
    http, app, _, repo, _ = delivery
    (repo / "tracked.txt").write_text("原始内容\n", encoding="utf-8")
    (repo / "delete.txt").write_text("待删除\n", encoding="utf-8")
    await git(repo, "add", "--", "tracked.txt", "delete.txt")
    commit(repo, "固定测试基线")
    handle = await delegate(delivery, task_input(await worktree(repo)))
    await finish(delivery, handle)
    task = await app.state.storage.get_task(handle["task_id"])
    yield http, app, task, Path(task["worktree"])


async def diff(context):
    http, _, task, _ = context
    response = await http.get(f"/api/tasks/{task['id']}/diff")
    assert response.status_code == 200, response.text
    return response.json()


async def preview(context, path):
    http, _, task, _ = context
    return await http.get(f"/api/tasks/{task['id']}/preview", params={"path": path})


@pytest.mark.asyncio
async def test_unstaged_staged_committed_and_deleted_changes_remain_visible(diff_context):
    _, _, task, root = diff_context
    name = "新增 中文 space.txt"
    (root / "tracked.txt").write_text("修改后的内容\n", encoding="utf-8")
    (root / name).write_text("新增正文 <script>\n", encoding="utf-8")
    (root / "delete.txt").unlink()
    initial = await diff(diff_context)
    assert initial["base_sha"] == initial["head_sha"] == task["base_sha"]
    assert initial["branch"] == task["branch"] and initial["worktree"] == str(root)
    assert initial["untracked"] == [name]
    assert "+修改后的内容" in initial["patch"] and "deleted file mode" in initial["patch"]
    assert name in initial["status"]
    response = await preview(diff_context, name)
    assert response.json() == {"path": name, "kind": "text", "content": (root / name).read_bytes().decode("utf-8"),
                               "truncated": False, "reason": None}
    await git(root, "add", "-A")
    staged = await diff(diff_context)
    assert staged["untracked"] == [] and "+新增正文" in staged["patch"]
    assert staged["head_sha"] == task["base_sha"]
    commit(root, "提交交付")
    committed = await diff(diff_context)
    assert committed["status"] == "" and committed["head_sha"] != task["base_sha"]
    assert committed["base_sha"] == task["base_sha"]
    assert committed["patch"] == staged["patch"]
    assert "+修改后的内容" in committed["patch"] and "deleted file mode" in committed["patch"]
    assert (await preview(diff_context, "delete.txt")).status_code == 404


@pytest.mark.asyncio
async def test_diff_and_preview_do_not_stage_or_change_index(diff_context):
    _, _, _, root = diff_context
    (root / "tracked.txt").write_text("已暂存\n", encoding="utf-8")
    await git(root, "add", "tracked.txt")
    (root / "tracked.txt").write_text("未暂存\n", encoding="utf-8")
    (root / "new name.txt").write_text("未跟踪", encoding="utf-8")
    index_path = Path((await git(root, "rev-parse", "--path-format=absolute", "--git-path", "index")).decode().strip())
    before_index = index_path.read_bytes()
    before_status = await git(root, "status", "--porcelain=v1", "-z")
    before_cached = await git(root, "diff", "--cached")
    await diff(diff_context)
    await preview(diff_context, "new name.txt")
    assert await git(root, "diff", "--cached") == before_cached
    assert await git(root, "status", "--porcelain=v1", "-z") == before_status
    # status 本身可能刷新索引的 stat 缓存；只读交付查询也不得产生这种写入。
    assert index_path.read_bytes() == before_index


@pytest.mark.asyncio
async def test_binary_empty_truncated_and_utf8_boundary(diff_context):
    _, _, _, root = diff_context
    samples = {"binary.bin": b"binary\0data", "invalid.bin": b"\xff\xfe", "empty.txt": b"",
               "large.txt": b"x" * (PREVIEW_LIMIT + 10),
               "utf8.txt": b"x" * (PREVIEW_LIMIT - 1) + "中".encode()}
    for name, content in samples.items():
        (root / name).write_bytes(content)
    for name in ("binary.bin", "invalid.bin"):
        result = (await preview(diff_context, name)).json()
        assert result["kind"] == "binary" and result["content"] is None and result["reason"]
    empty = (await preview(diff_context, "empty.txt")).json()
    assert empty["kind"] == "text" and empty["content"] == "" and not empty["truncated"]
    large = (await preview(diff_context, "large.txt")).json()
    assert len(large["content"]) == PREVIEW_LIMIT and large["truncated"] and "截断" in large["reason"]
    utf8 = (await preview(diff_context, "utf8.txt")).json()
    assert utf8["kind"] == "text" and utf8["truncated"] and "\ufffd" not in utf8["content"]
    assert len(utf8["content"]) == PREVIEW_LIMIT - 1
    await git(root, "add", "binary.bin")
    assert "Binary files" in (await diff(diff_context))["patch"]


@pytest.mark.parametrize("path", ["../outside.txt", "..\\outside.txt", ".git", ".git/config",
                                  ".GIT/config", "D:\\outside.txt", "/outside.txt", "file.txt:stream", ""])
@pytest.mark.asyncio
async def test_preview_rejects_outside_absolute_and_git_paths(diff_context, path):
    assert (await preview(diff_context, path)).status_code == 422


@pytest.mark.asyncio
async def test_preview_non_file_missing_and_unknown_task(diff_context):
    http, _, _, root = diff_context
    (root / "directory").mkdir()
    assert (await preview(diff_context, "directory")).status_code == 422
    assert (await preview(diff_context, "not there.txt")).status_code == 404
    assert (await http.get(f"/api/tasks/{uuid4()}/diff")).status_code == 404
    assert (await http.get(f"/api/tasks/{uuid4()}/preview", params={"path": ".git"})).status_code == 404
    assert (await http.get("/api/tasks/not-a-uuid/diff")).status_code == 422


@pytest.mark.asyncio
async def test_unprepared_legacy_and_changed_branch_are_conflicts(delivery):
    http, app, _, repo, parent = delivery
    task, child, run, _ = await app.state.storage.create_task_with_run(parent["id"], task_input())
    response = await http.get(f"/api/tasks/{task['id']}/diff")
    assert response.status_code == 409 and "尚无隔离" in response.json()["detail"]
    assert (await http.get(f"/api/tasks/{task['id']}/preview", params={"path": "anything"})).status_code == 409
    handle = await delegate(delivery, task_input(await worktree(repo)))
    await finish(delivery, handle)
    task = await app.state.storage.get_task(handle["task_id"])
    await git(Path(task["worktree"]), "checkout", "-b", "changed-branch")
    assert (await http.get(f"/api/tasks/{task['id']}/diff")).status_code == 409
    assert (await http.get(f"/api/tasks/{task['id']}/preview", params={"path": "anything"})).status_code == 409


def directory_link(path, target):
    if os.name == "nt":
        quoted = [str(value).replace("'", "''") for value in (path, target)]
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"New-Item -ItemType Junction -Path '{quoted[0]}' -Target '{quoted[1]}' -ErrorAction Stop"],
                       check=True, capture_output=True)
    else:
        path.symlink_to(target, target_is_directory=True)


def test_resolved_link_outside_or_git_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("private config")
    for name, target in (("escape", outside), ("config", root / ".git")):
        directory_link(root / name, target)
    for name in ("escape/private.txt", "config/config"):
        with pytest.raises(ValueError):
            read_preview(root, name)


@pytest.mark.parametrize("destination", ["outside", ".git"])
def test_link_swap_after_resolution_is_rejected_before_reading(tmp_path, monkeypatch, destination):
    root = tmp_path / "root"
    root.mkdir()
    safe = root / "safe"
    safe.mkdir()
    (safe / "private.txt").write_text("original")
    target = tmp_path / "outside" if destination == "outside" else root / ".git"
    target.mkdir()
    (target / "private.txt").write_text("secret")
    original_open = Path.open

    def swapped_open(path, *args, **kwargs):
        if path == safe / "private.txt":
            safe.rename(root / "saved")
            directory_link(safe, target)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swapped_open)
    with pytest.raises(ValueError, match="worktree|.git"):
        read_preview(root, "safe/private.txt")
