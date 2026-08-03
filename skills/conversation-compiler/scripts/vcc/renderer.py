"""IR line assignment and full, brief, and focused view rendering."""

import re

from .common import truncate_text, match_lines
from .parser import SEP, short_filename

def _is_tool_summary(o):
    return o.get("_tool_summary") is not None

def _walk(ir, key="content"):
    prev_blk = None
    prev_o = None
    for o in ir:
        c = o.get(key)
        if c is None:
            continue
        if not c:
            continue
        blk = o.get("_blk")
        if blk is not None and prev_blk is not None and blk != prev_blk:
            if key == "content":
                blank = True
            else:
                # non-content: suppress blank between consecutive tool summaries
                blank = not (_is_tool_summary(prev_o) and _is_tool_summary(o))
        else:
            blank = False
        yield o, c, blank
        if blk is not None:
            prev_blk = blk
            prev_o = o
        elif SEP in o.get("content", []):
            prev_blk = None
            prev_o = None


# ── line assignment ──

def assign_lines(ir):
    line = 0
    for o, c, blank in _walk(ir, "content"):
        if blank: line += 1
        o["start_line"] = line
        line += len(c)
        o["end_line"] = line - 1
    return line


# ── lowering helpers ──

def _is_truncatable(o):
    t = o["type"]
    if t in ("meta", "meta_header", "thinking", "redacted_thinking"):
        return False
    if t.endswith("_image") or t.endswith("_document"):
        return False
    return True

def _is_thinking(o):
    return o["type"] in ("thinking", "redacted_thinking")

def _sec_roles(ir):
    """Map sec -> role from meta_header content."""
    roles = {}
    for o in ir:
        if o["type"] == "meta_header":
            s = o.get("_sec")
            if s is None: continue
            h = o["content"][0]
            if h.startswith("[tool_error]"):
                roles[s] = "tool_error"
            elif h.startswith("[tool]"):
                roles[s] = "tool"
            elif h.startswith("[assistant]"):
                roles[s] = "assistant"
            elif h.startswith("[user]"):
                roles[s] = "user"
            elif h.startswith("[system]"):
                roles[s] = "system"
    return roles


# ── brief-mode filtering ──

_BRIEF_STRIP_RE = re.compile(
    r'<(ide_opened_file|ide_selection|system-reminder|command-message)>.*?</\1>\s*',
    re.DOTALL)
_BRIEF_UNWRAP_RE = re.compile(r'</?(?:command-name|command-args)>')
_META_USER_RE = re.compile(
    r'^\s*<(?:task-notification|local-command-caveat|local-command-stdout'
    r'|local-command-stderr)\b')
_SKILL_USER_RE = re.compile(r'^Base directory for this skill:')
_BRIEF_HIDE_TOOLS = {"TodoWrite", "ToolSearch"}
_BRIEF_HIDE_EXACT = {"Continue from where you left off.", "No response requested."}

def _strip_noise_xml(text):
    """Strip known noise XML from user text for brief mode."""
    text = _BRIEF_STRIP_RE.sub('', text)
    text = _BRIEF_UNWRAP_RE.sub('', text)
    return text.strip()

def _user_hidden_in_brief(nodes):
    """Check if a user section should be entirely hidden in brief mode."""
    blocks = [o for o in nodes if o.get("searchable")]
    if not blocks:
        return False
    for o in blocks:
        text = "\n".join(o["content"]).strip()
        if not text:
            continue
        if _META_USER_RE.match(text):
            continue
        if _SKILL_USER_RE.match(text):
            continue
        if not _strip_noise_xml(text):
            continue
        return False
    return True

def _section_hidden_exact(nodes):
    """Check if all searchable content in a section is an exact-match hide string."""
    blocks = [o for o in nodes if o.get("searchable")
              and o["type"] not in ("thinking", "redacted_thinking")]
    if not blocks:
        return False
    for o in blocks:
        text = "\n".join(o["content"]).strip()
        if text not in _BRIEF_HIDE_EXACT:
            return False
    return True


# ── lowering: brief ──

def _tid_result_ranges(ir):
    """Map short_tid → (start_line, end_line) for tool_result sections."""
    # Find which sec corresponds to which short_tid
    tid_sec = {}
    for o in ir:
        if o["type"] == "meta_header":
            c = o["content"]
            if c and len(c) >= 1:
                h = c[0]
                # [tool] name:AABBCC or [tool_error] name:AABBCC
                if h.startswith("[tool]") or h.startswith("[tool_error]"):
                    parts = h.split(":")
                    if len(parts) >= 2:
                        tid_sec[parts[-1]] = o.get("_sec")
    # Collect line ranges per sec
    sec_range = {}
    for o in ir:
        s = o.get("_sec")
        if s is None:
            continue
        sl = o.get("start_line")
        el = o.get("end_line")
        if sl is not None and el is not None:
            if s not in sec_range:
                sec_range[s] = [sl, el]
            else:
                sec_range[s][0] = min(sec_range[s][0], sl)
                sec_range[s][1] = max(sec_range[s][1], el)
    # Build tid → range (only content nodes, skip the separator/header)
    result = {}
    for tid, sec in tid_sec.items():
        if sec in sec_range:
            result[tid] = tuple(sec_range[sec])
    return result


