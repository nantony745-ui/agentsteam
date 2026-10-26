"""团队模板、项目成员快照及队长作用域；不启动 Harness。"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agents.acp_client import load_argv, load_env
from app.db.storage import RequestConflictError
from app.services.harness_skills import describe, discover


def member_profile(profile: dict) -> dict:
    summary = {key: profile[key] for key in ("employee_id", "name", "role", "instructions")}
    guidance = [summary["instructions"]]
    guidance.extend(f"{label}：\n{profile[key]}" for label, key in (
        ("专业职责", "responsibilities"), ("工作准则", "principles"), ("成长课题", "growth")
    ) if profile.get(key, "").strip())
    summary["instructions"] = "\n\n".join(item for item in guidance if item)
    if profile.get("deleted"):
        summary.update(name="队员已被删除", role="", instructions="", deleted=True)
    return summary


class Teams:
    def __init__(self, storage, employees, skills, mcp, runtimes):
        self.storage, self.employees = storage, employees
        self.skills, self.mcp = skills, mcp
        self.runtimes = runtimes

    def profiles(self, member_ids):
        profiles = [self.employees.get(employee_id) for employee_id in member_ids]
        for profile in profiles:
            if not profile["harness"]:
                raise ValueError(f"{profile['name']} 尚未配置 Harness")
            load_argv(profile["harness"])
        return profiles

    async def list(self):
        rows = await self.storage._fetch_all("SELECT * FROM teams ORDER BY created_at,id", ())
        return [{**row, "member_ids": json.loads(row["member_ids"])} for row in rows]

    async def get(self, team_id):
        row = await self.storage._fetch_one("SELECT * FROM teams WHERE id=?", (team_id,))
        if row is None:
            raise KeyError(team_id)
        return {**row, "member_ids": json.loads(row["member_ids"])}

    async def save(self, data, team_id=None):
        async with self.employees.admission:
            await asyncio.to_thread(self.profiles, data.member_ids)
            async with self.storage.transaction() as db:
                if team_id is None:
                    team_id = str(uuid4())
                    await db.execute("INSERT INTO teams VALUES (?,?,?,?,1,?)", (
                        team_id, data.name.strip(), json.dumps(data.member_ids), data.leader_id,
                        datetime.now(UTC).isoformat()))
                else:
                    async with db.execute("SELECT version FROM teams WHERE id=?", (team_id,)) as cursor:
                        current = await cursor.fetchone()
                    if current is None:
                        raise KeyError(team_id)
                    if current["version"] != data.version:
                        raise RequestConflictError("团队已更新，请刷新后重新编辑")
                    await db.execute("UPDATE teams SET name=?,member_ids=?,leader_id=?,version=version+1 WHERE id=?",
                                     (data.name.strip(), json.dumps(data.member_ids), data.leader_id, team_id))
            return await self.get(team_id)

    async def delete(self, team_id):
        async with self.storage.transaction() as db:
            async with db.execute("DELETE FROM teams WHERE id=?", (team_id,)) as cursor:
                if cursor.rowcount != 1:
                    raise KeyError(team_id)
        return {"deleted_team_id": team_id}

    async def snapshot(self, team_id):
        team = await self.get(team_id)
        return {**team, "members": await asyncio.to_thread(self.profiles, team["member_ids"])}

    async def project(self, project_id):
        row = await self.storage._fetch_one("SELECT * FROM project_teams WHERE project_id=?", (project_id,))
        return {**json.loads(row["snapshot"]), "context_mode": row["context_mode"]} if row else None

    async def association(self, session_id):
        return await self.storage._fetch_one("SELECT * FROM team_sessions WHERE session_id=?", (session_id,))

    async def capabilities(self, employee_id, cwd=None):
        profile = self.employees.read_profile(employee_id)
        if profile.get("deleted"):
            return {"profile": {"employee_id": employee_id, "name": "队员已被删除", "role": "",
                                "harness": profile["harness"], "deleted": True},
                    "skills": [], "mcp": [], "state": "deleted", "note": "队员已被删除，不能用于新的团队或项目引用。"}
        personal = await asyncio.to_thread(self.employees.skills, employee_id)
        if not profile["harness"]:
            return {"profile": profile, "skills": personal["skills"], "mcp": [], "state": "unconfigured"}
        skills, mcp = await asyncio.gather(self.skills.list(profile["harness"]), self.mcp.list(profile["harness"]))
        home = Path(load_env(profile["harness"]).get("USERPROFILE", str(Path.home())))
        roots = [home / ".claude/skills"]
        if cwd:
            roots.extend(Path(cwd) / name for name in (".agents/skills", ".claude/skills", f".{profile['harness']}/skills"))
        def project_skills():
            items, _ = discover(roots, profile["harness"])
            return [describe(item) for item in items]
        extra = await asyncio.to_thread(project_skills)
        names = set()
        available = []
        for skill in [*skills["skills"], *extra, *personal["skills"]]:
            if not skill["error"] and skill["name"] not in names:
                names.add(skill["name"])
                available.append({"name": skill["name"], "description": skill["description"]})
        return {"profile": profile, "skills": available,
                "mcp": [{key: item[key] for key in ("name", "transport", "enabled", "error")} for item in mcp["servers"]],
                "state": "discovered", "note": "目录与配置发现结果不代表当前会话已加载或连接。"}

    async def roster(self, session_id):
        session = await self.storage.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        team = await self.project(session["project_id"])
        association = await self.association(session_id)
        if not team or not association:
            raise ValueError("当前会话不属于团队")
        profiles = [self.employees.read_profile(item) for item in team["member_ids"]]
        members = [{"profile": {**member_profile(profile), "harness": profile["harness"]},
                    "state": "deleted" if profile.get("deleted") else "available"} for profile in profiles]
        async with self.storage.read_transaction() as db:
            async with db.execute(
            "SELECT s.*,ts.employee_id,ts.leader_session_id,"
            "(SELECT status FROM runs WHERE session_id=s.id ORDER BY CASE status "
            "WHEN 'waiting_permission' THEN 0 WHEN 'running' THEN 1 WHEN 'cancel_requested' THEN 2 "
            "WHEN 'queued' THEN 3 ELSE 4 END,rowid DESC LIMIT 1) AS status,"
            "(SELECT count(*) FROM runs WHERE session_id=s.id AND status='queued') AS queued_count "
            "FROM sessions s JOIN team_sessions ts ON ts.session_id=s.id "
                "WHERE ts.leader_session_id=? ORDER BY s.created_at,s.id", (association["leader_session_id"],)) as cursor:
                sessions = [dict(item) for item in await cursor.fetchall()]
            async with db.execute("SELECT coalesce(max(id),0) FROM events WHERE project_id=?", (session["project_id"],)) as cursor:
                high = (await cursor.fetchone())[0]
        titles = {item["id"]: item["title"] for item in await self.storage.list_sessions(session["project_id"])}
        for item in sessions:
            item["title"] = titles.get(item["id"], "新会话")
            runtime = self.runtimes.get(item["id"])
            binding = self.employees.binding(item["id"])
            item["capability_state"] = {
                "connection": "active" if runtime and runtime.client and runtime.client.reusable else
                              "unavailable" if item["native_id"] or (runtime and runtime.unavailable) else "not_started",
                "identity_digest": binding["digest"] if binding else None,
                "skills": "frozen_snapshot" if binding else "not_provided",
                "loaded_skills": "not_reported", "connected_mcp": "not_reported"}
        return {"team": {key: value for key, value in team.items() if key != "members"},
                "leader_session_id": association["leader_session_id"],
                "members": members, "sessions": [self.employees.decorate(item) for item in sessions], "high_watermark": high}

    async def context(self, team, employee_id):
        leader = employee_id == team["leader_id"]
        captain = self.employees.get(team["leader_id"])
        lines = ["<agentsteam_team>", f"团队：{team['name']}；队长员工 ID：{team['leader_id']}",
                 f"队长：{captain['name']}；角色：{captain['role']}",
                 "你是队长，负责分工、核验和汇总。收到执行任务后，先调用 team_members，根据成员职责分配工作。有匹配员工的实质执行任务，应通过 delegate_task 指定 employee_id 委派；" if leader else "你是团队员工，向队长交付结果，不继续委派。",
                 "使用 team_members 查询队员姓名、团队角色、工作指引与实时会话状态。",
                 f"默认任务上下文：{team['context_mode']}。continue 延续该员工在同一队长下的历史，new 单开；不继承队长聊天。",
                 "可用平台工具：team_members、check_user_feedback、memory_retain、memory_recall、memory_reflect。"]
        if leader:
            lines.append("委派前调用 team_members 核对成员与职责。队长另可调用 delegate_task、get_task_status、get_task_result、cancel_task；review 仅表示待验收。")
            for item in team["member_ids"]:
                if item != employee_id:
                    lines.append(json.dumps(member_profile(self.employees.read_profile(item)), ensure_ascii=False))
        return "\n".join([*lines, "</agentsteam_team>"])

    async def create_session(self, body):
        team = await self.project(body.project_id)
        if not team or body.employee_id not in team["member_ids"]:
            raise ValueError("员工不属于项目团队")
        leader_id = body.leader_session_id
        if body.employee_id == team["leader_id"]:
            if leader_id:
                raise ValueError("队长新会话不能指定父会话")
        else:
            parent = await self.storage.get_session(leader_id) if leader_id else None
            association = await self.association(leader_id) if parent else None
            if not parent or parent["project_id"] != body.project_id or parent["parent_id"] or not association:
                raise ValueError("请选择本项目的队长会话")
        profile = self.profiles([body.employee_id])[0]
        session_id = str(uuid4())
        project = await self.storage.get_project(body.project_id)
        settings = await self.storage.get_harness_settings(profile["harness"])
        try:
            self.employees.prepare(body.employee_id, session_id, settings, Path(project["path"]),
                                   await self.context(team, body.employee_id))
            async with self.storage.transaction() as db:
                await db.execute("INSERT INTO sessions(id,project_id,parent_id,harness,cwd,created_at) VALUES (?,?,?,?,?,?)",
                                 (session_id, body.project_id, leader_id, profile["harness"], project["path"], datetime.now(UTC).isoformat()))
                await db.execute("INSERT INTO team_sessions VALUES (?,?,?)", (session_id, leader_id or session_id, body.employee_id))
        except BaseException:
            if await self.storage.get_session(session_id) is None:
                self.employees.discard(session_id)
            raise
        return self.employees.decorate(await self.storage.get_session(session_id))
