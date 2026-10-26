"""队员资料、不可变运行快照与会话关联；不写入业务数据库。"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from app.agents.acp_client import load_env
from app.db.storage import RequestConflictError
from app.services.harness_skills import describe, discover, read_document, skill_roots, split_document


ADAPTER_VERSION = 1


def filesystem_path(path: Path) -> Path:
    value = str(path.absolute())
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
    return path


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def atomic_json(path: Path, value) -> None:
    path = filesystem_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def identity_text(profile: dict, skills_directory: Path) -> str:
    fields = [("姓名", "name"), ("团队角色", "role"), ("工作指引", "instructions"), ("专业职责", "responsibilities"),
              ("工作准则", "principles"), ("成长课题", "growth")]
    lines = ["<agentsteam_employee>"]
    lines.extend(f"{label}：{profile[key].strip()}" for label, key in fields if profile.get(key, "").strip())
    lines.extend([
        f"个人技能维护目录：{skills_directory.as_posix()}",
        "技能沉淀：在当前 agent loop 中，用户要求学习或纠正做法、验证出可复用方法、发现已用技能缺漏时，"
        "主动读取并执行 agentsteam-skill-stewardship；通过当前 Harness 的技能工具读取，"
        "未提供时读取个人技能维护目录下的 agentsteam-skill-stewardship/SKILL.md。",
        "沉淀写回个人技能维护目录；runtime 内为当前会话的冻结快照。新会话加载维护源的新版本，"
        "当前会话需要复用新经验时直接读取维护源。",
    ])
    return "\n".join([*lines, "</agentsteam_employee>"])


class Employees:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.admission = asyncio.Lock()
        self.bindings: dict[str, Path] = {}
        for path in self.root.glob("*/profile.json"):
            if not self.read_profile(path.parent.name).get("deleted"):
                self._initialize_skills(path.parent.name)
        for path in self.root.glob("*/sessions/*/binding.json"):
            self._inside(path)
            self.bindings[str(UUID(path.parent.name))] = path

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        value = str(resolved)
        if value.startswith("\\\\?\\UNC\\"):
            resolved = Path("\\\\" + value[8:])
        elif value.startswith("\\\\?\\"):
            resolved = Path(value[4:])
        if not resolved.is_relative_to(self.root):
            raise ValueError("路径超出队员目录")
        return resolved

    def directory(self, employee_id: str) -> Path:
        return self._inside(self.root / str(UUID(employee_id)))

    def _initialize_skills(self, employee_id: str) -> None:
        source = Path(__file__).resolve().parents[1] / "skills/en/agentsteam-skill-stewardship"
        destination = self._inside(self.directory(employee_id) / "skills" / source.name)
        if destination.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".seed-", dir=destination.parent))
        try:
            shutil.copytree(source, temporary, dirs_exist_ok=True)
            os.replace(filesystem_path(temporary), filesystem_path(destination))
        finally:
            if temporary.exists():
                shutil.rmtree(filesystem_path(temporary))

    def read_profile(self, employee_id: str) -> dict:
        path = self.directory(employee_id) / "profile.json"
        self._inside(path)
        if not path.is_file():
            raise KeyError(employee_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def get(self, employee_id: str) -> dict:
        profile = self.read_profile(employee_id)
        if profile.get("deleted"):
            raise RequestConflictError(f"队员“{profile['name']}”已被删除")
        return profile

    def list(self) -> list[dict]:
        profiles = (self.read_profile(path.parent.name) for path in self.root.glob("*/profile.json"))
        return sorted((profile for profile in profiles if not profile.get("deleted")),
                      key=lambda item: (item["name"].casefold(), item["employee_id"]))

    def delete(self, employee_id: str, version: int) -> dict:
        profile = self.read_profile(employee_id)
        if not profile.get("deleted"):
            if profile["version"] != version:
                raise RequestConflictError("队员资料已更新，请刷新后重新删除")
            atomic_json(self.directory(employee_id) / "profile.json",
                        {**profile, "deleted": True, "version": version + 1})
        return {"deleted_employee_id": employee_id}

    def create(self, data) -> dict:
        employee_id = str(uuid4())
        profile = {**data.model_dump(), "name": data.name.strip(), "employee_id": employee_id, "version": 1}
        directory = self.directory(employee_id)
        (directory / "skills").mkdir(parents=True)
        self._initialize_skills(employee_id)
        atomic_json(directory / "profile.json", profile)
        return profile

    def update(self, employee_id: str, data) -> dict:
        current = self.get(employee_id)
        if current["version"] != data.version:
            raise RequestConflictError("队员资料已更新，请刷新后重新编辑")
        profile = {**data.model_dump(), "name": data.name.strip(), "employee_id": employee_id,
                   "version": current["version"] + 1}
        atomic_json(self.directory(employee_id) / "profile.json", profile)
        return profile

    def skills(self, employee_id: str) -> dict:
        profile = self.get(employee_id)
        root = self._inside(self.directory(employee_id) / "skills")
        items, locations = discover([root], profile["harness"] or "codex")
        skills = []
        for item in items:
            try:
                path = self._inside(Path(item["path"]))
                if not path.is_relative_to(root):
                    raise ValueError("技能路径超出个人 Skills 目录")
                skill = describe(item)
                if Path(item["path"]).name != "SKILL.md":
                    skill["error"] = "个人技能必须使用 <skill-name>/SKILL.md 目录结构"
                if not skill["description"]:
                    skill["error"] = skill["error"] or "缺少技能 description"
                if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill["name"]) or len(skill["name"]) > 64:
                    skill["error"] = skill["error"] or "技能 name 必须是最长 64 字符的小写字母、数字和连字符"
                skills.append(skill)
            except ValueError as error:
                skills.append({**item, "name": item["slug"], "description": "", "error": str(error)})
        return {"directory": str(root), "skills": skills, "locations": locations}

    def read_skill(self, employee_id: str, skill_id: str) -> dict:
        scan = self.skills(employee_id)
        item = next((item for item in scan["skills"] if item["id"] == skill_id), None)
        if item is None:
            raise KeyError(skill_id)
        if item["error"]:
            raise ValueError(item["error"])
        content = read_document(self._inside(Path(item["path"])))
        metadata, body = split_document(content)
        return {"skill": item, "content": content, "metadata": metadata, "body": body}

    def _collect_skills(self, profile: dict, cwd: Path) -> tuple[dict, list]:
        harness = profile["harness"]
        source = self.skills(profile["employee_id"])
        home = Path(load_env(harness).get("USERPROFILE", str(Path.home())))
        global_items, _ = discover(skill_roots(harness, load_env(harness)) + [
            home / ".claude/skills", cwd / ".agents/skills", cwd / ".claude/skills",
            cwd / f".{harness}/skills"], harness)
        names = {describe(item)["name"] for item in global_items}
        files, warnings = {}, []
        for item in source["skills"]:
            if item["error"] or item["name"] in names:
                warnings.append({"name": item["name"], "error": item["error"] or "与既有技能重名，已排除"})
                continue
            names.add(item["name"])
            package = Path(item["directory"])
            try:
                package_files = {}
                for path in sorted(filesystem_path(package).rglob("*")):
                    resolved = self._inside(path)
                    if path.is_symlink() or path.is_junction() or not resolved.is_relative_to(package.resolve()):
                        raise ValueError("技能包包含链接或越界文件")
                    if path.is_file():
                        package_files[f"{item['slug']}/{resolved.relative_to(package).as_posix()}"] = path.read_bytes()
                files.update(package_files)
            except (OSError, ValueError) as error:
                warnings.append({"name": item["name"], "error": str(error)})
        return files, warnings

    def _snapshot(self, profile: dict, files: dict, team_context: str = "") -> tuple[Path, str]:
        identity = identity_text(profile, self.directory(profile["employee_id"]) / "skills")
        if team_context:
            identity += "\n\n" + team_context
        adapter = (Path(__file__).resolve().parents[1] / "agents/employee_native.cjs").read_bytes()
        digest = hashlib.sha256(canonical({"identity": identity, "adapter": ADAPTER_VERSION,
            "adapter_sha256": hashlib.sha256(adapter).hexdigest(),
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}})).hexdigest()
        runtime = self._inside(self.directory(profile["employee_id"]) / "runtime" / digest)
        if not runtime.exists():
            runtime.parent.mkdir(exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=runtime.parent))
            try:
                (temporary / "identity.md").write_text(identity, encoding="utf-8", newline="\n")
                (temporary / "native-adapter.cjs").write_bytes(adapter)
                (temporary / "skills").mkdir()
                for name, data in files.items():
                    destination = filesystem_path(temporary / "skills" / name)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                (temporary / ".codebuddy-plugin").mkdir()
                atomic_json(temporary / ".codebuddy-plugin/plugin.json", {"name": "agentsteam-employee", "version": "1.0.0"})
                atomic_json(temporary / "manifest.json", {"digest": digest, "adapter_version": ADAPTER_VERSION,
                    "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}})
                os.replace(filesystem_path(temporary), filesystem_path(runtime))
            finally:
                if temporary.exists():
                    shutil.rmtree(filesystem_path(temporary))
        return runtime, digest

    def prepare(self, employee_id: str, session_id: str, settings: dict, cwd: Path, team_context: str = "") -> dict:
        profile = self.get(employee_id)
        harness = profile["harness"]
        if harness is None:
            raise ValueError("请先配置队员的 Harness")
        config = {**settings, "config_values": {**settings["config_values"], **profile["config_values"]}}
        files, warnings = self._collect_skills(profile, cwd)
        runtime, digest = self._snapshot(profile, files, team_context)
        binding = {"session_id": str(UUID(session_id)), "employee_id": employee_id, "name": profile["name"],
                   "profile": profile, "settings": config, "harness": harness, "digest": digest,
                   "runtime": str(runtime), "identity_file": str(runtime / "identity.md"),
                   "skills_path": str(runtime / "skills"), "adapter_version": ADAPTER_VERSION, "warnings": warnings}
        path = self.directory(employee_id) / "sessions" / session_id / "binding.json"
        atomic_json(path, binding)
        self.bindings[session_id] = path
        return binding

    def binding(self, session_id: str) -> dict | None:
        path = self.bindings.get(session_id)
        return json.loads(self._inside(path).read_text(encoding="utf-8")) if path else None

    def discard(self, session_id: str) -> None:
        path = self.bindings.pop(session_id, None)
        if path:
            shutil.rmtree(self._inside(path.parent))

    def decorate(self, session: dict) -> dict:
        binding = self.binding(session["id"])
        return {**session, "employee_id": binding["employee_id"] if binding else None,
                "employee_name": binding["name"] if binding else None,
                "employee_warnings": binding["warnings"] if binding else []}

    def record_run(self, binding: dict, run_id: str) -> None:
        path = self.bindings[binding["session_id"]].parent / "runs" / f"{UUID(run_id)}.json"
        if not path.exists():
            atomic_json(path, {"run_id": run_id, "employee_id": binding["employee_id"],
                "digest": binding["digest"], "settings": binding["settings"],
                "identity_file": binding["identity_file"], "skills_path": binding["skills_path"],
                "adapter_version": binding["adapter_version"]})
