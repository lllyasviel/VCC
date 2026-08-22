"""Client-specific record normalization."""

import json


# DeepSeek Harness core events.  Storage rows (``session`` and the packed
# ``*-chunks`` rows) are included separately because they are not session
# events in the public event catalog.
DEEPSEEK_CORE_EVENT_TYPES = {
    "agent-preset/selected", "agent/inbox/spliced", "approval/asked",
    "approval/decided", "approval/policy", "assistant/chunk",
    "assistant/message", "command/done", "command/run", "compaction/end",
    "compaction/prune", "compaction/start", "compaction/summary",
    "feedback/record", "goal/change", "hook/invoked", "hook/result",
    "llm/retry", "llm/retry-started", "permission/preset", "plan/mode",
    "request/context", "request/header", "sandbox/mode", "schedule/change",
    "session/end-seed", "session/title", "session/title-llm-request",
    "step/end", "step/start", "subagent/descriptor", "team/member",
    "team/message/delivered", "team/message/queued", "team/task",
    "todo/write", "tool-workflow/agent-end", "tool-workflow/agent-start",
    "tool-workflow/run-end", "tool-workflow/run-start", "tool/call",
    "tool/code-dispatch", "tool/code-dispatch-start", "tool/result",
    "turn/end", "turn/start", "user/message",
    "web/deepseek-search-llm-request",
}

DEEPSEEK_STORAGE_EVENT_TYPES = {
    "session", "text-chunks", "reasoning-chunks", "tool-call-chunks",
}

# These records are valid but are control/configuration bookkeeping rather than
# conversation content.  Other valid core events get a compact searchable
# system record below, so new core events do not silently disappear.
DEEPSEEK_IGNORED_EVENT_TYPES = {
    "session", "request/header", "request/context", "turn/start", "turn/end",
    "step/start", "step/end", "session/end-seed", "compaction/end",
}


def _deepseek_json_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in ("text", "reasoning"):
                parts.append(item.get("text", ""))
            else:
                parts.append(json.dumps(item, ensure_ascii=False, indent=2))
        return "\n\n".join(part for part in parts if part)
    return json.dumps(value, ensure_ascii=False, indent=2)


def _deepseek_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for item in content or []:
        if not isinstance(item, dict):
            blocks.append({"type": "text", "text": str(item)})
            continue
        typ = item.get("type")
        if typ == "text":
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif typ == "reasoning":
            blocks.append({"type": "thinking", "thinking": item.get("text", "")})
        elif typ == "tool-call":
            blocks.append({
                "type": "tool_use", "id": item.get("id", item.get("callId", "")),
                "name": item.get("name", "unknown"),
                "input": _codex_tool_input(item.get("arguments", {})),
            })
        elif typ == "tool-result":
            blocks.append({
                "type": "tool_result",
                "tool_use_id": item.get("toolCallId", item.get("callId", "")),
                "content": _deepseek_json_text(item.get("content", "")),
                "is_error": bool(item.get("isError", False)),
            })
        elif typ == "image":
            blocks.append({"type": "text", "text": "[image attachment]"})
        else:
            blocks.append({"type": "text", "text": json.dumps(item, ensure_ascii=False, indent=2)})
    return blocks


def _deepseek_event_time(record, fallback=None):
    return record.get("time", fallback)


def _deepseek_system_event(record, label=None):
    """Keep unmodeled official events searchable without inventing an IR type."""
    typ = record.get("type", "unknown")
    data = record.get("data")
    if isinstance(data, dict):
        # Avoid copying large request schemas into the conversation output.
        visible = {k: v for k, v in data.items()
                   if k not in {"schema", "toolDefinitions", "tools"}}
        text = _deepseek_json_text(visible)
    else:
        text = _deepseek_json_text(data) if data is not None else ""
    prefix = label or typ
    content = f"[{prefix}]"
    if text and text != "{}":
        content += f"\n{text}"
    return {"type": "system", "timestamp": _deepseek_event_time(record),
            "message": {"content": [{"type": "text", "text": content}]}}


