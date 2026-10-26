"""Task 固定基线、worktree 与只读变更查询。"""

import asyncio
import codecs
import os
from pathlib import Path, PureWindowsPath
from typing import Any

from app.agents.acp_client import kill_process_tree


PREVIEW_LIMIT = 1024 * 1024


class DeliveryConflictError(ValueError):
    """交付目录尚未就绪，或已不符合持久化记录。"""


class WorkingDirectoryError(ValueError):
    """委派选择的执行目录无效。"""


async def git_output(cwd: Path, *args: str) -> tuple[bytes, bytes, int]:
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        "git", "--no-optional-locks", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0,
    ))
    try:
        proc = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        proc = await spawning
        await kill_process_tree(proc, lambda kind, data: None)
        await proc.communicate()
        raise
    reading = asyncio.create_task(proc.communicate())
    try:
        output, error = await asyncio.shield(reading)
    except asyncio.CancelledError:
        await kill_process_tree(proc, lambda kind, data: None)
        await reading
        raise
    return output, error, proc.returncode


async def git(cwd: Path, *args: str) -> bytes:
    output, error, returncode = await git_output(cwd, *args)
    if returncode:
        raise DeliveryConflictError(f"git {' '.join(args)} 失败：{error.decode('utf-8', 'replace').strip()}")
    return output


async def is_git_worktree(path: Path) -> bool:
    output, error, returncode = await git_output(path, "rev-parse", "--is-inside-work-tree")
    if returncode:
        if b"not a git repository" in error:
            return False
        raise DeliveryConflictError(f"无法读取 Git 状态：{error.decode('utf-8', 'replace').strip()}")
    return output.strip() == b"true"


async def repository_root(path: Path) -> Path:
    if not path.is_absolute() or not path.is_dir():
        raise DeliveryConflictError("Git 目录不存在或不是绝对路径")
    root = Path((await git(path, "rev-parse", "--show-toplevel")).decode("utf-8").strip()).resolve()
    if path.resolve() != root:
        raise DeliveryConflictError("记录目录不是 Git 根目录")
    return root


async def capture_base(repo: Path) -> tuple[str, str | None]:
    await repository_root(repo)
    sha = (await git(repo, "rev-parse", "--verify", "HEAD^{commit}")).decode("ascii").strip()
    branch = (await git(repo, "rev-parse", "--abbrev-ref", "HEAD")).decode("utf-8").strip()
    return sha, None if branch == "HEAD" else branch


async def validate_worktree(task: dict[str, Any], repo: Path) -> Path:
    if not all(task[key] for key in ("base_sha", "worktree")):
        raise DeliveryConflictError("尚无隔离交付目录")
    root = await repository_root(Path(task["worktree"]))
    await repository_root(repo)
    common = "rev-parse", "--path-format=absolute", "--git-common-dir"
    actual = Path((await git(root, *common)).decode("utf-8").strip()).resolve()
    expected = Path((await git(repo, *common)).decode("utf-8").strip()).resolve()
    branch = (await git(root, "rev-parse", "--abbrev-ref", "HEAD")).decode("utf-8").strip()
    branch = None if branch == "HEAD" else branch
    if actual != expected or branch != task["branch"] or root == repo.resolve():
        raise DeliveryConflictError("worktree 的仓库或分支与任务记录不一致")
    await git(root, "cat-file", "-e", f"{task['base_sha']}^{{commit}}")
    return root


async def selected_worktree(root: Path, repo: Path) -> tuple[str, str | None] | None:
    if root == repo.resolve():
        return None
    if not await is_git_worktree(repo):
        return None
    registered = await git(repo, "worktree", "list", "--porcelain", "-z")
    roots = [Path(field[9:].decode("utf-8")).resolve()
             for field in registered.split(b"\0") if field.startswith(b"worktree ")]
    if root not in roots:
        return None
    sha, branch = await capture_base(root)
    await validate_worktree({"worktree": str(root), "base_sha": sha, "branch": branch}, repo)
    return sha, branch


async def diff_task(task: dict[str, Any], repo: Path) -> dict[str, Any]:
    root = await validate_worktree(task, repo)
    head = (await git(root, "rev-parse", "--verify", "HEAD^{commit}")).decode("ascii").strip()
    patch = await git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", task["base_sha"], "--")
    status = await git(root, "-c", "core.quotepath=false", "status", "--short")
    untracked = await git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return {"task_id": task["id"], "base_sha": task["base_sha"], "head_sha": head,
            "branch": task["branch"], "worktree": str(root),
            "status": status.decode("utf-8", "replace"), "patch": patch.decode("utf-8", "replace"),
            "untracked": [path.decode("utf-8") for path in untracked.split(b"\0") if path]}


def opened_path(fd: int) -> Path:
    if os.name != "nt":
        return Path(f"/proc/self/fd/{fd}").resolve(strict=True)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    final_path = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_path.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(fd)
    size = final_path(handle, None, 0, 0)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(size)
    written = final_path(handle, buffer, size, 0)
    if not written or written >= size:
        raise OSError("无法核对预览文件句柄路径")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def require_preview_boundary(root: Path, candidate: Path) -> None:
    if not candidate.is_relative_to(root) or any(part.lower() == ".git" for part in candidate.relative_to(root).parts):
        raise ValueError("文件不在任务 worktree 内，或指向 .git")


def read_preview(root: Path, relative_path: str) -> dict[str, Any]:
    path = Path(relative_path)
    windows_path = PureWindowsPath(relative_path)
    if (not relative_path or path.is_absolute() or windows_path.drive or windows_path.root
            or any(part.lower().rstrip(" .") == ".git" for part in windows_path.parts)
            or ":" in relative_path):
        raise ValueError("必须提供 worktree 内的相对文件路径，不能读取 .git")
    root = root.resolve(strict=True)
    candidate = (root / path).resolve()
    require_preview_boundary(root, candidate)
    if not candidate.exists():
        raise FileNotFoundError(relative_path)
    if not candidate.is_file():
        raise ValueError("预览目标不是普通文件")
    with candidate.open("rb") as stream:
        require_preview_boundary(root, opened_path(stream.fileno()))
        data = stream.read(PREVIEW_LIMIT + 1)
    truncated = len(data) > PREVIEW_LIMIT
    content = None
    reason = "内容包含 NUL 字节" if b"\0" in data else None
    if reason is None:
        try:
            # 截断点可能落在 UTF-8 字符中间；增量解码只忽略末尾的不完整字符。
            content = codecs.getincrementaldecoder("utf-8")().decode(data[:PREVIEW_LIMIT], final=not truncated)
        except UnicodeDecodeError:
            reason = "内容无法按 UTF-8 解码"
    return {"path": relative_path, "kind": "binary" if reason else "text", "content": content,
            "truncated": truncated, "reason": reason or ("预览超过 1 MiB，已截断" if truncated else None)}
