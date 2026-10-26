import argparse
import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acp import Client
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse
from app.agents.acp_client import AcpClient, error_details, load_argv
from app.domain.models import Harness


class Evidence:
    def __init__(self, name: str, root: Path) -> None:
        self.name = name
        self.started = datetime.now(timezone(timedelta(hours=8)))
        self.rows: list[dict[str, Any]] = []
        directory = root / ".runtime/acp-runs"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{self.started:%Y%m%dT%H%M%S%f}-{name}.jsonl"
        self.stream = self.path.open("x", encoding="utf-8")

    def record(self, kind: str, data: dict[str, Any]) -> None:
        row = {"time": datetime.now(timezone.utc).isoformat(), "kind": kind, **data}
        self.rows.append(row)
        self.stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.stream.flush()
        if kind not in {"wire_incoming", "wire_outgoing", "update", "session", "initialize",
                        "config_selected", "stderr", "closed"}:
            print(json.dumps(row, ensure_ascii=False), file=sys.stderr, flush=True)

    def observe(self, event: Any) -> None:
        self.record(f"wire_{event.direction.value}", {"message": event.message})


async def terminal_line() -> str:
    print("输入选项序号；回车取消：", file=sys.stderr, flush=True)
    if os.name == "nt" and sys.stdin.isatty():
        import msvcrt
        characters: list[str] = []
        try:
            while True:
                if not msvcrt.kbhit():
                    await asyncio.sleep(0.05)
                    continue
                char = msvcrt.getwch()
                if char in {"\r", "\n", "\x1a", "\x03"}:
                    print(file=sys.stderr, flush=True)
                    return "" if char in {"\x1a", "\x03"} else "".join(characters)
                if char == "\b":
                    if characters:
                        characters.pop()
                        print("\b \b", file=sys.stderr, end="", flush=True)
                elif char in {"\x00", "\xe0"}:
                    msvcrt.getwch()
                else:
                    characters.append(char)
                    print(char, file=sys.stderr, end="", flush=True)
        finally:
            while msvcrt.kbhit():
                msvcrt.getwch()
    descriptor = sys.stdin.fileno()
    blocking = os.get_blocking(descriptor)
    os.set_blocking(descriptor, False)
    buffer = bytearray()
    try:
        while True:
            try:
                chunk = os.read(descriptor, 1)
            except BlockingIOError:
                await asyncio.sleep(0.05)
                continue
            if not chunk or chunk == b"\n":
                return buffer.decode("utf-8", "replace").strip()
            buffer.extend(chunk)
    finally:
        os.set_blocking(descriptor, blocking)