def _deepseek_expand_chunk_rows(recs):
    expanded = []
    for row in recs:
        tag = row.get("type")
        if tag not in {"text-chunks", "reasoning-chunks", "tool-call-chunks"}:
            expanded.append(row)
            continue
        data = row.get("data", {})
        members = data.get("args") if tag == "tool-call-chunks" else data.get("texts", [])
        deltas = data.get("dt", [])
        if (not isinstance(members, list) or not isinstance(deltas, list) or
                len(deltas) != max(0, len(members) - 1) or
                not isinstance(row.get("seq0"), int) or
                not isinstance(row.get("time0"), (int, float))):
            raise ValueError(f"malformed {tag} storage row")
        time_value = row.get("time0")
        for index, member in enumerate(members):
            if index:
                delta = deltas[index - 1]
                if not isinstance(delta, (int, float)):
                    raise ValueError(f"malformed {tag} storage row: invalid dt")
                time_value += delta
            if not isinstance(member, str):
                raise ValueError(f"malformed {tag} storage row: non-string chunk")
            if tag == "text-chunks":
                chunk = {"type": "text-delta", "index": data.get("index", 0), "text": member}
            elif tag == "reasoning-chunks":
                chunk = {"type": "reasoning-delta", "index": data.get("index", 0), "text": member}
            else:
                chunk = {
                    "type": "tool-call-delta", "index": data.get("index", 0),
                    "id": data.get("id", ""), "argumentsDelta": member,
                }
                if "name" in data:
                    chunk["name"] = data["name"]
            expanded.append({
                "type": "assistant/chunk", "seq": row.get("seq0", 0) + index,
                "time": time_value,
                "data": {"turn": data.get("turn", 0), "step": data.get("step", 0), "chunk": chunk},
            })
    return expanded


