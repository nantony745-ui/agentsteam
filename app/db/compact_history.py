"""停服后执行：python -m app.db.compact_history SOURCE OUTPUT --stopped [--apply]。"""

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from app.db.event_codec import (
    TEXT_LIMIT, ToolOutputTracker, business_payload, compact, pack_payload, restore_payload,
)
from app.db.lease import DatabaseLease


def decoded_events(db):
    has_details = db.execute("SELECT 1 FROM sqlite_master WHERE name='event_details'").fetchone()
    trackers = {}
    for row in db.execute("SELECT * FROM events ORDER BY id"):
        event = dict(row)
        payload = json.loads(event["payload"])
        if has_details:
            parts = [dict(part) for part in db.execute("SELECT * FROM event_details WHERE event_id=? ORDER BY part", (event["id"],))]
            payload = restore_payload(payload, parts)
        if event["kind"] == "raw" and isinstance(payload.get("raw"), dict):
            from app.agents.acp_client import normalize_update
            event["kind"] = normalize_update(payload["raw"])["kind"]
        payload = business_payload(event["kind"], payload)
        if event["kind"] == "agent.tool":
            payload = trackers.setdefault(event["run_id"], ToolOutputTracker()).normalize(payload)
        if event["kind"] == "run.status" and payload.get("status") in {"completed", "canceled", "interrupted", "failed"}:
            trackers.pop(event["run_id"], None)
        event["payload"] = payload
        yield event


def semantic_digest(db):
    digest = hashlib.sha256()
    text_hash = None
    text_key = None

    def finish_text():
        nonlocal text_hash, text_key
        if text_hash is not None:
            digest.update(compact([text_key, text_hash.hexdigest()]).encode("utf-8"))
        text_hash = text_key = None

    for event in decoded_events(db):
        payload = event["payload"]
        key = [event[name] for name in ("project_id", "session_id", "run_id", "task_id", "kind")]
        if event["kind"] == "agent.text":
            key.append({name: value for name, value in payload.items() if name != "text"})
            if key != text_key:
                finish_text()
                text_key, text_hash = key, hashlib.sha256()
            text_hash.update(payload["text"].encode("utf-8"))
        else:
            finish_text()
            # 转换只移除协议副本，工具业务输出和操作顺序必须逐项相同。
            digest.update(compact([key, payload]).encode("utf-8"))
    finish_text()
    return digest.hexdigest()


def convert(source, target):
    target.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
    high = source.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
    pending = None

    def emit(event):
        payload, parts = pack_payload(event["kind"], event["payload"])
        event = {**event, "payload": compact(payload)}
        names = list(event)
        target.execute(f"INSERT INTO events({','.join(names)}) VALUES ({','.join('?' for _ in names)})", tuple(event.values()))
        target.executemany("INSERT INTO event_details VALUES (?,?,?,?,?,?)", [(event["id"], *part) for part in parts])

    target.execute("BEGIN")
    target.execute("DELETE FROM event_details")
    target.execute("DELETE FROM events")
    for event in decoded_events(source):
        payload = event["payload"]
        if event["kind"] == "agent.text" and pending is not None:
            previous = pending["payload"]
            same = all(event[name] == pending[name] for name in ("project_id", "session_id", "run_id", "task_id", "kind"))
            same = same and {k: v for k, v in payload.items() if k != "text"} == {k: v for k, v in previous.items() if k != "text"}
            if same and len((previous["text"] + payload["text"]).encode("utf-8")) <= TEXT_LIMIT:
                pending = {**event, "payload": {**payload, "text": previous["text"] + payload["text"]}}
                continue
        if pending is not None:
            emit(pending)
        pending = event
    if pending is not None:
        emit(pending)
    target.execute("UPDATE tasks SET summary=substr(summary,1,160)")
    target.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='events'", (high,))
    target.commit()
    target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    target.execute("VACUUM")
    target.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def verify(source, target):
    before, after = semantic_digest(source), semantic_digest(target)
    if before != after:
        raise ValueError("转换前后业务历史不一致")
    for table in ("projects", "sessions", "runs", "interactions"):
        if any(before != after for before, after in zip(
                source.execute(f"SELECT * FROM {table} ORDER BY id"),
                target.execute(f"SELECT * FROM {table} ORDER BY id"), strict=True)):
            raise ValueError(f"{table} 转换前后不一致")
    for before_task, after_task in zip(source.execute("SELECT * FROM tasks ORDER BY id"), target.execute("SELECT * FROM tasks ORDER BY id"), strict=True):
        expected = dict(before_task)
        if expected["summary"] is not None:
            expected["summary"] = expected["summary"][:160]
        if expected != dict(after_task):
            raise ValueError("任务状态或关联发生变化")
    integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
    foreign = list(target.execute("PRAGMA foreign_key_check"))
    if integrity != "ok" or foreign:
        raise ValueError("完整性或外键检查失败")
    return {"semantic_sha256": after, "integrity_check": integrity, "foreign_key_check": foreign}


