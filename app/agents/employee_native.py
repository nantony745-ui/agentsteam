"""四种 Harness 的进程局部原生入口；不通过用户消息降级。"""

import asyncio
import json
import os
from pathlib import Path

from app.services.employees import ADAPTER_VERSION, atomic_json, filesystem_path
from app.services.harness_skills import describe, discover


VERSIONS = {"codex": ("@agentclientprotocol/codex-acp", "2.0.1", "dist/index.js"),
            "deepseek": ("deepseek-acp", "0.9.0", "lib/bin.js"),
            "codebuddy": ("@tencent-ai/codebuddy-code", "2.167.0", "bin/codebuddy"),
            "opencode": ("opencode-ai", "1.19.0", "bin/opencode.exe")}


def package_root(argv: list[str], harness: str) -> Path:
    name, version, entry = VERSIONS[harness]
    candidate = Path(argv[0] if harness == "opencode" else argv[1]).resolve()
    for root in candidate.parents:
        manifest = root / "package.json"
        if manifest.is_file():
            package = json.loads(manifest.read_text(encoding="utf-8"))
            if package.get("name") == name:
                if package.get("version") != version or candidate != (root / entry).resolve():
                    raise ValueError(f"{harness} 队员适配要求 {name}@{version} 的指定入口")
                return root
    raise ValueError(f"找不到 {harness} 队员适配所需的包入口")


def check_conflicts(binding: dict, roots: list[Path], harness: str) -> None:
    personal, _ = discover([Path(binding["skills_path"])], harness)
    existing, _ = discover(roots, harness)
    names = {describe(item)["name"] for item in existing}
    conflicts = sorted({describe(item)["name"] for item in personal} & names)
    if conflicts:
        raise ValueError("个人技能与原生配置中的技能重名，请改名后新建会话：" + "、".join(conflicts))


async def configure_employee(client, binding: dict, cwd: Path) -> None:
    if binding["adapter_version"] != ADAPTER_VERSION:
        raise ValueError("队员会话适配版本不兼容，请新建会话")
    harness = client.harness
    root = package_root(client.argv, harness)
    identity = filesystem_path(Path(binding["identity_file"])).read_text(encoding="utf-8")
    if harness in {"codex", "deepseek"}:
        target = root / ("lib/launcher/boot.js" if harness == "deepseek" else VERSIONS[harness][2])
        spec = {"harness": harness, "target": str(target), "identity": identity,
                "skills_path": binding["skills_path"]}
        destination = Path(binding["runtime"]) / f"{harness}-adapter.json"
        atomic_json(destination, spec)
        client.env["AGENTSTEAM_EMPLOYEE_SPEC"] = str(destination)
        client.argv[1:1] = ["--require", str(Path(binding["runtime"]) / "native-adapter.cjs")]
    elif harness == "codebuddy":
        flag = "--append-system-prompt"
        if flag in client.argv:
            index = client.argv.index(flag)
            client.argv[index + 1] += "\n\n" + identity
        else:
            client.argv.extend([flag, identity])
        directories = [item for item in client.env.get("CODEBUDDY_PLUGIN_DIRS", "").split(os.pathsep) if item]
        while "--plugin-dir" in client.argv:
            index = client.argv.index("--plugin-dir")
            end = index + 1
            while end < len(client.argv) and not client.argv[end].startswith("--"):
                directories.append(client.argv[end])
                end += 1
            del client.argv[index:end]
        directories = list(dict.fromkeys(directories))
        check_conflicts(binding, [(Path(item) if Path(item).is_absolute() else cwd / item) / "skills"
                                  for item in directories], harness)
        client.argv.extend(["--plugin-dir", *directories, binding["runtime"]])
    elif harness == "opencode":
        process = await asyncio.create_subprocess_exec(client.argv[0], "debug", "config",
            cwd=str(cwd), env=client.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000 if os.name == "nt" else 0)
        try:
            async with asyncio.timeout(30):
                output, _ = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode:
            raise ValueError("无法读取 OpenCode 有效配置，未启动队员会话")
        effective = json.loads(output)
        roots = [Path(value).expanduser() for value in effective.get("skills", {}).get("paths", [])]
        check_conflicts(binding, [root if root.is_absolute() else cwd / root for root in roots], harness)
        inline = json.loads(client.env.get("OPENCODE_CONFIG_CONTENT", "{}"))
        inline["instructions"] = list(dict.fromkeys([*effective.get("instructions", []), binding["identity_file"]]))
        inline["skills"] = {**effective.get("skills", {}), "paths": list(dict.fromkeys([
            *effective.get("skills", {}).get("paths", []), binding["skills_path"]]))}
        client.env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline, ensure_ascii=False, sort_keys=True)