def normalize_deepseek(recs):
    """Normalize DeepSeek Harness session events into VCC's IR input."""
    recs = _deepseek_expand_chunk_rows(recs)
    normalized = []
    chunk_indexes = {}
    chunk_tool_blocks = {}
    has_assembled = {
        ((r.get("data") if isinstance(r.get("data"), dict) else {}).get("turn"),
         (r.get("data") if isinstance(r.get("data"), dict) else {}).get("step"))
        for r in recs if r.get("type") == "assistant/message"
        and isinstance(r.get("data"), dict)
    }
    for r in recs:
        typ = r.get("type", "")
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        timestamp = _deepseek_event_time(r)
        if typ == "user/message":
            normalized.append({"type": "user", "timestamp": timestamp,
                               "message": {"content": _deepseek_blocks(data.get("content", []))}})
        elif typ == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else data
            blocks = _deepseek_blocks(message.get("content", []))
            entry = {"type": "assistant", "timestamp": timestamp,
                     "message": {"id": message.get("id"), "content": blocks}}
            if data.get("usage"):
                entry["message"]["usage"] = data["usage"]
            normalized.append(entry)
        elif typ == "tool/call":
            normalized.append({"type": "assistant", "timestamp": timestamp,
                               "message": {"content": [{
                                   "type": "tool_use", "id": data.get("callId", ""),
                                   "name": data.get("name", "unknown"),
                                   "input": _codex_tool_input(data.get("arguments", {})),
                               }]}})
        elif typ == "tool/result":
            message = data.get("message") if isinstance(data.get("message"), dict) else data
            blocks = _deepseek_blocks(message.get("content", []))
            normalized.append({"type": "user", "timestamp": timestamp,
                               "message": {"content": blocks}})
        elif typ == "assistant/chunk":
            key = (data.get("turn"), data.get("step"))
            if key in has_assembled:
                continue
            chunk = data.get("chunk", {})
            if key not in chunk_indexes:
                chunk_indexes[key] = len(normalized)
                normalized.append({"type": "assistant", "timestamp": timestamp,
                                   "message": {"content": []}})
            new_blocks = _deepseek_blocks([{
                    "type": "text", "text": chunk.get("text", "")
                }] if chunk.get("type") == "text-delta" else [{
                    "type": "reasoning", "text": chunk.get("text", "")
                }] if chunk.get("type") == "reasoning-delta" else [{
                    "type": "tool-call", "id": chunk.get("id", ""),
                    "name": chunk.get("name", "unknown"),
                    "arguments": chunk.get("argumentsDelta", "")
                }] if chunk.get("type") == "tool-call-delta" else [])
            blocks = normalized[chunk_indexes[key]]["message"]["content"]
            for block in new_blocks:
                if blocks and block.get("type") == blocks[-1].get("type") == "text":
                    blocks[-1]["text"] += block.get("text", "")
                elif blocks and block.get("type") == blocks[-1].get("type") == "thinking":
                    blocks[-1]["thinking"] += block.get("thinking", "")
                elif block.get("type") == "tool_use":
                    chunk = data.get("chunk", {})
                    tool_key = (key, chunk.get("index", 0),
                                chunk.get("id", ""))
                    previous = chunk_tool_blocks.get(tool_key)
                    if previous is not None:
                        old = previous.get("input", {}).get("input", "")
                        new = block.get("input", {}).get("input", "")
                        previous["input"] = {"input": old + new}
                    else:
                        chunk_tool_blocks[tool_key] = block
                        blocks.append(block)
                else:
                    blocks.append(block)
        elif typ == "compaction/start":
            normalized.append({"type": "system", "subtype": "compact_boundary", "timestamp": timestamp})
        elif typ == "compaction/summary":
            summary = data.get("rawOutput") or data.get("summary") or data.get("content")
            if summary:
                normalized.append({"type": "user", "timestamp": timestamp, "isCompactSummary": True,
                                   "message": {"content": _deepseek_json_text(summary)}})
        elif typ == "session/title":
            title = data.get("title") or data.get("text") or data.get("name")
            if title:
                normalized.append(_deepseek_system_event(r, f"session/title: {title}"))
        elif typ == "goal/change":
            goal = data.get("goal", data)
            objective = goal.get("objective") if isinstance(goal, dict) else None
            label = "goal/change" + (f": {objective}" if objective else "")
            normalized.append(_deepseek_system_event(r, label))
        elif typ in DEEPSEEK_IGNORED_EVENT_TYPES:
            continue
        elif typ in DEEPSEEK_CORE_EVENT_TYPES:
            normalized.append(_deepseek_system_event(r))
    return normalized

def _codex_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") in ("input_text", "output_text", "text"):
                parts.append(item.get("text", ""))
            else:
                parts.append(json.dumps(item, ensure_ascii=False, indent=2))
        return "\n\n".join(p for p in parts if p)
    return json.dumps(value, ensure_ascii=False, indent=2)


def _codex_blocks(content):
    blocks = []
    for item in content or []:
        if not isinstance(item, dict):
            blocks.append({"type": "text", "text": str(item)})
            continue
        if item.get("type") in ("input_text", "output_text", "text"):
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif item.get("type") == "encrypted_content":
            continue
        else:
            blocks.append({"type": "text", "text": json.dumps(item, ensure_ascii=False, indent=2)})
    return blocks


def _codex_tool_input(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"input": value}
        return decoded if isinstance(decoded, dict) else {"input": decoded}
    return {"input": value}


