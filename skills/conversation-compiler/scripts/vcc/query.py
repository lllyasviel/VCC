"""Structured block matching, ranking, limiting, and result emission."""

import json
import os

def _rel_path(fp):
    try:
        return os.path.relpath(fp)
    except ValueError:
        return os.path.abspath(fp)

_ROLE_SCORE = {
    "user": 10,
    "assistant": 8,
    "thinking": 6,
    "tool_call": 4,
    "tool_result": 2,
    "tool_error": 2,
    "system": 1,
}


def _query_matches(lines, patterns, match_mode):
    text = "\n".join(lines)
    hits = [p for p in patterns if p.search(text)]
    accepted = len(hits) == len(patterns) if match_mode == "all" else bool(hits)
    return accepted, hits


def collect_search_matches(results, patterns, match_mode="any"):
    """Return stable machine-readable block matches in discovery order."""
    matches = []
    for result in reversed(results):
        filepath, ir, source_path = result
        short = _rel_path(filepath)
        for o in reversed(ir):
            if not o["searchable"]:
                continue
            accepted, hits = _query_matches(o["content"], patterns, match_mode)
            if not accepted:
                continue
            start = o.get("start_line", 0) + 1
            end = start + len(o["content"]) - 1
            matching_lines = []
            for offset, line in enumerate(o["content"]):
                if any(pattern.search(line) for pattern in hits):
                    matching_lines.append({"line": start + offset, "text": line})
            role = o["type"]
            matches.append({
                "schema_version": 1,
                "source": os.path.abspath(source_path),
                "reference": short,
                "role": role,
                "line_start": start,
                "line_end": end,
                "matched_patterns": [pattern.pattern for pattern in hits],
                "score": _ROLE_SCORE.get(role, 0) + 2 * len(hits),
                "lines": matching_lines,
            })
    return matches


def limit_matches(matches, limit):
    if not limit:
        return matches
    grouped, order = {}, []
    for match in matches:
        source = match["source"]
        if source not in grouped:
            grouped[source] = []
            order.append(source)
        grouped[source].append(match)
    limited = []
    for source in order:
        ranked = sorted(grouped[source], key=lambda item: (-item["score"], item["line_start"]))
        limited.extend(ranked[:limit])
    return limited


def emit_search_results(results, patterns, match_mode="any", output_format="text",
                per_source_limit=0):
    matches = limit_matches(
        collect_search_matches(results, patterns, match_mode), per_source_limit)
    if output_format == "json":
        print(json.dumps(matches, ensure_ascii=False, indent=2))
        return matches
    if output_format == "ndjson":
        for match in matches:
            print(json.dumps(match, ensure_ascii=False))
        return matches
    for index, match in enumerate(matches):
        if index:
            print()
        print(
            f"({match['reference']}:{match['line_start']}-{match['line_end']}) "
            f"[{match['role']}]"
        )
        for line in match["lines"]:
            print(f"  {line['line']}: {line['text']}")
    return matches


# ── compile ──
