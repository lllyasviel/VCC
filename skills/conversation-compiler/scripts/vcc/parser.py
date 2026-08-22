"""Source detection, client normalization, JSONL parsing, and IR construction."""

import base64
import binascii
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime

from .normalizers import (
    DEEPSEEK_CORE_EVENT_TYPES, DEEPSEEK_IGNORED_EVENT_TYPES,
    DEEPSEEK_STORAGE_EVENT_TYPES,
    is_copilot_record, normalize_codex_record, normalize_copilot,
    normalize_deepseek,
)

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


def _discard(record):
    typ = record.get("type")
    return typ in _DISCARD_T or (
        typ == "system" and record.get("subtype") in _DISCARD_S
    )

def short_filename(fn):
    n, e = os.path.splitext(fn)
    return ("#" + n[-6:] + e) if len(n) > 12 else fn

def _short_tid(tid):
    return tid[-6:] if len(tid) > 6 else tid









_CODEX_SUPPORTED_RESPONSE_TYPES = {
    "message", "function_call", "custom_tool_call", "agent_message",
    "reasoning", "function_call_output", "custom_tool_call_output",
}
_CODEX_KNOWN_TOP_LEVEL_TYPES = {
    "session_meta", "event_msg", "response_item", "world_state",
    "turn_context", "inter_agent_communication_metadata", "compacted",
}
_COPILOT_KNOWN_TYPES = {
    "system.message", "user.message", "assistant.reasoning", "assistant.message",
    "tool.execution_start", "tool.user_requested", "tool.execution_complete",
    "session.compaction_complete", "session.error", "assistant.streaming_delta",
    "assistant.message_delta", "assistant.reasoning_delta", "assistant.turn_start",
    "assistant.turn_end", "assistant.intent", "assistant.usage",
    "assistant.tool_call_delta", "assistant.server_tool_progress",
    "tool.execution_partial_result", "tool.execution_progress",
    "permission.requested", "permission.completed", "session.idle",
    "session.compaction_start", "session.title_changed",
    "session.context_changed", "session.usage_info",
}
_CLAUDE_METADATA_TYPES = {"attachment", "mode"}
_CLAUDE_KNOWN_TYPES = {"system", "user", "assistant"} | _DISCARD_T | _CLAUDE_METADATA_TYPES
_CLAUDE_KNOWN_BLOCK_TYPES = {
    "thinking", "redacted_thinking", "text", "tool_use", "tool_result",
    "image", "document",
}