def normalize_codex_record(record):
    """Normalize one Codex record; compaction de-duplication is stream state."""
    normalized = []
    record_type = record.get("type")
    if record_type == "compacted":
        normalized.append({
            "type": "system",
            "subtype": "compact_boundary",
            "timestamp": record.get("timestamp"),
        })
    elif (record_type == "event_msg" and
          record.get("payload", {}).get("type") == "context_compacted"):
        normalized.append({
            "type": "system",
            "subtype": "compact_boundary",
            "timestamp": record.get("timestamp"),
        })
    elif record_type == "response_item":
        payload = record.get("payload", {})
        typ = payload.get("type")
        timestamp = record.get("timestamp")
        if typ == "message":
            role = payload.get("role")
            if role in ("developer", "system"):
                role = "system"
            if role not in ("system", "user", "assistant"):
                return normalized
            normalized.append({
                "type": role,
                "timestamp": timestamp,
                "message": {"content": _codex_blocks(payload.get("content"))},
            })
        elif typ in ("function_call", "custom_tool_call"):
            value = payload.get("arguments") if typ == "function_call" else payload.get("input")
            normalized.append({
                "type": "assistant",
                "timestamp": timestamp,
                "message": {"content": [{
                    "type": "tool_use",
                    "id": payload.get("call_id", payload.get("id", "")),
                    "name": payload.get("name", "unknown"),
                    "input": _codex_tool_input(value),
                }]},
            })
        elif typ in ("function_call_output", "custom_tool_call_output"):
            normalized.append({
                "type": "user",
                "timestamp": timestamp,
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": payload.get("call_id", ""),
                    "content": _codex_text(payload.get("output", "")),
                }]},
            })
        elif typ == "agent_message":
            blocks = _codex_blocks(payload.get("content"))
            if blocks:
                author = payload.get("author", "agent")
                recipient = payload.get("recipient", "agent")
                blocks[0]["text"] = f"[agent message {author} -> {recipient}]\n" + blocks[0]["text"]
                normalized.append({
                    "type": "assistant", "timestamp": timestamp,
                    "message": {"content": blocks},
                })
        elif typ == "reasoning":
            summary = payload.get("summary") or []
            parts = []
            for item in summary:
                if isinstance(item, dict) and item.get("type") in ("summary_text", "text"):
                    parts.append(item.get("text", ""))
            if parts:
                normalized.append({
                    "type": "assistant", "timestamp": timestamp,
                    "message": {"content": [{"type": "thinking",
                                              "thinking": "\n\n".join(parts)}]},
                })
    return normalized