def build_brief_view(ir, truncate, filename="", truncate_user=256):
    short = short_filename(filename)
    roles = _sec_roles(ir)
    tid_ranges = _tid_result_ranges(ir)
    section_nodes = {}
    for node in ir:
        sec = node.get("_sec")
        if sec is not None:
            section_nodes.setdefault(sec, []).append(node)

    # Sections visible in truncation: not tool/tool_error/system
    visible_secs = {sec for sec, role in roles.items()
                     if role not in ("tool", "tool_error", "system")}

    # Track which secs only have thinking content (no visible non-thinking blocks)
    sec_has_nonthink = set()
    for o in ir:
        s = o.get("_sec")
        if s is None: continue
        if o["type"] not in ("meta", "meta_header", "thinking", "redacted_thinking"):
            sec_has_nonthink.add(s)

    # Sections that are all-thinking should still be hidden in truncation
    # (they have no visible content after thinking is removed)
    for sec in list(visible_secs):
        if sec not in sec_has_nonthink:
            visible_secs.discard(sec)
        elif roles.get(sec) == "user" and _user_hidden_in_brief(section_nodes.get(sec, [])):
            visible_secs.discard(sec)
        elif _section_hidden_exact(section_nodes.get(sec, [])):
            visible_secs.discard(sec)

    # Merge adjacent visible assistant sections in one pass.
    merge_secs = set()
    previous_visible_role = None
    for cur_sec in sorted(roles):
        if cur_sec not in visible_secs:
            continue
        role = roles.get(cur_sec)
        if role == "assistant" and previous_visible_role == "assistant":
            merge_secs.add(cur_sec)
        previous_visible_role = role

    next_section = [None] * len(ir)
    following = None
    for idx in range(len(ir) - 1, -1, -1):
        next_section[idx] = following
        sec = ir[idx].get("_sec")
        if sec is not None:
            following = sec
    first_visible_sec = min(visible_secs, default=None)

    for idx, o in enumerate(ir):
        s = o.get("_sec")

        # Separator: replace with blank line in brief mode
        if s is None and o["type"] == "meta" and SEP in o.get("content", []):
            ns = next_section[idx]
            if ns is None or ns not in visible_secs:
                o["content_brief"] = None
            elif ns in merge_secs:
                o["content_brief"] = None
            elif first_visible_sec is None or ns <= first_visible_sec:
                o["content_brief"] = None
            else:
                o["content_brief"] = [""]
            continue

        # Section not visible → hide
        if s is not None and s not in visible_secs:
            o["content_brief"] = None
            continue

        # Merged assistant: hide separator and header
        if s in merge_secs and o["type"] == "meta_header":
            o["content_brief"] = None
            continue

        # Thinking / redacted_thinking → hide (including their >>> <<< metas)
        if _is_thinking(o):
            o["content_brief"] = None
            continue
        if o["type"] == "meta":
            c = o["content"]
            if c and (c[0].startswith(">>>thinking") or c[0].startswith("<<<thinking") or
                      c[0].startswith(">>>redacted_thinking") or c[0].startswith("<<<redacted_thinking")):
                o["content_brief"] = None
                continue

        # Tool_call three-piece: collapse to single-line summary with line ref
        if o["type"] == "meta" and o.get("content", []):
            c0 = o["content"][0]
            if c0.startswith(">>>tool_call"):
                # Hide noise tools (internal bookkeeping)
                tool_name = c0.split()[1].split(":")[0] if len(c0.split()) > 1 else ""
                if tool_name in _BRIEF_HIDE_TOOLS:
                    o["content_brief"] = None
                    continue
                summary = o.get("_tool_summary", f"* unknown")
                s = o.get("start_line")
                e = o.get("end_line")
                for j in (idx + 1, idx + 2):
                    if j < len(ir) and ir[j].get("_blk") == o.get("_blk"):
                        je = ir[j].get("end_line")
                        if je is not None:
                            e = je
                if s is not None and e is not None:
                    # Extract short_tid and look up tool_result range
                    parts = c0.split()[1].split(":") if len(c0.split()) > 1 else []
                    stid = parts[-1] if len(parts) >= 2 else ""
                    rr = tid_ranges.get(stid)
                    if rr:
                        summary = f"{summary} ({short}:{s+1}-{e+1},{rr[0]+1}-{rr[1]+1})"
                    else:
                        summary = f"{summary} ({short}:{s+1}-{e+1})"
                o["content_brief"] = [summary]
                continue
            if c0 == "<<<tool_call":
                o["content_brief"] = None
                continue

        if o["type"] == "tool_call":
            o["content_brief"] = None
            continue

        # meta_header → copy
        if o["type"] == "meta_header":
            o["content_brief"] = list(o["content"])
            continue

        # meta → copy
        if o["type"] == "meta":
            o["content_brief"] = list(o["content"])
            continue

        # Truncatable content
        if _is_truncatable(o):
            node_start = o.get("start_line", 0) + 1
            node_end = o.get("end_line", o.get("start_line", 0)) + 1
            ref = f"{short}:{node_start}-{node_end}"
            text = "\n".join(o["content"])
            if o["type"] == "user":
                text = _strip_noise_xml(text)
                if not text.strip():
                    o["content_brief"] = None
                    continue
            lim = truncate_user if o["type"] == "user" else truncate
            lines = (truncate_text(text, lim, ref) if lim else text).split("\n")
            # Strip leading blank lines
            while lines and not lines[0]:
                lines.pop(0)
            o["content_brief"] = lines
            continue

        # Non-truncatable (images, documents, etc) → copy
        o["content_brief"] = list(o["content"])


