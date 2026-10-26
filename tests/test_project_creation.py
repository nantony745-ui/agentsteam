import subprocess

import pytest

from test_http_runs import api, setup_app
from test_storage import repository


@pytest.mark.asyncio
async def test_empty_repository_creates_project_without_writing_git(api, tmp_path):
    client, app, _ = api
    path = tmp_path / "简历"
    subprocess.run(["git", "init", "--quiet", str(path)], check=True, capture_output=True)
    response = await client.post("/api/projects", json={"path": str(path)})
    assert response.status_code == 201
    assert response.json()["name"] == "简历"
    assert len(await app.state.storage.list_projects()) == 1
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"], capture_output=True)
    assert head.returncode != 0


@pytest.mark.asyncio
async def test_directory_browse_lists_server_paths_without_creating_project(api, repository):
    client, app, _ = api
    (repository / "中文 文件夹").mkdir()
    (repository / "文件.txt").write_text("content", encoding="utf-8")
    response = await client.get("/api/project-directories", params={"path": str(repository)})
    assert response.status_code == 200
    data = response.json()
    assert data["path"] == str(repository.resolve())
    assert data["parent"] == str(repository.parent.resolve())
    assert {"name": "中文 文件夹", "path": str(repository / "中文 文件夹")} in data["directories"]
    assert all(item["name"] != "文件.txt" for item in data["directories"])
    assert data["roots"]
    assert await app.state.storage.list_projects() == []
    assert (await client.get("/api/project-directories", params={"path": str(repository / "missing")})).status_code == 404
    assert (await client.get("/api/project-directories", params={"path": "relative"})).status_code == 422
    assert (await client.get("/api/project-directories", params={"path": str(repository / "文件.txt")})).status_code == 422


@pytest.mark.asyncio
async def test_plain_directory_creates_without_initializing_git(api, tmp_path):
    client, _, _ = api
    response = await client.post("/api/projects", json={"path": str(tmp_path)})
    assert response.status_code == 201
    assert not (tmp_path / ".git").exists()