class Host(Client):
    def __init__(self, evidence: Evidence, strict: bool) -> None:
        self.evidence = evidence
        self.strict = strict
        self.abort = asyncio.Event()
        self.pending: dict[str, asyncio.Event] = {}
        self._input_lock = asyncio.Lock()

    def expire_permissions(self, reason: str) -> None:
        for request_id, expired in self.pending.items():
            if not expired.is_set():
                self.evidence.record("permission_expired", {"request_id": request_id, "reason": reason})
                expired.set()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        payload = update.model_dump(mode="json", by_alias=True)
        self.evidence.record("update", {
            "session_id": session_id, "python_type": type(update).__name__, "update": payload,
        })
        if self.strict and payload.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
            self.abort.set()

    async def request_permission(self, session_id: str, tool_call: Any,
                                 options: list[Any], **kwargs: Any) -> RequestPermissionResponse:
        request_id = str(uuid4())
        expired = asyncio.Event()
        self.pending[request_id] = expired
        self.evidence.record("permission_request", {
            "request_id": request_id, "session_id": session_id,
            "tool_call": tool_call.model_dump(mode="json", by_alias=True),
            "options": [option.model_dump(mode="json", by_alias=True) for option in options],
            "meta": kwargs,
        })
        try:
            if self.strict:
                self.abort.set()
                response = RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
            else:
                response = await self._choose_permission(request_id, options, expired)
            self.evidence.record("permission_response", {
                "request_id": request_id, **response.model_dump(mode="json", by_alias=True),
            })
            return response
        except asyncio.CancelledError:
            self.evidence.record("permission_handler_cancelled", {"request_id": request_id})
            raise
        finally:
            self.pending.pop(request_id, None)

    async def _choose_permission(self, request_id: str, options: list[Any],
                                 expired: asyncio.Event) -> RequestPermissionResponse:
        async with self._input_lock:
            while not expired.is_set():
                for index, option in enumerate(options):
                    print(f"{index}: {option.name} | {option.kind} | {option.option_id}",
                          file=sys.stderr, flush=True)
                choice = await self._read_or_expire(expired)
                if choice is None or not choice.strip() or expired.is_set():
                    break
                if not choice.strip().isdecimal() or int(choice) >= len(options):
                    self.evidence.record("permission_invalid_input", {"request_id": request_id})
                    continue
                selected = options[int(choice)]
                self.evidence.record("permission_selected", {
                    "request_id": request_id, "option_id": selected.option_id,
                    "name": selected.name, "option_kind": selected.kind,
                })
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", option_id=selected.option_id),
                )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def _read_or_expire(self, expired: asyncio.Event) -> str | None:
        reader = asyncio.create_task(terminal_line())
        waiter = asyncio.create_task(expired.wait())
        try:
            done, _ = await asyncio.wait([reader, waiter], return_when=asyncio.FIRST_COMPLETED)
            return None if waiter in done else reader.result()
        finally:
            for task in (reader, waiter):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, waiter, return_exceptions=True)


def git_snapshot(cwd: Path) -> dict[str, str]:
    commands = {"head": ["rev-parse", "HEAD"],
                "status": ["status", "--porcelain=v1", "--untracked-files=all"],
                "diff": ["diff", "--no-ext-diff", "HEAD", "--"]}
    return {key: subprocess.check_output(
        ["git", "-C", str(cwd), *args], encoding="utf-8",
    ) for key, args in commands.items()}


def probe_snapshot(cwd: Path) -> dict[str, Any]:
    path = cwd / "acp_permission_probe.txt"
    return {"path": str(path), "exists": path.exists(),
            "content": path.read_text(encoding="utf-8") if path.is_file() else None}


def package_versions(name: str) -> dict[str, str]:
    argv = load_argv(name)
    entry = Path(argv[1] if Path(argv[0]).name.lower() == "node.exe" else argv[0])
    manifest = next(parent / "package.json" for parent in entry.parents
                    if (parent / "package.json").is_file())
    package = json.loads(manifest.read_text(encoding="utf-8"))
    return {"python": sys.version.split()[0],
            "agent-client-protocol": importlib.metadata.version("agent-client-protocol"),
            package["name"]: package["version"]}


def make_client(name: str, evidence: Evidence, strict: bool) -> tuple[AcpClient, Host]:
    host = Host(evidence, strict)
    return AcpClient(name, host, evidence.record, evidence.observe, host.expire_permissions), host


async def open_client(client: AcpClient, cwd: Path, model: str | None) -> None:
    await client.connect(cwd, [])
    if model is not None:
        await client.configure("model", model)


async def strict_smoke(client: AcpClient, host: Host) -> None:
    prompt = asyncio.create_task(client.prompt("只回复 ACP_OK，不修改文件。"))
    forbidden = asyncio.create_task(host.abort.wait())
    try:
        done, _ = await asyncio.wait([prompt, forbidden], return_when=asyncio.FIRST_COMPLETED)
        if forbidden in done:
            await client.cancel()
            raise RuntimeError("观察到工具调用或权限请求，停止无工具验收")
        await prompt
    finally:
        forbidden.cancel()
        if not prompt.done():
            await client.close()
        await asyncio.gather(prompt, forbidden, return_exceptions=True)


