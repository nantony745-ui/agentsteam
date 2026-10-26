"""Harness 技能目录的只读发现与预览，不代表运行会话已加载。"""

import asyncio
import hashlib
import os
from pathlib import Path
from app.agents.acp_client import load_env


MAX_SKILL_BYTES = 2 * 1024 * 1024


def skill_roots(harness: str, env: dict[str, str]) -> list[Path]:
    home = Path(env.get("USERPROFILE" if os.name == "nt" else "HOME") or Path.home())

    def directory(key: str, fallback: Path) -> Path:
        value = env.get(key, "").strip()
        if value == "~" or value.startswith(("~/", "~\\")):
            return home / value[2:] if len(value) > 1 else home
        return Path(value) if value else fallback

    codex = directory("CODEX_HOME", home / ".codex")
    # DeepSeek 的共享目录变量直接作为路径使用，与 DSH_HOME 的 ~ 展开规则不同。
    dsh_agents = Path(env.get("DSH_AGENTS_HOME") or home / ".agents")
    roots = {
        "codex": [codex / "skills", codex / "skills/.system", home / ".agents/skills"],
        "opencode": [home / ".config/opencode/skills", home / ".agents/skills"],
        "codebuddy": [home / ".codebuddy/skills"],
        "deepseek": [directory("DSH_HOME", home / ".dsh") / "skills",
                     dsh_agents / "skills"],
    }
    return roots[harness]


def split_document(content: str) -> tuple[str, str]:
    lines = content.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.strip() in {"---", "..."}:
                return "".join(lines[1:index]).strip(), "".join(lines[index + 1:]).lstrip()
    return "", content


def metadata_label(metadata: str, key: str) -> str:
    lines = metadata.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            value = line.split(":", 1)[1].strip()
            if value in {"|", ">", "|-", ">-", "|+", ">+"}:
                continuation = []
                for following in lines[index + 1:]:
                    if following and not following[0].isspace():
                        break
                    continuation.append(following.strip())
                value = " ".join(continuation)
            return value.strip("\"'")
    return ""


def read_document(path: Path) -> str:
    with path.open("rb") as stream:
        data = stream.read(MAX_SKILL_BYTES + 1)
    if len(data) > MAX_SKILL_BYTES:
        raise ValueError("技能文档超过 2 MiB，无法预览")
    return data.decode("utf-8-sig")


def discover(roots: list[Path], harness: str) -> tuple[list[dict], list[dict]]:
    items, locations, seen = [], [], set()
    for root in roots:
        location = {"path": str(root), "exists": root.is_dir(), "error": None}
        locations.append(location)
        try:
            entries = sorted(root.iterdir(), key=lambda entry: entry.name.casefold()) if location["exists"] else []
            for entry in entries:
                document = entry / "SKILL.md" if entry.is_dir() else entry
                if not document.is_file() or (not entry.is_dir() and (
                        harness not in {"codex", "deepseek"} or entry.suffix.lower() != ".md")):
                    continue
                resolved = document.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                items.append({"id": hashlib.sha256(str(resolved).encode()).hexdigest(),
                              "slug": entry.stem if entry.is_file() else entry.name,
                              "path": str(document), "directory": str(document.parent),
                              "source": str(root)})
        except OSError as error:
            location["error"] = f"无法读取目录：{error.strerror or error}"
    return items, locations


def describe(item: dict) -> dict:
    try:
        metadata, _ = split_document(read_document(Path(item["path"])))
        return {**item, "name": metadata_label(metadata, "name") or item["slug"],
                "description": metadata_label(metadata, "description"), "error": None}
    except (OSError, UnicodeError, ValueError) as error:
        return {**item, "name": item["slug"], "description": "", "error": str(error)}


class HarnessSkills:
    async def list(self, harness: str) -> dict:
        roots = skill_roots(harness, load_env(harness))

        def scan():
            items, locations = discover(roots, harness)
            skills = sorted((describe(item) for item in items), key=lambda item: item["name"].casefold())
            return {"harness": harness, "locations": locations, "skills": skills}

        return await asyncio.to_thread(scan)

    async def read(self, harness: str, skill_id: str) -> dict:
        roots = skill_roots(harness, load_env(harness))

        def preview():
            items, _ = discover(roots, harness)
            item = next((item for item in items if item["id"] == skill_id), None)
            if item is None:
                raise KeyError(skill_id)
            content = read_document(Path(item["path"]))
            metadata, body = split_document(content)
            return {"skill": {**item, "name": metadata_label(metadata, "name") or item["slug"]},
                    "metadata": metadata, "body": body, "content": content}

        return await asyncio.to_thread(preview)
