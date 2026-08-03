"""Shared constants, errors, and small text utilities."""

import re

VCC_VERSION = "2.3.1"
DEFAULT_MAX_MEDIA_BYTES = 64 * 1024 * 1024


class VCCError(Exception):
    """Expected input or parsing failure suitable for concise CLI reporting."""

# ── dict emitter ──

def emit_mapping(data, indent=0):
    parts = []
    prefix = " " * indent
    for k, v in data.items():
        if isinstance(v, dict):
            parts.append(prefix + k + ":")
            parts.append(emit_mapping(v, indent + 2))
        elif isinstance(v, list):
            parts.append(prefix + k + ":")
            for item in v:
                if isinstance(item, dict):
                    parts.append(prefix + "  -")
                    parts.append(emit_mapping(item, indent + 4))
                else:
                    parts.append(prefix + "  - " + str(item))
        else:
            s = str(v)
            if "\n" in s:
                parts.append(prefix + k + ": |")
                p = " " * (indent + 2)
                for line in s.split("\n"):
                    parts.append((p + line) if line else "")
            else:
                parts.append(prefix + k + ": " + s)
    return "\n".join(parts)

# ── tokenizer ──

_TOK_RE = re.compile(
    r'[a-zA-Z]+'           # letters (grouped)
    r'|[0-9]+'             # digits (grouped)
    r'|[^\sa-zA-Z0-9]'     # single char: any non-whitespace non-letter non-digit
    r'|\s+'                # whitespace (preserved, not counted)
)
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

def tokenize(text):
    return _TOK_RE.findall(text)

# ── truncation (token-based) ──

def truncate_text(text, limit, ref=""):
    if not limit or not text:
        return text
    tokens = tokenize(text)
    count, cut = 0, len(tokens)
    for i, t in enumerate(tokens):
        if t.strip():
            count += 1
            if count > limit:
                cut = i
                break
    if cut >= len(tokens):
        return text
    return "".join(tokens[:cut]) + (f"...(truncated from {ref})" if ref else "...(truncated)")

# ── match lines ──

def match_lines(lines, regex, ref_fn="x.txt", start_line=1):
    if not lines:
        return []
    end_line = start_line + len(lines) - 1
    from_ref = f"...(from {ref_fn}:{start_line}-{end_line})"

    matched = []
    for i, line in enumerate(lines):
        if regex.search(line):
            matched.append((start_line + i, line))
    if not matched:
        return [from_ref]

    block_ref = f"({ref_fn}:{start_line}-{end_line})"
    result = [block_ref]
    for ln, lt in matched:
        result.append(f"  {ln}: {lt}")
    return result
