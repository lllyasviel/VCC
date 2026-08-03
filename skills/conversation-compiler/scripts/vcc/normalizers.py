"""Client-specific Codex and GitHub Copilot record normalization."""

import json

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


def normalize_codex(recs):
    normalized = []
    has_authoritative_compaction = any(r.get("type") == "compacted" for r in recs)
    for r in recs:
        record_type = r.get("type")
        if record_type == "compacted":
            normalized.append({
                "type": "system",
                "subtype": "compact_boundary",
                "timestamp": r.get("timestamp"),
            })
            continue
        if (record_type == "event_msg" and
                r.get("payload", {}).get("type") == "context_compacted"):
            if not has_authoritative_compaction:
                normalized.append({
                    "type": "system",
                    "subtype": "compact_boundary",
                    "timestamp": r.get("timestamp"),
                })
            continue
        if record_type != "response_item":
            continue
        payload = r.get("payload", {})
        typ = payload.get("type")
        timestamp = r.get("timestamp")
        if typ == "message":
            role = payload.get("role")
            if role in ("developer", "system"):
                role = "system"
            if role not in ("system", "user", "assistant"):
                continue
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
        r.get("data", {}).get("toolCallId")
        for r in recs if r.get("type") == "tool.execution_start"
    }
    for r in recs:
        typ = r.get("type", "")
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        timestamp = r.get("timestamp")

        # Delta/progress events are ephemeral and would duplicate their complete event.
        if r.get("ephemeral") or typ.endswith("_delta") or typ in {
            "assistant.streaming_delta", "tool.execution_partial_result",
            "tool.execution_progress",
        }:
            continue

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
    return normalized


def is_copilot_events(recs):
    return any(
        isinstance(r.get("data"), dict) and (
            r.get("type") in {"user.message", "system.message", "assistant.message"}
            or str(r.get("type", "")).startswith("tool.execution_")
        )
        for r in recs
    )
