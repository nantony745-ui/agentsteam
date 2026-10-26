"""只读技能发现与 HTTP 边界，仅使用临时目录及数据库。"""

from pathlib import Path
import os
import subprocess

import pytest

from app.services.harness_skills import MAX_SKILL_BYTES, skill_roots, split_document
from test_harness_settings import settings_api


def skill(root, slug, name):
    path = root / slug / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: "{name}"\ndescription: >\n  第一行\n  第二行\n---\n# {name}\n\n正文 **只读**\n', encoding="utf-8-sig")
    return path


@pytest.fixture
def skill_home(tmp_path, monkeypatch):
    for name in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(name, str(tmp_path))
    for name in ("CODEX_HOME", "DSH_HOME", "DSH_AGENTS_HOME"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("harness,relative", [("codex", ".codex/skills"), ("opencode", ".config/opencode/skills"),
                                             ("codebuddy", ".codebuddy/skills"), ("deepseek", ".dsh/skills")])
async def test_four_harnesses_read_global_skill_without_agent_or_file_writes(settings_api, skill_home, harness, relative):
    client, app, factory = settings_api
    path = skill(skill_home / relative, "sample", "示例技能")
    original = path.read_bytes(), path.stat().st_mtime_ns
    url = f"/api/settings/harnesses/{harness}/skills"
    response = await client.get(url)
    assert response.status_code == 200, response.text
    items = response.json()["skills"]
    assert len(items) == 1 and items[0]["name"] == "示例技能" and items[0]["description"] == "第一行 第二行"
    assert Path(items[0]["path"]) == path
    detail = (await client.get(f"{url}/{items[0]['id']}")).json()
    assert detail["body"].startswith("# 示例技能") and "name:" in detail["metadata"]
    assert detail["content"].startswith("---")
    assert original == (path.read_bytes(), path.stat().st_mtime_ns)
    assert not factory.clients and not await app.state.storage.list_sessions(None)
    for method in ("POST", "PUT", "DELETE"):
        assert (await client.request(method, f"{url}/{items[0]['id']}", json={})).status_code == 405


@pytest.mark.asyncio
async def test_global_preview_is_independent_of_projects(settings_api, skill_home, monkeypatch):
    client, app, _ = settings_api

    async def unexpected(*args):
        raise AssertionError("全局 Skills 不读取项目")

    monkeypatch.setattr(app.state.storage, "get_project", unexpected)
    assert (await client.get("/api/settings/harnesses/codex/skills")).json()["skills"] == []
    specification = (await client.get("/openapi.json")).json()
    for path in ("/api/settings/harnesses/{harness}/skills", "/api/settings/harnesses/{harness}/skills/{skill_id}"):
        assert all(parameter["in"] == "path" for parameter in specification["paths"][path]["get"]["parameters"])


@pytest.mark.asyncio
async def test_shared_system_flat_files_and_environment_roots(settings_api, skill_home, monkeypatch):
    client, _, _ = settings_api
    custom = skill_home / "custom"
    monkeypatch.setenv("CODEX_HOME", str(custom))
    monkeypatch.setenv("DSH_HOME", "~/deepseek")
    monkeypatch.setenv("DSH_AGENTS_HOME", str(custom))
    skill(custom / "skills/.system", "system", "系统技能")
    skill(custom / "skills", "native", "原生技能")
    skill(skill_home / ".agents/skills", "shared", "共享技能")
    (custom / "skills/flat.md").write_text("# 平铺技能", encoding="utf-8")
    (custom / "skills/ignore.txt").write_text("忽略", encoding="utf-8")
    codex = (await client.get("/api/settings/harnesses/codex/skills")).json()
    assert {item["name"] for item in codex["skills"]} == {"系统技能", "原生技能", "共享技能", "flat"}
    deepseek = (await client.get("/api/settings/harnesses/deepseek/skills")).json()
    assert {item["name"] for item in deepseek["skills"]} == {"原生技能", "flat"}
    assert Path(deepseek["locations"][0]["path"]) == skill_home / "deepseek/skills"
    assert deepseek["locations"][0]["exists"] is False
    opencode = (await client.get("/api/settings/harnesses/opencode/skills")).json()
    assert {item["name"] for item in opencode["skills"]} == {"共享技能"}
    # 共享目录按原始路径使用；DSH_HOME 继续展开 ~。
    roots = skill_roots("deepseek", {"USERPROFILE": str(skill_home), "HOME": str(skill_home),
                                    "DSH_HOME": "~/deepseek", "DSH_AGENTS_HOME": "~/shared"})
    assert roots == [skill_home / "deepseek/skills", Path("~/shared/skills")]


@pytest.mark.asyncio
async def test_unknown_id_traversal_changed_files_and_invalid_text(settings_api, skill_home):
    client, _, _ = settings_api
    root = skill_home / ".codex/skills"
    path = skill(root, "sample", "样本")
    url = "/api/settings/harnesses/codex/skills"
    item = (await client.get(url)).json()["skills"][0]
    assert (await client.get(f"{url}/unknown")).status_code == 404
    assert (await client.get("/api/settings/harnesses/unknown/skills")).status_code == 422
    path.unlink()
    assert (await client.get(f"{url}/{item['id']}")).status_code == 404
    path.write_bytes(b"\xff\xfe")
    assert (await client.get(f"{url}/{item['id']}")).status_code == 422
    assert (await client.get(url)).json()["skills"][0]["error"]
    path.write_bytes(b"a" * (MAX_SKILL_BYTES + 1))
    assert (await client.get(f"{url}/{item['id']}")).status_code == 422


@pytest.mark.asyncio
async def test_links_are_previewed_once_and_broken_links_are_ignored(settings_api, skill_home):
    client, _, _ = settings_api
    source = skill(skill_home / "source", "linked", "链接技能")
    root = skill_home / ".codex/skills"
    root.mkdir(parents=True)
    for name, target in (("first", source.parent), ("second", source.parent), ("broken", skill_home / "missing")):
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(root / name), str(target)],
                           check=True, capture_output=True)
        else:
            (root / name).symlink_to(target, target_is_directory=True)
    data = (await client.get("/api/settings/harnesses/codex/skills")).json()
    assert len(data["skills"]) == 1
    assert (await client.get(f"/api/settings/harnesses/codex/skills/{data['skills'][0]['id']}")).json()["body"].startswith("# 链接技能")


def test_plain_markdown_and_unclosed_frontmatter_remain_intact():
    assert split_document("# 无元信息\n") == ("", "# 无元信息\n")
    assert split_document("---\nname: 未结束") == ("", "---\nname: 未结束")