async def start_readonly(client: AcpClient, delay: float) -> asyncio.Task[Any]:
    prompt = asyncio.create_task(client.prompt(
        "只读分析本演示仓库：逐个读取已跟踪源码、测试和 README，解释每个函数，"
        "再逐条列出至少 80 个具体审查点及依据。不要修改文件、不要联网、不要委派。",
    ))
    try:
        await asyncio.sleep(delay)
    except BaseException:
        await client.close()
        await asyncio.gather(prompt, return_exceptions=True)
        raise
    if prompt.done():
        await prompt
        raise RuntimeError("只读任务已在实验前结束，本次不能验收取消或异常中断")
    return prompt


async def followup(client: AcpClient, cwd: Path, model: str | None,
                   evidence: Evidence, original_id: str) -> None:
    replacement = None
    if not client.reusable:
        await client.close()
        replacement, _ = make_client(client.harness, evidence, False)
    try:
        if replacement is not None:
            await open_client(replacement, cwd, model)
            client = replacement
        evidence.record("followup_start", {"native_id": client.session_id,
                                           "same_session": client.session_id == original_id})
        marker = len(evidence.rows)
        response = await client.prompt("只回复 CANCEL_OK，不使用工具，不修改文件。")
        text = message_text(evidence.rows[marker:])
        evidence.record("followup_result", {
            "native_id": client.session_id, "text": text, "stop_reason": response.stop_reason,
            "same_session": client.session_id == original_id,
            "passed": text.strip() == "CANCEL_OK" and response.stop_reason == "end_turn",
        })
    finally:
        if replacement is not None:
            await replacement.close()


async def cancel_experiment(client: AcpClient, cwd: Path, model: str | None,
                            evidence: Evidence, delay: float) -> None:
    prompt = await start_readonly(client, delay)
    original_id = client.session_id
    try:
        await client.cancel()
    except TimeoutError as error:
        evidence.record("forced_cancel_observed", {"error": repr(error), "reusable": client.reusable})
    finally:
        await asyncio.gather(prompt, return_exceptions=True)
    await followup(client, cwd, model, evidence, original_id)


async def process_exit_experiment(client: AcpClient, evidence: Evidence, delay: float) -> None:
    prompt = await start_readonly(client, delay)
    try:
        await client.terminate_owned_process("process_exit_experiment")
        try:
            await asyncio.wait_for(asyncio.shield(prompt), 5)
        except TimeoutError as error:
            raise RuntimeError("进程树终止后，prompt 五秒内仍未报告退出错误") from error
        except Exception as error:
            evidence.record("expected_process_error", {"error": repr(error),
                                                       "reusable": client.reusable})
        else:
            raise RuntimeError("进程被终止后 prompt 未报告异常")
    finally:
        await client.close()
        await asyncio.gather(prompt, return_exceptions=True)


async def conversation(args: argparse.Namespace, cwd: Path, evidence: Evidence) -> None:
    client, host = make_client(args.harness, evidence, args.scenario in {"smoke", "handshake"})
    try:
        if args.scenario == "bad-executable":
            client.argv = [str(cwd / f"missing-acp-{uuid4()}.exe")]
            evidence.record("experimental_argv", {"argv": client.argv, "config_unchanged": True})
            try:
                await client.connect(cwd, [])
            except FileNotFoundError as error:
                evidence.record("expected_spawn_error", {"error": repr(error)})
                return
            raise RuntimeError("错误 executable 意外启动成功")
        await open_client(client, cwd, args.model)
        if args.scenario == "handshake":
            return
        if args.scenario == "smoke":
            await strict_smoke(client, host)
        elif args.scenario == "permission":
            if args.harness == "codex":
                await client.configure("mode", "read-only")
            evidence.record("permission_probe_started", {"native_id": client.session_id})
            await client.prompt(
                "在当前演示仓库根目录创建 acp_permission_probe.txt，内容为 ACP_PERMISSION_OK。"
                "只创建这个文件；文件存在则不要覆盖。需要审批时请求原生权限；"
                "如果用户拒绝或取消审批，立即停止本次任务，不要换工具重试，也不要创建其他文件。",
            )
        elif args.scenario == "cancel":
            await cancel_experiment(client, cwd, args.model, evidence, args.cancel_after)
        else:
            await process_exit_experiment(client, evidence, args.cancel_after)
    finally:
        original = sys.exception()
        try:
            await client.close()
        except Exception as cleanup:
            if original is None:
                raise
            original.add_note(f"关闭失败: {cleanup!r}")


