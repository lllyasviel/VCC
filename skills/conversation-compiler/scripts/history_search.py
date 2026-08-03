#!/usr/bin/env python3
"""Deterministic tiered search across supported local agent histories."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


CLIENTS = ("copilot", "codex", "claude")


def default_roots():
    home = Path.home()
    return {
        "copilot": Path(os.environ.get("COPILOT_HOME", home / ".copilot")) / "session-state",
        "codex": Path(os.environ.get("CODEX_HOME", home / ".codex")) / "sessions",
        "claude": home / ".claude" / "projects",
    }


def parse_root_overrides(values, roots):
    for value in values:
        if "=" not in value:
            raise ValueError(f"invalid --root {value!r}; expected CLIENT=PATH")
        client, path = value.split("=", 1)
        if client not in CLIENTS:
            raise ValueError(f"unsupported client in --root: {client}")
        roots[client] = Path(path).expanduser()


def enumerate_sessions(client, root, since_days=None, path_contains=None, max_files=0):
    if not root.is_dir():
        return []
    if client == "copilot":
        paths = root.glob("*/events.jsonl")
    elif client == "codex":
        paths = root.glob("**/rollout-*.jsonl")
    else:
        paths = root.glob("**/*.jsonl")
    selected = sorted(
        (path for path in paths if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if since_days is not None:
        cutoff = time.time() - since_days * 86400
        selected = [path for path in selected if path.stat().st_mtime >= cutoff]
    for fragment in path_contains or []:
        selected = [path for path in selected if fragment in str(path)]
    return selected[:max_files] if max_files else selected


def chunks(items, max_items=64, max_chars=24000):
    """Bound both item count and argv size for Windows CreateProcess portability."""
    batch, chars = [], 0
    for item in items:
        cost = len(str(item)) + 3
        if batch and (len(batch) >= max_items or chars + cost > max_chars):
            yield batch
            batch, chars = [], 0
        batch.append(item)
        chars += cost
    if batch:
        yield batch


def run_vcc(vcc_path, paths, terms, match_mode, ignore_case):
    matches, errors, warnings = [], [], []
    for batch in chunks(paths):
        command = [sys.executable, str(vcc_path), *(str(path) for path in batch),
                   "--search-only", "--format", "ndjson", "--match", match_mode,
                   "--max-matches-per-input", "3"]
        for term in terms:
            command.extend(("--term", term))
        if ignore_case:
            command.append("--ignore-case")
        try:
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
        except OSError as exc:
            errors.append(f"failed to start VCC search batch: {exc}")
            continue
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            try:
                matches.append(json.loads(line))
            except json.JSONDecodeError:
                errors.append(f"unparseable VCC output: {line[:200]}")
        for line in completed.stderr.splitlines():
            if line.startswith("warning:"):
                warnings.append(line)
            elif line.strip():
                errors.append(line)
    return matches, errors, warnings


def emit(report, output_format):
    if output_format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if output_format == "ndjson":
        print(json.dumps({key: value for key, value in report.items() if key != "results"},
                         ensure_ascii=False))
        for result in report["results"]:
            print(json.dumps(result, ensure_ascii=False))
        return
    print(f"scope: {report['scope']}")
    print(f"tiers searched: {', '.join(report['tiers_searched']) or 'none'}")
    print(f"expansion: {report['expansion_reason'] or 'not needed'}")
    for result in report["results"]:
        print(
            f"[{result['client']}] score={result['score']} {result['source']} "
            f"{result['role']}:{result['line_start']}-{result['line_end']}"
        )
        for line in result["lines"]:
            print(f"  {line['line']}: {line['text']}")
    if report["errors"]:
        print(f"partial errors: {len(report['errors'])}", file=sys.stderr)
        for error in report["errors"]:
            print(f"  {error}", file=sys.stderr)
    for warning in report["warnings"]:
        print(f"  {warning}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="VCC.py history-search",
        description="Search supported local histories in deterministic source-priority tiers",
    )
    parser.add_argument("query", help="natural-language search anchors")
    parser.add_argument("--client", action="append", choices=("auto", "all", *CLIENTS),
                        help="source client; repeat to select multiple (default: auto)")
    parser.add_argument("--current-client", choices=CLIENTS,
                        help="explicit runtime client used by auto source priority")
    parser.add_argument("--current-session", type=Path,
                        help="exact current JSONL path to search before any history root")
    parser.add_argument("--exclude-session", action="append", default=[], type=Path,
                        help="exclude an exact JSONL path, for example the active search task")
    parser.add_argument("--root", action="append", default=[], metavar="CLIENT=PATH",
                        help="override one history root; primarily for portable installations/tests")
    parser.add_argument("--query-mode", choices=("phrase", "all", "any"), default="all")
    parser.add_argument("--case-sensitive", action="store_true")
    parser.add_argument("--expand-on", choices=("no-match", "weak", "always"), default="weak")
    parser.add_argument("--strong-score", type=int, default=12)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--since-days", type=int,
                        help="search only files modified within this many days")
    parser.add_argument("--path-contains", action="append", default=[], metavar="TEXT",
                        help="require this literal fragment in candidate paths; repeatable")
    parser.add_argument("--max-files-per-client", type=int, default=0,
                        help="cap newest candidate files per client; 0 means unlimited")
    parser.add_argument("--format", choices=("text", "json", "ndjson"), default="json")
    args = parser.parse_args(argv)
    if (args.limit < 1 or args.strong_score < 0 or
            (args.since_days is not None and args.since_days < 0) or
            args.max_files_per_client < 0):
        parser.error("limits, scores, and time windows must be non-negative; --limit must be positive")

    roots = default_roots()
    try:
        parse_root_overrides(args.root, roots)
    except ValueError as exc:
        parser.error(str(exc))

    requested = args.client or ["auto"]
    if "all" in requested:
        explicit = list(CLIENTS)
        scope = "all"
    else:
        explicit = [client for client in requested if client != "auto"]
        scope = "explicit" if explicit else "auto"
    current = args.current_client or os.environ.get("VCC_CURRENT_CLIENT")
    if current not in CLIENTS:
        current = None

    if args.query_mode == "phrase":
        terms, match_mode = [args.query], "all"
    else:
        terms = [term for term in args.query.split() if term]
        match_mode = args.query_mode
    if not terms:
        parser.error("query must contain at least one non-whitespace term")

    if explicit:
        tiers = [explicit]
    elif current:
        tiers = [[current], [client for client in CLIENTS if client != current]]
    else:
        tiers = [list(CLIENTS)]

    vcc_path = Path(__file__).with_name("VCC.py")
    all_matches, errors, warnings, searched, absent_roots = [], [], [], [], []
    expansion_reason = None
    exact = args.current_session.expanduser() if args.current_session else None
    excluded = {path.expanduser().resolve() for path in args.exclude_session}
    use_exact = bool(exact and (not explicit or (current is not None and current in explicit)))
    if exact and not use_exact:
        warnings.append(
            f"current session skipped because explicit client scope excludes {current}"
        )
    if use_exact:
        if exact.is_file():
            matches, batch_errors, batch_warnings = run_vcc(
                vcc_path, [exact], terms, match_mode, not args.case_sensitive)
            for match in matches:
                match["client"] = current or "current"
                match["tier"] = "current-session"
            all_matches.extend(matches)
            errors.extend(batch_errors)
            warnings.extend(batch_warnings)
            searched.append("current-session")
        else:
            errors.append(f"current session does not exist: {exact}")

    exact_strong = any(match["score"] >= args.strong_score for match in all_matches)
    if not exact_strong:
        for tier_index, clients in enumerate(tiers):
            if not clients:
                continue
            tier_matches = []
            for client in clients:
                paths = enumerate_sessions(
                    client, roots[client], args.since_days,
                    args.path_contains, args.max_files_per_client)
                searched.append(client)
                if not roots[client].is_dir():
                    absent_roots.append(client)
                if use_exact:
                    paths = [path for path in paths if path.resolve() != exact.resolve()]
                paths = [path for path in paths if path.resolve() not in excluded]
                matches, batch_errors, batch_warnings = run_vcc(
                    vcc_path, paths, terms, match_mode, not args.case_sensitive)
                for match in matches:
                    match["client"] = client
                    match["tier"] = tier_index + 1
                tier_matches.extend(matches)
                errors.extend(batch_errors)
                warnings.extend(batch_warnings)
            all_matches.extend(tier_matches)
            best = max((match["score"] for match in tier_matches), default=-1)
            should_expand = (
                args.expand_on == "always" or
                (args.expand_on == "no-match" and not tier_matches) or
                (args.expand_on == "weak" and best < args.strong_score)
            )
            if tier_index + 1 < len(tiers) and should_expand:
                expansion_reason = "no match" if not tier_matches else f"best score {best} is weak"
                continue
            break

    all_matches.sort(key=lambda match: (-match["score"], match["source"], match["line_start"]))
    for match in all_matches:
        match["strength"] = "strong" if match["score"] >= args.strong_score else "weak"
    report = {
        "schema_version": 1,
        "scope": scope,
        "current_client": current,
        "tiers_searched": searched,
        "expansion_reason": expansion_reason,
        "query": {"terms": terms, "match": match_mode,
                  "case_sensitive": args.case_sensitive},
        "scoring": {"strong_score": args.strong_score,
                    "note": "ranking selects candidates; materialized context proves conclusions"},
        "filters": {"since_days": args.since_days,
                    "path_contains": args.path_contains,
                    "max_files_per_client": args.max_files_per_client,
                    "excluded_sessions": [str(path) for path in sorted(excluded)]},
        "results": all_matches[:args.limit],
        "errors": errors,
        "warnings": warnings,
        "absent_roots": absent_roots,
    }
    emit(report, args.format)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