def open_readonly(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def compact_database(source: Path, output: Path, *, stopped: bool, apply: bool = False,
                     diagnostic_path: Path | None = None) -> dict:
    if not stopped:
        raise ValueError("请停止后端和活动执行，并显式提供 --stopped")
    source, output = source.resolve(strict=True), output.resolve()
    if source == output:
        raise ValueError("输出必须为新的数据库路径")
    output.parent.mkdir(parents=True, exist_ok=True)
    backup = output.with_suffix(output.suffix + ".backup")
    report_path = output.with_suffix(output.suffix + ".report.json")
    if any(path.exists() for path in (output, backup, report_path)):
        raise FileExistsError("输出、备份或报告已存在")
    with DatabaseLease(source), closing(open_readonly(source)) as live:
        active = live.execute("SELECT count(*) FROM runs WHERE status IN ('running','waiting_permission','cancel_requested')").fetchone()[0]
        if active:
            raise ValueError("数据库仍记录活动执行；请先正常停止执行")
        with closing(sqlite3.connect(backup)) as saved:
            live.backup(saved)
        with closing(open_readonly(backup)) as saved, closing(sqlite3.connect(output)) as target:
            target.row_factory = sqlite3.Row
            saved.backup(target)
            convert(saved, target)
            checks = verify(saved, target)
            log_path = diagnostic_path or source.parent / "acp-protocol.log"
            logs = [Path(str(log_path) + suffix) for suffix in ("", ".1", ".2")]
            log_sizes = [{"path": str(path), "bytes": path.stat().st_size} for path in logs if path.exists()]
            report = {"diagnostic_logs": log_sizes, "diagnostic_bytes": sum(item["bytes"] for item in log_sizes),
                      "source": str(source), "output": str(output), "backup": str(backup),
                      "source_events": saved.execute("SELECT count(*) FROM events").fetchone()[0],
                      "output_events": target.execute("SELECT count(*) FROM events").fetchone()[0],
                      "source_bytes": source.stat().st_size,
                      "source_wal_bytes": Path(str(source) + "-wal").stat().st_size if Path(str(source) + "-wal").exists() else 0,
                      "output_bytes": output.stat().st_size,
                      "backup_bytes": backup.stat().st_size, "applied": False, **checks}
        if apply:
            # 共享租约保证新版后端不会在 checkpoint 与替换之间启动。
            live.close()
            with closing(sqlite3.connect(source)) as writable:
                checkpoint = writable.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint[0] != 0:
                    raise RuntimeError("WAL 仍被占用，拒绝替换")
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(source) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            os.replace(output, source)
            report["applied"] = True
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--stopped", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--diagnostic-path", type=Path)
    args = parser.parse_args()
    print(json.dumps(compact_database(args.source, args.output, stopped=args.stopped, apply=args.apply, diagnostic_path=args.diagnostic_path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