def message_text(rows: list[dict[str, Any]]) -> str:
    return "".join(row["update"].get("content", {}).get("text", "") for row in rows
                   if row["kind"] == "update" and row["update"].get("sessionUpdate") == "agent_message_chunk")


def summarize(evidence: Evidence, scenario: str, before: dict[str, str],
              after: dict[str, str], failure: str) -> dict[str, Any]:
    rows = evidence.rows
    of = lambda kind: [row for row in rows if row["kind"] == kind]
    sessions = [row["native_id"] for row in of("session")]
    responses = of("prompt_response")
    selections = of("permission_selected")
    cancel = of("cancel_result")
    followups = of("followup_result")
    unchanged = before == after
    healthy = not failure and not of("cleanup_error")
    passed = False
    if scenario == "handshake":
        passed = (healthy and bool(of("initialize")) and bool(sessions) and unchanged
                  and not of("prompt_input") and not responses and not of("permission_request"))
    elif scenario == "smoke":
        tools = any(row["update"].get("sessionUpdate") in {"tool_call", "tool_call_update"}
                    for row in of("update"))
        passed = (healthy and bool(sessions) and message_text(rows).strip() == "ACP_OK"
                  and bool(responses) and responses[-1].get("stopReason") == "end_turn"
                  and unchanged and not tools and not of("permission_request"))
    elif scenario == "permission":
        probe = of("probe_after")
        decision = selections[-1]["option_kind"] if selections else None
        disk_matches = bool(probe) and (
            probe[-1]["exists"] and probe[-1]["content"] == "ACP_PERMISSION_OK"
            if decision in {"allow_once", "allow_always"} else not probe[-1]["exists"]
        )
        status_without_probe = lambda snapshot: sorted(
            line for line in snapshot["status"].splitlines() if line != "?? acp_permission_probe.txt")
        only_probe_changed = (before["head"] == after["head"] and before["diff"] == after["diff"]
                              and status_without_probe(before) == status_without_probe(after))
        passed = (healthy and bool(of("permission_request")) and bool(selections)
                  and bool(responses) and disk_matches and only_probe_changed)
    elif scenario == "cancel":
        normal = bool(cancel) and cancel[-1]["outcome"] == "cancelled"
        forced = bool(of("forced_cancel_observed"))
        passed = (healthy and unchanged and bool(followups) and followups[-1]["passed"]
                  and ((normal and followups[-1]["same_session"])
                       or (forced and not followups[-1]["same_session"])))
    elif scenario == "bad-executable":
        passed = healthy and unchanged and bool(of("expected_spawn_error")) and not of("spawn")
    elif scenario == "process-exit":
        passed = (healthy and unchanged and bool(of("expected_process_error"))
                  and bool(of("force_terminate")) and bool(of("process_exit")))
    permission_status = ("已触发，选择待人工完成" if of("permission_request") else
                         "本次未触发" if of("permission_probe_started") else "未进行权限探测")
    return {"passed": passed, "scenario": scenario, "native_ids": sessions,
            "git_unchanged": unchanged, "error": failure,
            "permission_status": "已有人工作出原生选项选择" if selections else permission_status,
            "cancel_outcome": cancel[-1]["outcome"] if cancel else
                              "forced" if of("forced_cancel_observed") else "未进行/未完成",
            "followup": followups[-1] if followups else None}


