"""保留原始来源，将提交内容整理为带身份的完整消息记录。"""

import hashlib
import json
import re


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def capture_content(prompt: str, feedback: list[str], answer: str, metadata: dict) -> tuple[str, dict]:
    speaker = "上级委派" if metadata.get("task_id") else "用户"
    messages = [{"speaker": speaker, "content": prompt}]
    content = f"{speaker}输入：\n{prompt}"
    if feedback:
        supplement = "\n".join(feedback)
        content += f"\n{speaker}补充（已被拉取，不代表采纳）：\n{supplement}"
        messages.append({"speaker": speaker, "content": supplement})
    content += "\n员工报告（未经独立核验，执行结束不代表验收通过）：\n" + answer
    messages.append({"speaker": "员工报告", "content": answer})
    return content, {**metadata, "messages": messages, "content_sha256": content_hash(content)}


def legacy_messages(content: str, metadata: dict) -> list[dict]:
    speaker = "上级委派" if metadata.get("task_id") else "用户"
    prefix = "用户陈述：\n"
    before, marker, answer = content.partition("\nAgent 回复（未经事实核验，执行结束不代表验收通过）：\n")
    if not content.startswith(prefix) or not marker:
        return [{"speaker": "来源提交者（按正文判断实际陈述者）", "content": content}]
    prompt, feedback_marker, feedback = before[len(prefix):].partition("\n用户补充（已被拉取，不代表采纳）：\n")
    messages = [{"speaker": speaker, "content": prompt}]
    if feedback_marker:
        messages.append({"speaker": speaker, "content": feedback})
    return [*messages, {"speaker": "员工报告", "content": answer}]


def source_messages(content: str, metadata: dict) -> list[tuple[dict, dict]]:
    if metadata.get("content_sha256") == content_hash(content):
        return [(metadata, message) for message in metadata["messages"]]
    if metadata.get("content_sha256"):
        return [(metadata, {"speaker": "来源修订者（按正文判断实际陈述者）", "content": content})]
    if metadata.get("origin") == "completed_run_batch":
        remaining = content
        result = []
        for index, turn in enumerate(metadata["turns"], 1):
            heading = f"=== 回合 {index} · Run {turn['run_id']} ===\n"
            if not remaining.startswith(heading):
                raise ValueError("旧批次正文与回合来源不一致，不能推测归属")
            remaining = remaining[len(heading):]
            if index < len(metadata["turns"]):
                next_turn = metadata["turns"][index]
                separator = f"\n\n=== 回合 {index + 1} · Run {next_turn['run_id']} ===\n"
                text, found, rest = remaining.partition(separator)
                if not found:
                    raise ValueError("旧批次缺少完整回合边界")
                remaining = separator[2:] + rest
            else:
                text = remaining
            result.extend((turn, message) for message in legacy_messages(text, turn))
        return result
    return [(metadata, message) for message in legacy_messages(content, metadata)]


def message_parts(message: dict, provenance: dict, budget: int) -> list[dict]:
    text = message["content"]
    template = {**provenance, "speaker": message["speaker"], "part_index": len(text), "part_count": len(text), "content": ""}
    if len(json.dumps([template], ensure_ascii=False)) > budget:
        raise ValueError("提取块大小不足以容纳消息来源信息")
    if len(json.dumps([{**template, "content": text}], ensure_ascii=False)) <= budget:
        return [{**template, "part_index": 1, "part_count": 1, "content": text}]
    parts, pending = [], ""
    for paragraph in re.findall(r".+?(?:\r?\n[ \t]*\r?\n|\Z)", text, re.DOTALL):
        if len(json.dumps([{**template, "content": paragraph}], ensure_ascii=False)) > budget:
            raise ValueError("单个完整段落超过提取块大小，请按完整段落整理来源后重试；原文未截断")
        if len(json.dumps([{**template, "content": pending + paragraph}], ensure_ascii=False)) > budget:
            parts.append({**template, "content": pending})
            pending = ""
        pending += paragraph
    if pending:
        parts.append({**template, "content": pending})
    return [{**part, "part_index": index, "part_count": len(parts)} for index, part in enumerate(parts, 1)]


def structured_content(operation: dict, metadata: dict, config: dict) -> str:
    budget = min(config["retain_chunk_size"], config["retain_structured_chunk_size"] or config["retain_chunk_size"])
    records = []
    for index, (turn, message) in enumerate(source_messages(operation["content"], metadata), 1):
        provenance = {
            "employee_id": operation.get("member_id"), "source_id": operation["source_id"],
            "source_version": operation["version"], "run_id": turn.get("run_id"),
            "session_id": turn.get("session_id"), "task_id": turn.get("task_id"),
            "verification": turn.get("verification", "unverified"), "message_index": index,
        }
        records.extend(message_parts(message, provenance, budget))
    return json.dumps(records, ensure_ascii=False)
