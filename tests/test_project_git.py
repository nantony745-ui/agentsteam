from uuid import uuid4

import pytest

from app.delivery.git_delivery import git
from app.domain.models import TaskCreate
from test_http_runs import api, setup_app, new_session, submit, terminal
from test_storage import repository


@pytest.mark.asyncio
async def test_plain_project_chat_delegation_and_explicit_git_init(api, tmp_path):
    client, app, factory = api
    path = tmp_path / "普通项目"
    path.mkdir()
    file = path / "中文 文件.txt"
    file.write_text("用户内容", encoding="utf-8")
    project = (await client.post('/api/projects', json={"path": str(path)})).json()
    url = f'/api/projects/{project["id"]}/git'
    status = (await client.get(url)).json()
    assert status["is_repo"] is False
    assert not (path / '.git').exists()
    session = await new_session(client, project)
    run = await submit(client, session, "使用普通目录")
    agent, _ = await factory.started()
    assert agent.cwd == path.resolve()
    agent.finish()
    await terminal(client, run["run_id"])
    other = tmp_path / "指定目录"
    other.mkdir()
    task = await app.state.service.delegate_task(session["id"], TaskCreate(
        harness="codex", goal="普通目录委派", acceptance="成功", request_id=str(uuid4()), working_dir=str(other)))
    worker, _ = await factory.started()
    assert worker.cwd == other.resolve()
    worker.finish()
    await terminal(client, task["run_id"])
    response = await client.post(url + '/init')
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["is_repo"] and state["branch"] and not state["has_commits"] and not state["detached"]
    assert state["changes"] == [{"status": "??", "path": file.name}]
    assert file.read_text(encoding="utf-8") == "用户内容"
    assert await git(path, 'diff', '--cached', '--name-only') == b''
    assert (await client.post(url + '/init')).json() == state


@pytest.mark.asyncio
async def test_nested_directory_head_changes_and_session_target(api, repository):
    client, app, _ = api
    child = repository / "子目录"
    child.mkdir()
    (repository / 'old.txt').write_text('old', encoding='utf-8')
    await git(repository, 'add', 'old.txt')
    await git(repository, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
              '-c', 'commit.gpgsign=false', '-c', f'core.hooksPath={repository.parent / "no-hooks"}', 'commit', '-qm', 'tracked')
    await git(repository, 'mv', 'old.txt', '新 名.txt')
    (child / '未跟踪.txt').touch()
    project = (await client.post('/api/projects', json={"path": str(child)})).json()
    url = f'/api/projects/{project["id"]}/git'
    state = (await client.get(url)).json()
    assert state['is_repo'] and state['has_commits'] and not state['detached']
    assert state['root'] == str(repository.resolve()) and state['relative_path'] == '子目录'
    assert {'status':'R ', 'path':'新 名.txt', 'original_path':'old.txt'} in state['changes']
    assert (await client.post(url + '/init')).status_code == 200
    assert not (child / '.git').exists()
    await git(repository, 'checkout', '--detach', '-q')
    state = (await client.get(url)).json()
    assert state['detached'] and state['branch'] is None and len(state['short_sha']) == 7
    session = await new_session(client, project)
    other = repository.parent / '普通会话目录'
    other.mkdir()
    async with app.state.storage.transaction() as db:
        await db.execute('UPDATE sessions SET cwd=? WHERE id=?', (str(other),session['id']))
    state = (await client.get(url, params={'session_id':session['id']})).json()
    assert not state['is_repo'] and state['path'] == str(other.resolve())
    other_project = (await client.post('/api/projects',json={'path':str(other)})).json()
    foreign = f'/api/projects/{other_project["id"]}/git/init'
    assert (await client.post(foreign,params={'session_id':session['id']})).status_code == 404
    assert not (other / '.git').exists()
    assert (await client.get(f'/api/projects/{uuid4()}/git')).status_code == 404
