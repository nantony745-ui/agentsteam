"""参照 Codeg folders.rs::resolve_git_head、git_init、git_status。"""

from pathlib import Path

from app.delivery.git_delivery import git, git_output, is_git_worktree


async def project_git_status(path: Path) -> dict:
    result = {"path": str(path), "is_repo": False, "root": None, "relative_path": None,
              "branch": None, "detached": False, "has_commits": False, "short_sha": None, "changes": []}
    if not await is_git_worktree(path):
        return result
    root = Path((await git(path, "rev-parse", "--show-toplevel")).decode("utf-8").strip()).resolve()
    branch, _, branch_code = await git_output(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    sha, _, sha_code = await git_output(path, "rev-parse", "--verify", "HEAD^{commit}")
    raw = await git(path, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    fields = iter(raw.decode("utf-8").split("\0"))
    changes = []
    for field in fields:
        if not field:
            continue
        change = {"status": field[:2], "path": field[3:]}
        if "R" in field[:2] or "C" in field[:2]:
            change["original_path"] = next(fields)
        changes.append(change)
    return {**result, "is_repo": True, "root": str(root), "relative_path": path.relative_to(root).as_posix(),
            "branch": branch.decode("utf-8").strip() if branch_code == 0 else None,
            "detached": branch_code != 0 and sha_code == 0, "has_commits": sha_code == 0,
            "short_sha": sha.decode("ascii").strip()[:7] if sha_code == 0 else None, "changes": changes}


async def initialize_project_git(path: Path) -> dict:
    status = await project_git_status(path)
    if not status["is_repo"]:
        await git(path, "init")
    return await project_git_status(path)