# ── lowering: view ──

def build_search_view(ir, filename="", grep_pattern=None):
    if not grep_pattern:
        # No grep: view is same as truncated (shouldn't normally be called)
        for o in ir:
            o["content_view"] = o.get("content_brief")
        return

    short = short_filename(filename)

    # Per-node match check
    def _node_matches(o):
        if not o["searchable"]:
            return False
        for line in o["content"]:
            if grep_pattern.search(line):
                return True
        return False

    node_matches = [_node_matches(o) for o in ir]

    # Pass 1: determine visibility for each searchable block
    block_visible = {}  # blk -> bool
    for idx, o in enumerate(ir):
        blk = o.get("_blk")
        if blk is None or blk in block_visible:
            continue
        if node_matches[idx]:
            block_visible[blk] = True

    # Derive which sections have any visible block (for header/separator logic)
    sec_has_visible = set()
    for o in ir:
        blk = o.get("_blk")
        if blk is not None and block_visible.get(blk):
            s = o.get("_sec")
            if s is not None:
                sec_has_visible.add(s)

    next_section = [None] * len(ir)
    following = None
    for idx in range(len(ir) - 1, -1, -1):
        next_section[idx] = following
        sec = ir[idx].get("_sec")
        if sec is not None:
            following = sec
    visible_before = [False] * len(ir)
    seen_visible = False
    for idx, o in enumerate(ir):
        visible_before[idx] = seen_visible
        if o.get("_sec") in sec_has_visible:
            seen_visible = True

    # Pass 2: set content_view for each node
    for idx, o in enumerate(ir):
        s = o.get("_sec")
        blk = o.get("_blk")

        # Separator: show only between two sections that have visible blocks
        if s is None and o["type"] == "meta" and SEP in o.get("content", []):
            next_vis = next_section[idx] in sec_has_visible
            prev_vis = visible_before[idx]
            o["content_view"] = list(o["content"]) if (next_vis and prev_vis) else None
            continue

        # meta_header: show if section has any visible block
        if o["type"] == "meta_header":
            o["content_view"] = list(o["content"]) if s in sec_has_visible else None
            continue

        # Thinking / tool_call metas: show if same blk matched
        if o["type"] == "meta" and o.get("content", []):
            c0 = o["content"][0]
            if c0.startswith(">>>thinking") or c0.startswith("<<<thinking") or \
               c0.startswith(">>>redacted_thinking") or c0.startswith("<<<redacted_thinking") or \
               c0.startswith(">>>tool_call ") or c0 == "<<<tool_call":
                o["content_view"] = list(o["content"]) if block_visible.get(blk) else None
                continue

        # Other meta → show if section has visible blocks
        if o["type"] == "meta":
            o["content_view"] = list(o["content"]) if s in sec_has_visible else None
            continue

        # Searchable content blocks: show only if this block matches
        if o["searchable"]:
            if node_matches[idx]:
                node_start = o.get("start_line", 0) + 1
                o["content_view"] = match_lines(
                    o["content"], grep_pattern, short, node_start)
            else:
                o["content_view"] = None
            continue

        # Non-searchable (images, docs, etc) → hide
        o["content_view"] = None


# ── codegen ──

def render_lines(ir, key="content"):
    lines = []
    for o, c, blank in _walk(ir, key):
        if blank: lines.append("")
        lines.extend(c)
    return lines


# ── grep ──
