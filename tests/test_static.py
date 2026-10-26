"""2.6：静态工作台资源、API 不被遮挡及工程文件不能被静态访问。"""

import re

import httpx
import pytest

from app.main import create_app


@pytest.mark.asyncio
async def test_workbench_static_boundary(tmp_path):
    application = create_app(tmp_path / "static.db")
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            index = await client.get("/")
            assert index.status_code == 200
            assert 'lang="zh-CN"' in index.text
            for path, mime in (
                ("/style.css", "text/css"),
                ("/app.js", "javascript"),
                ("/layout.js", "javascript"),
                ("/settings.js", "javascript"),
                ("/skills.js", "javascript"),
                ("/settings-layout.js", "javascript"),
                ("/harness-preview.js", "javascript"),
                ("/mcp.js", "javascript"),
                ("/vendor/markdown-it.umd.min.js", "javascript"),
            ):
                response = await client.get(path)
                assert response.status_code == 200
                assert mime in response.headers["content-type"]
            assert (await client.get("/vendor/markdown-it.LICENSE")).status_code == 200
            assert index.text.index('/vendor/markdown-it.umd.min.js') < index.text.index('/app.js')
            resources = re.findall(r'(?:src|href)="(/[^"\s]+)"', index.text)
            for path in ('/app.js', '/style.css'):
                versioned, = [resource for resource in resources if resource.startswith(path + '?v=')]
                assert versioned.split('?v=')[1]
                response = await client.get(versioned)
                assert response.status_code == 200
                assert response.content == (await client.get(path)).content
            assert 'id="left-resizer"' in index.text
            assert 'id="right-resizer"' in index.text
            assert 'id="check-argv"' not in index.text
            assert 'id="project-name"' not in index.text
            assert 'id="project-list"' in index.text
            assert '<dialog id="project-dialog"' in index.text
            assert 'id="project-details"' not in index.text
            assert 'aria-labelledby="project-dialog-title"' in index.text
            assert 'aria-label="最近主会话"' in index.text
            assert index.text.index('id="harness"') > index.text.index('id="message-form"')
            assert index.text.index('id="project-select"') > index.text.index('id="message-form"')
            assert '<option value="codebuddy">CodeBuddy</option>' in index.text
            assert 'id="open-settings"' in index.text
            assert 'id="settings-view"' in index.text
            assert 'id="harness-settings-form"' in index.text
            assert 'id="settings-skills-panel"' in index.text
            assert 'Agent SDK管理' in index.text
            assert '统一管理 Agent 的连接SDK、启用状态、环境变量、配置管理与版本预检信息。' in index.text
            assert 'id="skill-markdown"' in index.text
            assert 'id="settings-harness-resizer"' in index.text
            assert 'id="settings-skills-resizer"' in index.text
            assert 'id="settings-mcp-nav"' in index.text
            assert 'id="settings-mcp-panel"' in index.text
            assert 'id="mcp-config"' in index.text
            assert 'id="settings-mcp-resizer"' in index.text
            assert (await client.get("/api/projects")).json() == []
            for path in ("/agents.toml", "/pyproject.toml", "/data/lab.db", "/main.py"):
                assert (await client.get(path)).status_code == 404
