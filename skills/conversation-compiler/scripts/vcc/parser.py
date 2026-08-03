"""Source detection, client normalization, JSONL parsing, and IR construction."""

import base64
import binascii
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

from .normalizers import is_copilot_events, normalize_codex, normalize_copilot

from .common import (
    DEFAULT_MAX_MEDIA_BYTES,
    VCCError,
    ANSI_RE,
    CONTROL_RE,
    emit_mapping,
)

# ── lexer ──

SEP = "══════════════════════════════"
_DISCARD_T = {"queue-operation", "file-history-snapshot", "last-prompt", "progress"}
_DISCARD_S = {"stop_hook_summary", "api_error", "bridge_status", "informational", "local_command"}

def short_filename(fn):
    n, e = os.path.splitext(fn)
    return ("#" + n[-6:] + e) if len(n) > 12 else fn

def _short_tid(tid):
    return tid[-6:] if len(tid) > 6 else tid









def load_records(path, tolerate_partial_tail=True, diagnostics=None):
    recs = []

    def parse_line(line, line_number, final=False):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            is_partial_tail = (
                tolerate_partial_tail and final and
                not line.endswith(("\n", "\r"))
            )
            if is_partial_tail:
                if diagnostics is not None:
                    diagnostics["partial_tail_ignored"] = True
                print(
                    f"warning: {path}:{line_number}: ignored incomplete live-session tail",
                    file=sys.stderr,
                )
                return
            raise VCCError(
                f"{path}:{line_number}:{exc.colno}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(rec, dict):
            raise VCCError(f"{path}:{line_number}: expected a JSON object")
        recs.append(rec)

    # Hold only the most recent nonblank line so an unterminated malformed tail can
    # be distinguished from a malformed middle record without retaining raw JSONL.
    pending = None
    with open(path, encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            if not line.strip():
                continue
            if pending is not None:
                parse_line(*pending)
            pending = (line, line_number)
    if pending is not None:
        parse_line(*pending, final=True)
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update({"schema_version": 2, "source": os.path.abspath(path),
                        "source_records_total": len(recs),
                        "partial_tail_ignored": diagnostics.get("partial_tail_ignored", False)})
    if any(r.get("type") == "response_item" for r in recs):
        supported = {"message", "function_call", "custom_tool_call", "agent_message",
                     "reasoning", "function_call_output", "custom_tool_call_output"}
        response_types = [r.get("payload", {}).get("type") for r in recs
                          if r.get("type") == "response_item"]
        normalized = normalize_codex(recs)
        known_top_level = {
            "session_meta", "event_msg", "response_item", "world_state",
            "turn_context", "inter_agent_communication_metadata", "compacted",
        }
        unknown_response_types = {str(t) for t in response_types if t not in supported}
        unknown_top_level_types = {
            str(r.get("type")) for r in recs if r.get("type") not in known_top_level
        }
        source_unknown = (
            sum(1 for t in response_types if t not in supported) +
            sum(1 for r in recs if r.get("type") not in known_top_level)
        )
        has_authoritative_compaction = any(r.get("type") == "compacted" for r in recs)
        source_supported = sum(
            1 for r in recs
            if (r.get("type") == "response_item" and
                r.get("payload", {}).get("type") in supported)
            or r.get("type") == "compacted"
            or (not has_authoritative_compaction and r.get("type") == "event_msg" and
                r.get("payload", {}).get("type") == "context_compacted")
        )
        source_ignored = len(recs) - source_supported - source_unknown
        compaction_boundaries = sum(
            1 for r in normalized
            if r.get("type") == "system" and r.get("subtype") == "compact_boundary"
        )
        diagnostics.update({
            "client": "codex",
            "source_records_supported": source_supported,
            "source_records_ignored": source_ignored,
            "source_records_unknown": source_unknown,
            "normalized_records_emitted": len(normalized),
            "unknown_types": sorted(unknown_response_types | unknown_top_level_types),
            "compaction_boundaries": compaction_boundaries,
        })
        return normalized
    if is_copilot_events(recs):
        normalized = normalize_copilot(recs)
        known = {
            "system.message", "user.message", "assistant.reasoning", "assistant.message",
            "tool.execution_start", "tool.user_requested", "tool.execution_complete",
            "session.compaction_complete", "session.error", "assistant.streaming_delta",
            "tool.execution_partial_result", "tool.execution_progress",
        }
        unknown = {str(r.get("type")) for r in recs
                   if r.get("type") not in known and not str(r.get("type", "")).endswith("_delta")}
        source_unknown = sum(1 for r in recs if str(r.get("type")) in unknown)
        ignored_types = {
            "assistant.streaming_delta", "tool.execution_partial_result",
            "tool.execution_progress",
        }
        source_ignored = sum(
            1 for r in recs
            if r.get("ephemeral") or r.get("type") in ignored_types
            or str(r.get("type", "")).endswith("_delta")
        )
        source_supported = len(recs) - source_ignored - source_unknown
        compaction_boundaries = sum(
            1 for r in normalized
            if r.get("type") == "system" and r.get("subtype") == "compact_boundary"
        )
        diagnostics.update({
            "client": "copilot",
            "source_records_supported": source_supported,
            "source_records_ignored": source_ignored,
            "source_records_unknown": source_unknown,
            "normalized_records_emitted": len(normalized),
            "unknown_types": sorted(unknown),
            "compaction_boundaries": compaction_boundaries,
        })
        return normalized
    known_claude_metadata = {"attachment", "mode"}
    known_claude = {"system", "user", "assistant"} | _DISCARD_T | known_claude_metadata
    supported_count = sum(1 for r in recs
                          if r.get("type") in {"system", "user", "assistant"} and not _discard(r))
    unknown_count = sum(1 for r in recs if r.get("type") not in known_claude)
    diagnostics.update({
        "client": "claude",
        "source_records_supported": supported_count,
        "source_records_ignored": sum(1 for r in recs
                                      if _discard(r) or r.get("type") in known_claude_metadata),
        "source_records_unknown": unknown_count,
        "normalized_records_emitted": supported_count,
        "unknown_types": sorted({str(r.get("type")) for r in recs
                                 if r.get("type") not in known_claude}),
        "compaction_boundaries": sum(
            1 for r in recs
            if r.get("type") == "system" and r.get("subtype") == "compact_boundary"
        ),
    })
    return recs


def collect_stats(chain):
    """Extract usage/timing/model stats from a chain of records, including subagents."""
    from collections import defaultdict
    from datetime import datetime
    totals = defaultdict(int)
    models = set()
    timestamps = []
    api_calls = 0
    tool_uses = 0
    subagent_tokens = 0
    for r in chain:
        msg = r.get("message", {})
        ts = r.get("timestamp")
        if ts:
            timestamps.append(ts)
        usage = msg.get("usage")
        if usage:
            api_calls += 1
            for k, v in usage.items():
                if isinstance(v, int):
                    totals[k] += v
        model = msg.get("model")
        if model:
            models.add(model)
        content = msg.get("content", [])
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_uses += 1
        # subagent usage from toolUseResult
        tur = r.get("toolUseResult")
        if isinstance(tur, dict) and tur.get("agentId"):
            try:
                subagent_tokens += int(tur.get("totalTokens", 0))
            except (ValueError, TypeError):
                pass
    duration = None
    if len(timestamps) >= 2:
        try:
            t0 = datetime.fromisoformat(min(timestamps).replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(max(timestamps).replace("Z", "+00:00"))
            duration = int((t1 - t0).total_seconds())
        except Exception:
            pass
    if api_calls == 0:
        return None
    lines = [SEP, "[stats]", ""]
    lines.append(f"model: {', '.join(sorted(models))}")
    lines.append(f"api_calls: {api_calls}  tool_uses: {tool_uses}")
    if duration is not None:
        m, s = divmod(duration, 60)
        lines.append(f"duration: {m}m{s:02d}s" if m else f"duration: {s}s")
    inp = totals.get("input_tokens", 0)
    cr = totals.get("cache_read_input_tokens", 0)
    cc = totals.get("cache_creation_input_tokens", 0)
    out = totals.get("output_tokens", 0)
    own = inp + cr + cc + out
    own_eff = int(inp * 1.0 + cr * 0.1 + cc * 1.25 + out * 5.0)

    def _fmt_block(label, raw, eff):
        return f"{label}: {raw:,} (effective: {eff:,})"

    if subagent_tokens:
        lines.append("")
        lines.append("Subagents:")
        lines.append(f"  total: {subagent_tokens:,}")
        lines.append("")
        lines.append("Main:")

    parts = []
    if inp: parts.append(f"input: {inp:,}")
    if cr: parts.append(f"cache_read: {cr:,}")
    if cc: parts.append(f"cache_create: {cc:,}")
    pfx = "  " if subagent_tokens else ""
    if parts:
        lines.append(f"{pfx}{'  '.join(parts)}")
    lines.append(f"{pfx}output: {out:,}")
    lines.append(f"{pfx}{_fmt_block('total', own, own_eff)}")

    if subagent_tokens:
        all_raw = own + subagent_tokens
        all_eff = own_eff + subagent_tokens
        lines.append("")
        lines.append("All:")
        lines.append(f"  {_fmt_block('total', all_raw, all_eff)}")
    return lines

def _sanitize(text):
    if not text:
        return text
    if "\r" in text:
        text = text.replace("\r", "")
    if "\x1b" in text:
        text = ANSI_RE.sub("", text)
    return CONTROL_RE.sub("", text)

def _preprocess_tool_text(text, tool_name):
    text = _sanitize(text)
    if tool_name != "Read":
        return text
    lines = []
    for line in text.split("\n"):
        if "→" in line:
            head, tail = line.split("→", 1)
            if head.strip().isdigit():
                line = tail
        lines.append(line)
    return "\n".join(lines)

def _discard(r):
    t = r.get("type")
    return t in _DISCARD_T or (t == "system" and r.get("subtype") in _DISCARD_S)

def merge_chunks(recs):
    merged = []
    active_mid = None
    active_idx = None
    for r in recs:
        if r.get("type") == "assistant":
            m = r.get("message", {})
            mid = m.get("id")
            if mid and mid == active_mid and active_idx is not None:
                merged[active_idx]["message"]["content"].extend(m.get("content", []))
                if m.get("stop_reason"):
                    merged[active_idx]["message"]["stop_reason"] = m["stop_reason"]
            else:
                merged.append(r)
                if mid:
                    active_mid = mid
                    active_idx = len(merged) - 1
                else:
                    active_mid = None
                    active_idx = None
        else:
            merged.append(r)
            if not _discard(r):
                active_mid = None
                active_idx = None
    return merged

def split_chains(recs):
    kept = [r for r in recs if not _discard(r)]
    chains, cur = [], []
    for r in kept:
        if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
            if cur: chains.append(cur)
            cur = []
        else:
            cur.append(r)
    if cur: chains.append(cur)
    return chains

# ── image / doc ──

def _media_ext(media_type, default_ext):
    if "/" not in media_type:
        return default_ext
    ext = media_type.split("/", 1)[1].split("+", 1)[0]
    if ext == "jpeg":
        return "jpg"
    return ext if re.fullmatch(r"[A-Za-z0-9]{1,10}", ext) else default_ext

def _extract_base64(source, outdir, data_prefix, data_index, stem, default_mt, default_ext,
                    max_media_bytes, protected_inputs, media_writer):
    mt = source.get("media_type", default_mt)
    fn = f"{data_prefix}_{stem}_{data_index}.{_media_ext(mt, default_ext)}"
    encoded = source.get("data", "")
    if not isinstance(encoded, (str, bytes)):
        raise VCCError(f"embedded {stem} payload is not base64 text")
    if max_media_bytes and len(encoded) > ((max_media_bytes + 2) // 3) * 4:
        raise VCCError(f"embedded {stem} exceeds {max_media_bytes} decoded bytes")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VCCError(f"invalid embedded {stem} base64 data: {exc}") from exc
    if max_media_bytes and len(decoded) > max_media_bytes:
        raise VCCError(f"embedded {stem} exceeds {max_media_bytes} decoded bytes")
    path = os.path.join(outdir, fn)
    if os.path.realpath(path) in protected_inputs:
        raise VCCError(f"refusing to overwrite authoritative input with media: {path}")
    media_writer(path, decoded)
    return fn

def _extract_img(source, outdir, data_prefix, data_index, max_media_bytes,
                 protected_inputs, media_writer):
    return _extract_base64(source, outdir, data_prefix, data_index,
                           "img", "image/png", "png", max_media_bytes, protected_inputs,
                           media_writer)

def _extract_doc(source, outdir, data_prefix, data_index, max_media_bytes,
                 protected_inputs, media_writer):
    return _extract_base64(source, outdir, data_prefix, data_index,
                           "doc", "application/octet-stream", "bin", max_media_bytes,
                           protected_inputs, media_writer)


# ── tool_call summary ──

_TOOL_SUMMARY_FIELDS = {
    "Read": "file_path", "Edit": "file_path", "Write": "file_path",
    "Glob": "pattern", "Grep": "pattern",
    "Agent": "description", "Skill": "skill",
}

def _tool_summary(name, inp):
    """Build one-line summary: * Name "param" """
    field = _TOOL_SUMMARY_FIELDS.get(name)
    if field and field in inp:
        return f'* {name} "{inp[field]}"'
    if name == "Bash":
        val = inp.get("description") or inp.get("command", "")
        if val:
            # truncate long commands
            if not inp.get("description") and len(val) > 60:
                val = val[:57] + "..."
            return f'* {name} "{val}"'
    value = inp.get("input")
    if isinstance(value, str) and value:
        value = value.splitlines()[0]
        if len(value) > 60:
            value = value[:57] + "..."
        return f'* {name} "{value}"'
    return f"* {name}"


# ── IR node ──

def _node(typ, content, **kw):
    o = {"type": typ, "content": content,
         "searchable": kw.pop("searchable", False)}
    o.update(kw)
    return o

# ── parser ──

def build_ir(chain, outdir, data_prefix, data_ctr, extract_media=True,
             max_media_bytes=DEFAULT_MAX_MEDIA_BYTES, protected_inputs=None,
             media_writer=None):
    ir = []
    protected_inputs = protected_inputs or set()
    if extract_media and media_writer is None:
        raise VCCError("materialized parsing requires a media writer")
    sec = 0
    blk = 0

    tid_name = {}
    for r in chain:
        if r.get("type") == "assistant":
            for b in r.get("message", {}).get("content", []):
                if b.get("type") == "tool_use":
                    tid_name[b.get("id", "")] = b.get("name", "unknown")

    def _emit_sep():
        if sec > 0:
            ir.append(_node("meta", ["", SEP]))

    def _emit_header(h):
        ir.append(_node("meta_header", [h, ""], _sec=sec))

    def _emit_blocks(blocks, text_type):
        nonlocal blk
        has_any = False
        for b in blocks:
            bt = b.get("type")
            if bt == "thinking":
                txt = _sanitize(b.get("thinking", ""))
                if not txt: continue
                ir.append(_node("meta", [">>>thinking"], _sec=sec, _blk=blk))
                ir.append(_node("thinking", txt.split("\n"), searchable=True,
                                 _sec=sec, _blk=blk))
                ir.append(_node("meta", ["<<<thinking"], _sec=sec, _blk=blk))
                blk += 1; has_any = True

            elif bt == "redacted_thinking":
                ir.append(_node("meta", [">>>redacted_thinking"], _sec=sec, _blk=blk))
                ir.append(_node("redacted_thinking",
                                 ["[content redacted by model provider]"],
                                 searchable=True, _sec=sec, _blk=blk))
                ir.append(_node("meta", ["<<<redacted_thinking"], _sec=sec, _blk=blk))
                blk += 1; has_any = True

            elif bt == "text":
                txt = _sanitize(b.get("text", ""))
                if not txt: continue
                ir.append(_node(text_type, txt.split("\n"), searchable=True,
                                 _sec=sec, _blk=blk))
                blk += 1; has_any = True

            elif bt == "tool_use":
                name = b.get("name", "unknown")
                tid = b.get("id", "")
                inp = b.get("input", {})
                hl = f">>>tool_call {name}:{_short_tid(tid)}"
                summary = _tool_summary(name, inp)
                ir.append(_node("meta", [hl], _sec=sec, _blk=blk,
                                 _tool_summary=summary))
                if inp:
                    ir.append(_node("tool_call", emit_mapping(inp).split("\n"),
                                     searchable=True, _sec=sec, _blk=blk))
                ir.append(_node("meta", ["<<<tool_call"], _sec=sec, _blk=blk))
                blk += 1; has_any = True

            elif bt == "image":
                src = b.get("source", {})
                if src.get("type") == "base64":
                    fn = (_extract_img(src, outdir, data_prefix, data_ctr[0], max_media_bytes,
                                       protected_inputs, media_writer)
                          if extract_media else "embedded image omitted in search-only mode")
                    data_ctr[0] += 1
                    ir.append(_node(f"{text_type}_image", [f"[image: {fn}]"],
                                     searchable=True, _sec=sec, _blk=blk))
                    blk += 1; has_any = True

            elif bt == "document":
                src = b.get("source", {})
                label = "[document]"
                if src.get("type") == "base64":
                    fn = (_extract_doc(src, outdir, data_prefix, data_ctr[0], max_media_bytes,
                                       protected_inputs, media_writer)
                          if extract_media else "embedded document omitted in search-only mode")
                    data_ctr[0] += 1
                    label = f"[document: {fn}]"
                ir.append(_node(f"{text_type}_document", [label],
                                 searchable=True, _sec=sec, _blk=blk))
                blk += 1; has_any = True
        return has_any

    for r in chain:
        rt = r.get("type")

        if rt == "system":
            if r.get("subtype") == "compact_boundary": continue
            content = r.get("content", "") or r.get("message", {}).get("content", "")
            if not content: continue
            _emit_sep(); _emit_header("[system]")
            if isinstance(content, list):
                _emit_blocks(content, "system")
            else:
                ir.append(_node("system", _sanitize(content).split("\n"), searchable=True,
                                 _sec=sec, _blk=blk))
                blk += 1
            sec += 1

        elif rt == "user":
            if r.get("isCompactSummary"):
                content = r.get("message", {}).get("content", "")
                nlines = content.count("\n") + 1 if content else 0
                _emit_sep(); _emit_header("[user]")
                ir.append(_node("user", [f"[compact summary — {nlines} lines]"], searchable=False,
                                 _sec=sec, _blk=blk))
                blk += 1; sec += 1
                continue
            content = r.get("message", {}).get("content", "")
            if isinstance(content, str):
                if content:
                    _emit_sep(); _emit_header("[user]")
                    ir.append(_node("user", _sanitize(content).split("\n"), searchable=True,
                                     _sec=sec, _blk=blk))
                    blk += 1; sec += 1
            elif isinstance(content, list):
                tblocks = [b for b in content if b.get("type") != "tool_result"]
                tresults = [b for b in content if b.get("type") == "tool_result"]
                if tblocks:
                    mark = len(ir)
                    saved_data = data_ctr[0]
                    saved_blk = blk
                    _emit_sep(); _emit_header("[user]")
                    if _emit_blocks(tblocks, "user"):
                        sec += 1
                    else:
                        del ir[mark:]
                        data_ctr[0] = saved_data
                        blk = saved_blk
                for tr in tresults:
                    tuid = tr.get("tool_use_id", "")
                    is_err = tr.get("is_error", False)
                    nm = tid_name.get(tuid, "unknown")
                    role = "tool_error" if is_err else "tool"
                    btype = "tool_error" if is_err else "tool_result"
                    _emit_sep(); _emit_header(f"[{role}] {nm}:{_short_tid(tuid)}")
                    tc = tr.get("content", "")
                    parts = []
                    if isinstance(tc, str):
                        parts.append(_preprocess_tool_text(tc, nm))
                    elif isinstance(tc, list):
                        for item in tc:
                            if item.get("type") == "text":
                                parts.append(_preprocess_tool_text(item.get("text", ""), nm))
                            elif item.get("type") == "image":
                                src = item.get("source", {})
                                if src.get("type") == "base64":
                                    fn = (_extract_img(src, outdir, data_prefix, data_ctr[0],
                                                       max_media_bytes, protected_inputs,
                                                       media_writer)
                                          if extract_media else "embedded image omitted in search-only mode")
                                    data_ctr[0] += 1
                                    parts.append(f"[image: {fn}]")
                            elif item.get("type") == "document":
                                src = item.get("source", {})
                                if src.get("type") == "base64":
                                    fn = (_extract_doc(src, outdir, data_prefix, data_ctr[0],
                                                       max_media_bytes, protected_inputs,
                                                       media_writer)
                                          if extract_media else "embedded document omitted in search-only mode")
                                    data_ctr[0] += 1
                                    parts.append(f"[document: {fn}]")
                    ir.append(_node(btype, _sanitize("\n\n".join(parts)).split("\n"),
                                     searchable=True, _sec=sec, _blk=blk))
                    blk += 1; sec += 1

        elif rt == "assistant":
            blocks = r.get("message", {}).get("content", [])
            has = any(
                (b.get("type") == "thinking" and b.get("thinking")) or
                b.get("type") == "redacted_thinking" or
                (b.get("type") == "text" and b.get("text")) or
                b.get("type") == "tool_use" or
                b.get("type") == "image" or
                b.get("type") == "document"
                for b in blocks)
            if has:
                _emit_sep(); _emit_header("[assistant]")
                _emit_blocks(blocks, "assistant")
                sec += 1

    ir.append(_node("meta", [""]))  # trailing newline
    return ir


# ── IR walk ──