def _copilot_text(value):
    """Render Copilot event content without losing structured tool output."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                parts.append(text if isinstance(text, str) else
                             json.dumps(item, ensure_ascii=False, indent=2))
            else:
                parts.append(str(item))
        return "\n\n".join(p for p in parts if p)
    return json.dumps(value, ensure_ascii=False, indent=2)


def _copilot_user_blocks(data):
    blocks = []
    content = data.get("content", "")
    if content:
        blocks.append({"type": "text", "text": _copilot_text(content)})
    attachments = data.get("attachments")
    if attachments:
        blocks.append({
            "type": "text",
            "text": "[attachments]\n" + json.dumps(
                attachments, ensure_ascii=False, indent=2),
        })
    return blocks


def normalize_copilot(recs):
    """Normalize GitHub Copilot CLI persisted session events to VCC's IR input."""
    normalized = []
    started_tool_ids = {
        r["data"].get("toolCallId")
        for r in recs
        if r.get("type") == "tool.execution_start" and
        isinstance(r.get("data"), dict)
    }
    pending_stream = []
    pending_timestamp = None

    def flush_stream():
        nonlocal pending_stream, pending_timestamp
        content = "".join(pending_stream)
        if content:
            normalized.append({
                "type": "assistant", "timestamp": pending_timestamp,
                "message": {"content": [{"type": "text", "text": content}]},
            })
        pending_stream = []
        pending_timestamp = None

    for r in recs:
        typ = r.get("type", "")
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        timestamp = r.get("timestamp")

        if typ in {"assistant.message_delta", "assistant.streaming_delta"}:
            # Current Copilot SDKs use message_delta/deltaContent for text.
            # Older captured logs may carry text in streaming_delta/content;
            # current streaming_delta rows contain only byte-progress metadata.
            content = (
                data.get("deltaContent", "")
                if typ == "assistant.message_delta"
                else data.get("content", data.get("delta", data.get("text", "")))
            )
            text = _copilot_text(content)
            if text:
                if pending_timestamp is None:
                    pending_timestamp = timestamp
                pending_stream.append(text)
            continue

        # Other delta/progress events are ephemeral and would duplicate their
        # complete event. Preserve an unfinished assistant stream before crossing
        # into another semantic event.
        if r.get("ephemeral") or typ.endswith("_delta") or typ in {
            "tool.execution_partial_result", "tool.execution_progress",
        }:
            continue

        if typ != "assistant.message":
            flush_stream()

        if typ == "system.message":
            content = data.get("content", "")
            if content:
                normalized.append({
                    "type": "system", "timestamp": timestamp,
                    "content": _copilot_text(content),
                })
        elif typ == "user.message":
            blocks = _copilot_user_blocks(data)
            if blocks:
                normalized.append({
                    "type": "user", "timestamp": timestamp,
                    "message": {"content": blocks},
                })
        elif typ == "assistant.reasoning":
            content = data.get("content", "")
            if content:
                normalized.append({
                    "type": "assistant", "timestamp": timestamp,
                    "message": {"content": [{
                        "type": "thinking", "thinking": _copilot_text(content),
                    }]},
                })
        elif typ == "assistant.message":
            blocks = []
            content = data.get("content", "")
            if content:
                blocks.append({"type": "text", "text": _copilot_text(content)})
                # A persisted final message is authoritative and replaces the
                # immediately preceding stream fragments.
                pending_stream = []
                pending_timestamp = None
            elif pending_stream:
                blocks.append({
                    "type": "text", "text": "".join(pending_stream),
                })
                pending_stream = []
                pending_timestamp = None
            # Some SDK producers persist requests without execution events.
            for request in data.get("toolRequests") or []:
                if not isinstance(request, dict):
                    continue
                tool_id = request.get("toolCallId", "")
                if tool_id in started_tool_ids:
                    continue
                blocks.append({
                    "type": "tool_use", "id": tool_id,
                    "name": request.get("name", "unknown"),
                    "input": _codex_tool_input(request.get("arguments", {})),
                })
            if blocks:
                normalized.append({
                    "type": "assistant", "timestamp": timestamp,
                    "message": {"id": data.get("messageId"), "content": blocks},
                })
        elif typ in ("tool.execution_start", "tool.user_requested"):
            if (typ == "tool.user_requested" and
                    data.get("toolCallId") in started_tool_ids):
                continue
            normalized.append({
                "type": "assistant", "timestamp": timestamp,
                "message": {"content": [{
                    "type": "tool_use",
                    "id": data.get("toolCallId", ""),
                    "name": data.get("toolName", "unknown"),
                    "input": _codex_tool_input(data.get("arguments", {})),
                }]},
            })
        elif typ == "tool.execution_complete":
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            if data.get("success", False):
                output = (result.get("detailedContent") or result.get("content") or
                          result.get("contents") or "")
            else:
                error = data.get("error")
                output = error.get("message", "") if isinstance(error, dict) else error
            normalized.append({
                "type": "user", "timestamp": timestamp,
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": data.get("toolCallId", ""),
                    "content": _copilot_text(output),
                    "is_error": not data.get("success", False),
                }]},
            })
        elif typ == "session.compaction_complete" and data.get("success"):
            normalized.append({"type": "system", "subtype": "compact_boundary",
                               "timestamp": timestamp})
            summary = data.get("summaryContent")
            if summary:
                normalized.append({
                    "type": "user", "timestamp": timestamp,
                    "isCompactSummary": True,
                    "message": {"content": _copilot_text(summary)},
                })
        elif typ == "session.error" and data.get("message"):
            normalized.append({
                "type": "system", "timestamp": timestamp,
                "content": "Copilot session error: " + _copilot_text(data["message"]),
            })
    flush_stream()
    return normalized


def is_copilot_record(record):
    return isinstance(record.get("data"), dict) and (
        record.get("type") in {
            "user.message", "system.message", "assistant.message",
            "assistant.reasoning", "assistant.message_delta",
            "assistant.streaming_delta",
        }
        or str(record.get("type", "")).startswith("tool.execution_")
    )