def write_observations(root: Path, evidence: Evidence, summary: dict[str, Any]) -> None:
    scenario = summary["scenario"]
    path = root / ".runtime" / ("acp-observations.md" if scenario in {"smoke", "permission", "handshake"} else "acp-lifecycle.md")
    rows = evidence.rows
    metadata = {**summary, "versions": next((r for r in rows if r["kind"] == "versions"), {}),
                "command": next(r["command"] for r in rows if r["kind"] == "experiment"),
                "session_models": [
                    {"native_id": row["native_id"], "model": option["currentValue"]}
                    for row in rows if row["kind"] == "session"
                    for option in row["response"].get("configOptions", [])
                    if option.get("category") == "model" or option.get("id") == "model"],
                "config_changes": [row for row in rows if row["kind"] == "config_selection"]}
    selected_kinds = {
        "permission_request", "permission_response", "permission_selected", "permission_expired",
        "probe_before", "probe_after", "config_selection", "cancel_requested", "cancel_result",
        "cancel_timeout", "forced_cancel_observed", "force_terminate", "taskkill", "cleanup",
        "process_exit", "expected_spawn_error", "expected_process_error", "connect_error",
        "prompt_error", "cleanup_error", "followup_result", "closed", "error",
    }
    with path.open("a", encoding="utf-8") as stream:
        if path.stat().st_size == 0:
            stream.write("# 第一天：取消、复用与异常退出实测\n\n")
        stream.write(f"\n## {evidence.name} / {scenario} · {evidence.started.isoformat()}\n\n")
        stream.write(f"本次场景：{'通过' if summary['passed'] else '未通过/未完成'}。"
                     f"原始双向协议及逐阶段记录：[JSONL]({evidence.path.relative_to(root / '.runtime').as_posix()})。\n\n")
        stream.write("```json\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n```\n\n")
        stream.write("权限选择、磁盘检查与生命周期证据（空缺表示本次未观察到，不代表通过）：\n\n```json\n")
        for row in rows:
            if row["kind"] in selected_kinds:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.write("```\n")


async def main(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    cwd = Path(args.cwd).resolve(strict=True)
    evidence = Evidence(args.harness, root)
    before = git_snapshot(cwd)
    failure = ""
    try:
        evidence.record("experiment", {"scenario": args.scenario, "command": subprocess.list2cmdline(
            ["uv", "run", "--locked", "python", "scripts/acp_smoke.py", *sys.argv[1:]])})
        evidence.record("versions", package_versions(args.harness))
        evidence.record("git_before", before)
        if args.scenario == "permission":
            probe = probe_snapshot(cwd)
            evidence.record("probe_before", probe)
            if probe["exists"]:
                raise FileExistsError("权限探测文件已存在，停止实验；不覆盖或自动删除")
        async with asyncio.timeout(600 if args.scenario == "permission" else 180):
            await conversation(args, cwd, evidence)
    except (Exception, asyncio.CancelledError) as error:
        failure = repr(error)
        evidence.record("error", error_details(error))
    finally:
        if args.scenario == "permission":
            evidence.record("probe_after", probe_snapshot(cwd))
        after = git_snapshot(cwd)
        evidence.record("git_after", after)
        summary = summarize(evidence, args.scenario, before, after, failure)
        evidence.record("summary", summary)
        evidence.stream.close()
        write_observations(root, evidence, summary)
    return 0 if summary["passed"] else 1


def positive_delay(value: str) -> float:
    number = float(value)
    if not 0 < number < 180:
        raise argparse.ArgumentTypeError("等待时间必须大于零且小于 180 秒")
    return number


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="真实 ACP 权限、取消与生命周期实验")
    parser.add_argument("harness", choices=get_args(Harness))
    parser.add_argument("--cwd", required=True, help="用于真实 ACP 实验的独立测试 Git 仓库目录")
    parser.add_argument("--model", help="仅为本次会话通过 ACP config option 选择模型")
    parser.add_argument("--scenario", choices=["handshake", "smoke", "permission", "cancel", "bad-executable", "process-exit"],
                        default="smoke")
    parser.add_argument("--cancel-after", type=positive_delay, default=1.0,
                        help="取消或异常退出实验在 prompt 发出后等待的秒数，默认 1")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