def _iter_source_records(path, tolerate_partial_tail, diagnostics):
    """Yield JSON objects while retaining only one raw source line."""
    def parse_line(line, line_number, final=False):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            if (tolerate_partial_tail and final and
                    not line.endswith(("\n", "\r"))):
                if diagnostics is not None:
                    diagnostics["partial_tail_ignored"] = True
                print(
                    f"warning: {path}:{line_number}: ignored incomplete live-session tail",
                    file=sys.stderr,
                )
                return None
            raise VCCError(
                f"{path}:{line_number}:{exc.colno}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(record, dict):
            raise VCCError(f"{path}:{line_number}: expected a JSON object")
        return record

    if str(path).endswith(".zstd"):
        try:
            import zstandard
        except ImportError as exc:
            raise VCCError(
                f"{path}: DeepSeek Harness .zstd logs require the optional 'zstandard' package"
            ) from exc
        binary = open(path, "rb")
        stream = zstandard.ZstdDecompressor().stream_reader(binary)
        source = io.TextIOWrapper(stream, encoding="utf-8")
    else:
        source = open(path, encoding="utf-8")

    pending = None
    with source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            if pending is not None:
                record = parse_line(*pending)
                if record is not None:
                    yield record
            pending = (line, line_number)
        if pending is not None:
            record = parse_line(*pending, final=True)
            if record is not None:
                yield record


def _copilot_source_class(record):
    typ = str(record.get("type", ""))
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    if typ == "assistant.message_delta":
        return "supported"
    if typ == "assistant.streaming_delta":
        return "supported" if any(
            key in data for key in ("content", "delta", "text")
        ) else "ignored"
    if typ in _COPILOT_KNOWN_TYPES:
        if typ not in {
            "system.message", "user.message", "assistant.reasoning",
            "assistant.message", "tool.execution_start", "tool.user_requested",
            "tool.execution_complete", "session.compaction_complete", "session.error",
        } or record.get("ephemeral"):
            return "ignored"
        return "supported"
    if typ.endswith("_delta"):
        return "ignored"
    return "unknown"


class _SourceDiagnostics:
    """Keep source-wide coverage counters without retaining source records."""
    def __init__(self, path):
        self.path = path
        self.total = 0
        self.types = Counter()
        self.response_types = Counter()
        self.copilot_classes = Counter()
        self.copilot_unknown = set()
        self.claude_unknown_blocks = set()
        self.normalized = 0
        self.boundaries = 0
        self.has_authoritative_compaction = False

    def observe_source(self, record):
        self.total += 1
        typ = record.get("type")
        self.types[typ] += 1
        if typ == "compacted":
            self.has_authoritative_compaction = True
        if typ == "response_item":
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            self.response_types[payload.get("type")] += 1
        copilot_class = _copilot_source_class(record)
        self.copilot_classes[copilot_class] += 1
        if copilot_class == "unknown":
            self.copilot_unknown.add(str(typ))
        if typ in {"system", "user", "assistant"}:
            content = record.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        self.claude_unknown_blocks.add("<non-object>")
                    elif block.get("type") not in _CLAUDE_KNOWN_BLOCK_TYPES:
                        self.claude_unknown_blocks.add(str(block.get("type")))

    def observe_normalized(self, records, client):
        if client == "claude":
            self.normalized += sum(
                1 for r in records
                if r.get("type") in {"system", "user", "assistant"} and not _discard(r)
            )
        else:
            self.normalized += len(records)
        self.boundaries += sum(
            1 for r in records
            if r.get("type") == "system" and r.get("subtype") == "compact_boundary"
        )

    def finish(self, client, diagnostics):
        if diagnostics is None:
            return
        diagnostics.update({
            "schema_version": 2,
            "source": os.path.abspath(self.path),
            "source_records_total": self.total,
            "partial_tail_ignored": diagnostics.get("partial_tail_ignored", False),
            "client": client,
            "normalized_records_emitted": self.normalized,
            "compaction_boundaries": self.boundaries,
        })
        if client == "deepseek":
            known = DEEPSEEK_CORE_EVENT_TYPES | DEEPSEEK_STORAGE_EVENT_TYPES
            ignored = DEEPSEEK_IGNORED_EVENT_TYPES
            unknown_count = sum(count for typ, count in self.types.items() if typ not in known)
            ignored_count = sum(self.types[typ] for typ in ignored)
            diagnostics.update({
                "source_records_supported": self.total - unknown_count - ignored_count,
                "source_records_ignored": ignored_count,
                "source_records_unknown": unknown_count,
                "unknown_types": sorted(str(typ) for typ in self.types if typ not in known),
            })
        elif client == "codex":
            unknown_response = {
                str(typ) for typ in self.response_types
                if typ not in _CODEX_SUPPORTED_RESPONSE_TYPES
            }
            unknown_top = {
                str(typ) for typ in self.types
                if typ not in _CODEX_KNOWN_TOP_LEVEL_TYPES
            }
            unknown_count = (
                sum(count for typ, count in self.response_types.items()
                    if typ not in _CODEX_SUPPORTED_RESPONSE_TYPES) +
                sum(count for typ, count in self.types.items()
                    if typ not in _CODEX_KNOWN_TOP_LEVEL_TYPES)
            )
            supported_count = sum(
                count for typ, count in self.response_types.items()
                if typ in _CODEX_SUPPORTED_RESPONSE_TYPES
            ) + self.types["compacted"]
            context_compacted = getattr(self, "codex_context_compacted", 0)
            if not self.has_authoritative_compaction:
                supported_count += context_compacted
            diagnostics.update({
                "source_records_supported": supported_count,
                "source_records_ignored": self.total - supported_count - unknown_count,
                "source_records_unknown": unknown_count,
                "unknown_types": sorted(unknown_response | unknown_top),
            })
        elif client == "copilot":
            diagnostics.update({
                "source_records_supported": self.copilot_classes["supported"],
                "source_records_ignored": self.copilot_classes["ignored"],
                "source_records_unknown": self.copilot_classes["unknown"],
                "unknown_types": sorted(self.copilot_unknown),
            })
        else:
            supported = sum(
                count for typ, count in self.types.items()
                if typ in {"system", "user", "assistant"}
            )
            # Discarded system subtypes need record-level accounting.
            supported -= getattr(self, "claude_discarded_conversation", 0)
            ignored = getattr(self, "claude_discarded", 0) + sum(
                self.types[typ] for typ in _CLAUDE_METADATA_TYPES
            )
            unknown = sum(
                count for typ, count in self.types.items()
                if typ not in _CLAUDE_KNOWN_TYPES
            )
            diagnostics.update({
                "source_records_supported": supported,
                "source_records_ignored": ignored,
                "source_records_unknown": unknown,
                "unknown_types": sorted(
                    str(typ) for typ in self.types if typ not in _CLAUDE_KNOWN_TYPES
                ),
                "unknown_content_block_types": sorted(self.claude_unknown_blocks),
            })


class _ChainCollector:
    """Incrementally merge assistant chunks and retain only selected chains."""
    def __init__(self, chain_window):
        self.chains = deque(maxlen=chain_window or None)
        self.current = []
        self.active_mid = None
        self.active_index = None
        self.total = 0

    def consume(self, record):
        if _discard(record):
            return
        if (record.get("type") == "system" and
                record.get("subtype") == "compact_boundary"):
            self._finish_chain()
            return
        if record.get("type") == "assistant":
            message = record.get("message", {})
            mid = message.get("id")
            if mid and mid == self.active_mid and self.active_index is not None:
                target = self.current[self.active_index]["message"]
                target["content"].extend(message.get("content", []))
                if message.get("stop_reason"):
                    target["stop_reason"] = message["stop_reason"]
                return
            self.current.append(record)
            if mid:
                self.active_mid = mid
                self.active_index = len(self.current) - 1
            else:
                self.active_mid = None
                self.active_index = None
            return
        self.current.append(record)
        self.active_mid = None
        self.active_index = None

    def _finish_chain(self):
        if self.current:
            self.chains.append(self.current)
            self.total += 1
        self.current = []
        self.active_mid = None
        self.active_index = None

    def finish(self):
        self._finish_chain()
        first_index = self.total - len(self.chains) + 1
        return list(enumerate(self.chains, first_index)), self.total


class _StreamingNormalizer:
    """Use the smallest client-safe buffer: record, turn, or DeepSeek turn."""
    def __init__(self, client, emit, path):
        self.client = client
        self.emit = emit
        self.path = path
        self.buffer = []
        self.deepseek_has_user = False
        self.codex_authoritative_seen = False

    def feed(self, record):
        typ = record.get("type")
        if self.client == "claude":
            self.emit([record])
        elif self.client == "codex":
            if typ == "compacted":
                self.codex_authoritative_seen = True
                self.emit(normalize_codex_record(record))
            elif (typ == "event_msg" and
                  record.get("payload", {}).get("type") == "context_compacted" and
                  self.codex_authoritative_seen):
                self.emit([])
            else:
                self.emit(normalize_codex_record(record))
        elif self.client == "copilot":
            if typ == "user.message" and self.buffer:
                self._flush()
            self.buffer.append(record)
            if typ in {"assistant.turn_end", "session.compaction_complete", "session.idle"}:
                self._flush()
        else:
            if typ == "turn/start" and self.buffer:
                self._flush()
            if typ == "user/message" and self.deepseek_has_user:
                self._flush()
            self.buffer.append(record)
            if typ == "user/message":
                self.deepseek_has_user = True
            if typ in {"turn/end", "compaction/start", "compaction/end"}:
                self._flush()

    def _flush(self):
        if not self.buffer:
            return
        try:
            if self.client == "copilot":
                normalized = normalize_copilot(self.buffer)
            else:
                normalized = normalize_deepseek(self.buffer)
        except ValueError as exc:
            raise VCCError(f"{self.path}: {exc}") from exc
        self.buffer = []
        self.deepseek_has_user = False
        self.emit(normalized)

    def finish(self):
        self._flush()


def load_chains(path, chain_window=0, tolerate_partial_tail=True, diagnostics=None):
    """Stream a source into merged chains, retaining at most ``chain_window`` chains."""
    stats = _SourceDiagnostics(path)
    collector = _ChainCollector(chain_window)
    client = None
    pending = []
    normalizer = None

    def emit(records):
        stats.observe_normalized(records, client)
        for record in records:
            collector.consume(record)

    def start(selected_client):
        nonlocal client, normalizer, pending
        client = selected_client
        normalizer = _StreamingNormalizer(client, emit, path)
        buffered, pending = pending, []
        for item in buffered:
            normalizer.feed(item)

    for record in _iter_source_records(path, tolerate_partial_tail, diagnostics):
        stats.observe_source(record)
        if (record.get("type") == "event_msg" and
                record.get("payload", {}).get("type") == "context_compacted"):
            stats.codex_context_compacted = getattr(stats, "codex_context_compacted", 0) + 1
        if _discard(record):
            stats.claude_discarded = getattr(stats, "claude_discarded", 0) + 1
            if record.get("type") in {"system", "user", "assistant"}:
                stats.claude_discarded_conversation = getattr(
                    stats, "claude_discarded_conversation", 0
                ) + 1
        if client is not None:
            normalizer.feed(record)
            continue
        pending.append(record)
        typ = record.get("type")
        if len(pending) == 1 and typ == "session":
            start("deepseek")
        elif typ == "response_item":
            start("codex")
        elif is_copilot_record(record):
            start("copilot")
        elif typ in _CLAUDE_KNOWN_TYPES:
            start("claude")

    if client is None:
        start("claude")
    normalizer.finish()
    indexed_chains, total_chains = collector.finish()
    stats.finish(client, diagnostics)
    return indexed_chains, total_chains


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
    active_timestamp = None

    tid_name = {}
    for r in chain:
        if r.get("type") == "assistant":
            for b in r.get("message", {}).get("content", []):
                if b.get("type") == "tool_use":
                    tid_name[b.get("id", "")] = b.get("name", "unknown")

    def _emit_sep():
        if sec > 0:
            ir.append(_node("meta", ["", SEP]))

    def _emit_header(h, **metadata):
        ir.append(_node(
            "meta_header", [h, ""], _sec=sec,
            _event_timestamp=active_timestamp,
            **metadata,
        ))

    def _emit_blocks(blocks, text_type):
        nonlocal blk
        has_any = False
        for b in blocks:
            if not isinstance(b, dict):
                b = {"type": "<non-object>", "value": b}
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
                                 _tool_summary=summary, _tool_id=tid))
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
            else:
                label = f"[unsupported content block: {bt}]"
                payload = json.dumps(b, ensure_ascii=False, indent=2, default=str)
                ir.append(_node(
                    f"{text_type}_unknown", [label] + _sanitize(payload).split("\n"),
                    searchable=True, _sec=sec, _blk=blk,
                ))
                blk += 1; has_any = True
        return has_any

    for r in chain:
        active_timestamp = r.get("timestamp")
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
                tblocks = [b for b in content
                           if not isinstance(b, dict) or b.get("type") != "tool_result"]
                tresults = [b for b in content
                            if isinstance(b, dict) and b.get("type") == "tool_result"]
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
                    _emit_sep(); _emit_header(
                        f"[{role}] {nm}:{_short_tid(tuid)}", _tool_id=tuid)
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
                            else:
                                parts.append(
                                    f"[unsupported tool result block: {item.get('type')}]\n" +
                                    json.dumps(item, ensure_ascii=False, indent=2, default=str)
                                )
                    ir.append(_node(btype, _sanitize("\n\n".join(parts)).split("\n"),
                                     searchable=True, _sec=sec, _blk=blk))
                    blk += 1; sec += 1

        elif rt == "assistant":
            blocks = r.get("message", {}).get("content", [])
            has = bool(blocks)
            if has:
                _emit_sep(); _emit_header("[assistant]")
                _emit_blocks(blocks, "assistant")
                sec += 1

    ir.append(_node("meta", [""]))  # trailing newline
    return ir


# ── IR walk ──
