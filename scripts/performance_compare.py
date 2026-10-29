"""P1/P2 前后版本的隔离基准；仅生成临时数据库，不连接真实 Agent 或模型。"""

import argparse
import asyncio
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import zipfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def backend(root, directory):
    sys.path[:0] = [str(root), str(root / "tests")]
    import httpx
    from app.db.storage import Storage
    from app.domain.employees import EmployeeInput
    from app.main import create_app
    from app.memory.models import RetainInput
    from app.memory.service import MemoryService
    from app.memory.storage import flush_capture_batch
    from app.services.employees import Employees
    from test_http_runs import ControlledFactory
    from test_memory import Engine

    result = {}
    store = Storage(directory / "capture.db")
    await store.open()
    stamp = time.perf_counter()
    async with store.transaction() as db:
        for index in range(120):
            await db.execute("INSERT INTO memory_capture_turns(run_id,session_id,content,metadata) VALUES (?,?,?,?)",
                             (f"run-{index}", "session", f"{index}:" + "abcdefgh" * 1024, '{"origin":"completed_run"}'))
            await flush_capture_batch(db, "session", None, 3)
        async with db.execute("SELECT count(*),coalesce(sum(length(CAST(content AS BLOB))),0) FROM memory_capture_turns") as cursor:
            count, buffer_bytes = await cursor.fetchone()
        async with db.execute("SELECT count(*),sum(length(CAST(content AS BLOB))) FROM memory_operations") as cursor:
            operations, operation_bytes = await cursor.fetchone()
    result["capture"] = {"elapsed_ms": (time.perf_counter()-stamp)*1000, "buffer_rows": count,
                         "buffer_content_bytes": buffer_bytes, "operation_rows": operations,
                         "operation_content_bytes": operation_bytes}
    await store.close()
    result["capture"]["database_bytes_after_close"] = store.path.stat().st_size

    connection = sqlite3.connect(":memory:")
    connection.executescript((root / "app/db/schema.sql").read_text(encoding="utf-8"))
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.executemany("INSERT INTO events(project_id,session_id,kind,payload,created_at) VALUES ('p','s','agent.text',?,'2026-10-29T12:00:00Z')",
                           [(json.dumps({"text":str(index) + 'x'*256}),) for index in range(100000)])
    connection.executemany("INSERT INTO events(project_id,session_id,kind,payload,created_at) VALUES ('p','s','feedback.pulled',?,'2026-10-29T12:00:00Z')",
                           [(json.dumps({"message":str(index)}),) for index in range(10)])
    query = "SELECT payload FROM events WHERE session_id=? AND kind='feedback.pulled' AND created_at>=? AND created_at<=? ORDER BY id"
    parameters = ('s','2026-10-29T11:00:00Z','2026-10-29T13:00:00Z')
    timings = []
    for index in range(35):
        stamp = time.perf_counter()
        rows = connection.execute(query, parameters).fetchall()
        if index >= 5:
            timings.append((time.perf_counter()-stamp)*1000)
    assert len(rows) == 10
    result["feedback"] = {"query_median_ms":statistics.median(timings), "samples_ms":timings,
                          "plan":[row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query, parameters)]}
    connection.close()

    service = Employees(directory / "snapshot-employees")
    employee = service.create(EmployeeInput(name="benchmark", harness="codex"))
    package = service.directory(employee["employee_id"]) / "skills/benchmark-package"
    package.mkdir()
    (package / "SKILL.md").write_text("---\nname: benchmark-package\ndescription: fixture\n---\nfixture", encoding="utf-8")
    for index in range(128):
        (package / f"file-{index}.txt").write_bytes(bytes([65+index%26])*65536)
    settings = {"revision":1, "config_values":{}}
    ticks, stop = [], False

    async def heartbeat():
        previous = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.002)
            current = time.perf_counter()
            ticks.append((current-previous)*1000)
            previous = current

    snapshot_samples = []
    with patch("app.services.employees.load_env", return_value={"USERPROFILE":str(directory / "home")}), \
         patch("app.services.employees.skill_roots", return_value=[]):
        for mode in ("cold", "reused"):
            ticks.clear()
            task = asyncio.create_task(heartbeat())
            await asyncio.sleep(0.015)
            stamp = time.perf_counter()
            if hasattr(service, "prepare_async"):
                binding = await service.prepare_async(employee["employee_id"], str(uuid4()), settings, directory)
            else:
                binding = service.prepare(employee["employee_id"], str(uuid4()), settings, directory)
            elapsed = (time.perf_counter()-stamp)*1000
            await asyncio.sleep(0.015)
            stop = True
            await task
            # 内存跟踪与计时分开，避免 tracemalloc 放大文件操作的时延。
            tracemalloc.start()
            context = "separate-memory-sample" if mode == "cold" else ""
            if hasattr(service, "prepare_async"):
                await service.prepare_async(employee["employee_id"], str(uuid4()), settings, directory, context)
            else:
                service.prepare(employee["employee_id"], str(uuid4()), settings, directory, context)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            snapshot_samples.append({"mode":mode, "elapsed_ms":elapsed, "max_loop_gap_ms":max(ticks),
                                     "tracemalloc_peak_bytes":peak, "warnings":binding["warnings"]})
            stop = False
    result["snapshot"] = snapshot_samples

    store = Storage(directory / "concurrency.db")
    await store.open()
    memory = MemoryService(store, Engine)
    memory.config["enabled"] = True
    await memory.store.save_config(memory.config)
    source = await memory.retain(RetainInput(request_id=uuid4(), content="source"))
    engine = await memory._ready()
    entered = asyncio.Event()

    async def reflect(query, member_id):
        entered.set()
        await asyncio.sleep(0.5)
        return query

    engine.reflect = reflect
    slow = asyncio.create_task(memory.query("reflect", "slow"))
    await entered.wait()
    async def timed(operation):
        stamp = time.perf_counter()
        await operation
        return (time.perf_counter()-stamp)*1000
    recall, revise = await asyncio.gather(timed(memory.query("recall", "fast")),
                                        timed(memory.revise(source["id"], 1, "revision", False)))
    await slow
    result["memory_concurrency"] = {"slow_reflect_ms":500, "recall_ms":recall, "revise_ms":revise}
    await memory.close()
    await store.close()

    application = create_app(directory / "sidebar.db", ControlledFactory())
    async with application.router.lifespan_context(application):
        store, employees = application.state.storage, application.state.employees
        session_ids = [str(uuid4()) for _ in range(1000)]
        async with store.transaction() as db:
            await db.execute("INSERT INTO projects VALUES ('p','bench',?,'[]','now')", (str(directory),))
            await db.executemany("INSERT INTO sessions(id,project_id,harness,cwd,created_at) VALUES (?,'p','codex',?,'now')",
                                 [(identity,str(directory)) for identity in session_ids])
        for identity in session_ids:
            path = employees.root / f"bindings/{identity}/binding.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"employee_id":"bench", "name":"fixture", "warnings":[]}), encoding="utf-8")
            employees.bindings[identity] = path
        counts = {"summary_queries":0, "binding_reads":0, "wire_body_bytes":0}
        list_sessions, binding = store.list_sessions, employees.binding
        async def counted_list(project_id):
            counts["summary_queries"] += 1
            return await list_sessions(project_id)
        def counted_binding(identity):
            counts["binding_reads"] += 1
            return binding(identity)
        store.list_sessions, employees.binding = counted_list, counted_binding
        async def observed(scope, receive, send):
            async def counted_send(message):
                if message["type"] == "http.response.body":
                    counts["wire_body_bytes"] += len(message.get("body",b""))
                await send(message)
            await application(scope, receive, counted_send)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observed),base_url="http://bench") as client:
            first = await client.get("/api/sessions")
            assert len(first.json()) == 1000
            headers = {"If-None-Match": first.headers["etag"]} if "etag" in first.headers else {}
            counts.update({key:0 for key in counts})
            samples, statuses = [], []
            cpu = time.process_time()
            for _ in range(12):
                stamp = time.perf_counter()
                response = await client.get("/api/sessions", headers=headers)
                samples.append((time.perf_counter()-stamp)*1000)
                statuses.append(response.status_code)
            result["sidebar_idle"] = {**counts, "requests":12, "latency_median_ms":statistics.median(samples),
                                      "cpu_ms":(time.process_time()-cpu)*1000, "statuses":statuses}
            await store.set_session_connection(session_ids[0], "updated", 123)
            changed = await client.get("/api/sessions", headers=headers)
            assert changed.status_code == 200
            assert next(row for row in changed.json() if row["id"] == session_ids[0])["native_id"] == "updated"
            result["sidebar_idle"]["change_recovered"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", default="02e38bb")
    parser.add_argument("--after", default="156936d")
    parser.add_argument("--output", type=Path, default=Path("profile_output/p1p2-ab-20261029"))
    parser.add_argument("--worker-root", type=Path)
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args()
    if args.worker_root:
        with tempfile.TemporaryDirectory(prefix="agentsteam-ab-") as temporary:
            print(json.dumps(asyncio.run(backend(args.worker_root.resolve(), Path(temporary))), ensure_ascii=False))
        return
    repository = Path(__file__).resolve().parents[1]
    output = (repository / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    refs = {mode:subprocess.check_output(["git","rev-parse",ref],cwd=repository,text=True).strip()
            for mode,ref in (("before",args.before),("after",args.after))}
    for mode, ref in refs.items():
        archive = output / f"{mode}.zip"
        subprocess.run(["git","archive","--format=zip",f"--output={archive}",ref,"app","tests","pyproject.toml"], cwd=repository,check=True)
        with zipfile.ZipFile(archive) as saved:
            saved.extractall(output / mode)
    dump(output / "commits.json", refs)
    if args.export_only:
        return
    results = {"commits":refs,"measured_at":time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "python":sys.version,"platform":platform.platform(),"cpu":platform.processor(),
               "timing_memory_separated":True,
               "samples":{"before":[],"after":[]},"order":["before","after","after","before","before","after"]}
    for index, mode in enumerate(results["order"]):
        print(f"backend {index+1}/6: {mode}", flush=True)
        process = subprocess.run([sys.executable,str(Path(__file__).resolve()),"--worker-root",str(output / mode)],
                                 cwd=output / mode,check=True,capture_output=True,text=True,encoding="utf-8",timeout=150,
                                 env={**os.environ,"PYTHONUTF8":"1"})
        results["samples"][mode].append(json.loads(process.stdout))
        dump(output / "backend.json",results)
    print(str(output / "backend.json"))


if __name__ == "__main__":
    main()
