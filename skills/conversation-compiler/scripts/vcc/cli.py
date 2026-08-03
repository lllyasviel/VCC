"""Command-line argument validation and multi-input orchestration."""

import argparse
import glob as globmod
import json
import os
import re
import sys
import tempfile

from .cache import (
    cache_is_valid,
    default_cache_root,
    managed_artifact_names,
    protect_cache_tree,
    prepare_cache_output_dir,
    remove_obsolete_managed_artifacts,
    report_cache_hit,
    write_cache_metadata,
)
from .common import DEFAULT_MAX_MEDIA_BYTES, VCCError, VCC_VERSION
from .compiler import compile_session
from .query import limit_matches, collect_search_matches, emit_search_results

MAX_REGEX_PATTERN_LENGTH = 4096
_NESTED_UNBOUNDED_REPEAT = re.compile(
    r"\((?:\\.|[^()])*(?:[*+]|\{\d+,\})(?:\\.|[^()])*\)\s*"
    r"(?:[*+]|\{\d+,\})"
)


def validate_regex_pattern(pattern):
    """Reject common catastrophic-backtracking shapes before Python re executes."""
    if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
        return f"regex exceeds {MAX_REGEX_PATTERN_LENGTH} characters"
    if _NESTED_UNBOUNDED_REPEAT.search(pattern):
        return "regex contains nested unbounded repetition"
    if re.search(r"\\[1-9]", pattern) and re.search(r"[*+]|\{\d+,\}", pattern):
        return "regex combines a backreference with unbounded repetition"
    return None

def _expand_inputs(raw):
    files, errors = [], []
    for r in raw:
        expanded = globmod.glob(r, recursive=True)
        expanded.sort(key=lambda f: os.path.getmtime(f))
        if expanded:
            files.extend(expanded)
        elif globmod.has_magic(r):
            errors.append(f"{r}: input pattern matched no files")
        else:
            files.append(r)
    # Preserve discovery order while avoiding duplicate work from overlapping globs.
    files = list(dict.fromkeys(files))
    return files, errors

def main():
    p = argparse.ArgumentParser(description="VCC - View-oriented Conversation Compiler")
    p.add_argument("--version", action="version", version=f"VCC {VCC_VERSION}")
    p.add_argument("input", nargs="+")
    p.add_argument("-o", "--output-dir")
    p.add_argument("--cache-dir", metavar="DIR",
                   help="override the private managed cache root")
    p.add_argument("--cache-policy", choices=("reuse", "refresh"), default="reuse",
                   help="reuse valid full/brief cache entries or force regeneration")
    p.add_argument("--search-only", action="store_true",
                   help="search incrementally without writing transcript views")
    p.add_argument("--strict", action="store_true",
                   help="fail on an incomplete final JSONL line and stop at the first error")
    p.add_argument("-t", "--truncate", nargs="?", type=int, const=128, default=128, metavar="N")
    p.add_argument("-tu", "--truncate-user", nargs="?", type=int, const=256, default=256, metavar="N")
    query = p.add_mutually_exclusive_group()
    query.add_argument("--grep", metavar="PATTERN",
                       help="search with one Python regular expression")
    query.add_argument("--literal", metavar="TEXT",
                       help="search for one literal string")
    query.add_argument("--term", action="append", metavar="TEXT",
                       help="search for a literal term; repeat for multiple terms")
    p.add_argument("--allow-unsafe-regex", action="store_true",
                   help="bypass conservative regex resource-safety checks")
    p.add_argument("--match", choices=("all", "any"), default="all",
                   help="require all or any repeated --term values (default: all)")
    p.add_argument("-i", "--ignore-case", action="store_true",
                   help="match without case distinctions")
    p.add_argument("--format", choices=("text", "json", "ndjson"), default="text",
                   help="search-result output format (default: text)")
    p.add_argument("--max-matches-per-input", type=int, default=0, metavar="N",
                   help="retain the N highest-scoring blocks per input; 0 means unlimited")
    p.add_argument("--diagnostics", action="store_true",
                   help="emit schema-v2 source accounting and recall selection on stderr")
    p.add_argument("--max-media-bytes", type=int, default=DEFAULT_MAX_MEDIA_BYTES,
                   help="maximum decoded bytes per embedded item; 0 means unlimited")
    p.add_argument("--chain-window", type=int, default=0, metavar="N",
                   help="materialize only the newest N compaction chains; 0 means all")
    a = p.parse_args()
    if a.output_dir and a.cache_dir:
        p.error("--output-dir and --cache-dir cannot be used together")
    has_query = bool(a.grep or a.literal is not None or a.term)
    if a.search_only and not has_query:
        p.error("--search-only requires --grep, --literal, or --term")
    if a.search_only and (a.output_dir or a.cache_dir):
        p.error("--search-only cannot be combined with --output-dir or --cache-dir")
    if (a.truncate < 0 or a.truncate_user < 0 or a.max_matches_per_input < 0 or
            a.max_media_bytes < 0 or a.chain_window < 0):
        p.error("truncation and match limits must be non-negative")
    flags = re.IGNORECASE if a.ignore_case else 0
    try:
        if a.grep:
            unsafe_reason = validate_regex_pattern(a.grep)
            if unsafe_reason and not a.allow_unsafe_regex:
                p.error(f"unsafe --grep pattern: {unsafe_reason}; use literals/terms or "
                        "--allow-unsafe-regex")
            patterns = [re.compile(a.grep, flags)]
        elif a.literal is not None:
            patterns = [re.compile(re.escape(a.literal), flags)]
        elif a.term:
            if any(not term for term in a.term):
                p.error("--term values must not be empty")
            patterns = [re.compile(re.escape(term), flags) for term in a.term]
        else:
            patterns = []
    except re.error as e:
        p.error(f"invalid regex for --grep: {e}")
    grep_pattern = patterns[0] if len(patterns) == 1 else None
    if a.literal == "":
        p.error("--literal must not be empty")
    files, input_errors = _expand_inputs(a.input)
    collision_errors = []
    if a.output_dir:
        stems = {}
        for path in files:
            stem = os.path.splitext(os.path.basename(path))[0]
            stems.setdefault(stem, []).append(path)
        for stem, paths in stems.items():
            if len(paths) > 1:
                collision_errors.append(
                    f"shared output directory collision for stem {stem!r}: " +
                    ", ".join(paths)
                )
    if collision_errors:
        for message in collision_errors:
            print(f"error: {message}", file=sys.stderr)
        return 1
    protected_inputs = {os.path.abspath(path) for path in files}
    failures = len(input_errors)
    for message in input_errors:
        print(f"error: {message}", file=sys.stderr)
    if a.strict and failures:
        return 1
    if not files:
        return 1

    diagnostics_by_path = {}

    def _compile_one(f, output_dir, quiet, write_outputs):
        nonlocal failures
        diagnostics = {}
        try:
            result = compile_session(
                f, output_dir, a.truncate, a.truncate_user, grep_pattern,
                quiet=quiet, write_outputs=write_outputs,
                tolerate_partial_tail=not a.strict,
                diagnostics=diagnostics,
                protected_inputs=protected_inputs,
                max_media_bytes=a.max_media_bytes,
                chain_window=a.chain_window,
            )
            diagnostics_by_path[f] = diagnostics
            if a.diagnostics:
                print("diagnostics: " + json.dumps(diagnostics, ensure_ascii=False),
                      file=sys.stderr)
            return result
        except (OSError, VCCError) as exc:
            failures += 1
            print(f"error: {exc}", file=sys.stderr)
            return None

    json_matches = []
    if a.search_only:
        with tempfile.TemporaryDirectory(prefix="vcc-search-") as temp_dir:
            for f in files:
                res = _compile_one(f, temp_dir, quiet=True, write_outputs=False)
                if res is None:
                    if a.strict:
                        return 1
                    continue
                if a.format == "json":
                    json_matches.extend(limit_matches(
                        collect_search_matches(res, patterns, a.match),
                        a.max_matches_per_input))
                else:
                    emit_search_results(res, patterns, a.match, a.format,
                                a.max_matches_per_input)
                del res
        if a.format == "json":
            print(json.dumps(json_matches, ensure_ascii=False, indent=2))
        return 1 if failures else 0

    cache_root = None if a.output_dir else (a.cache_dir or default_cache_root())
    ordered_files = reversed(files) if has_query else files
    for f in ordered_files:
        if cache_root:
            try:
                output_dir = prepare_cache_output_dir(cache_root, f)
            except (OSError, VCCError) as exc:
                failures += 1
                print(f"error: {exc}", file=sys.stderr)
                if a.strict:
                    return 1
                continue
        else:
            output_dir = a.output_dir
            os.makedirs(output_dir, mode=0o700, exist_ok=True)
        if (cache_root and not has_query and not a.diagnostics and
                a.cache_policy == "reuse" and
                cache_is_valid(output_dir, f, a.truncate, a.truncate_user,
                               a.chain_window)):
            protect_cache_tree(output_dir)
            report_cache_hit(output_dir)
            continue
        previous_artifacts = managed_artifact_names(output_dir) if cache_root else set()
        res = _compile_one(f, output_dir, quiet=has_query, write_outputs=True)
        if res is None:
            if a.strict:
                return 1
            continue
        if cache_root:
            artifact_names = set()
            for full_path, _, _ in res:
                artifact_names.add(os.path.basename(full_path))
                artifact_names.add(os.path.basename(full_path).removesuffix(".txt") + ".min.txt")
                if grep_pattern:
                    artifact_names.add(os.path.basename(full_path).removesuffix(".txt") + ".view.txt")
                with open(full_path, encoding="utf-8") as rendered:
                    for media_name in re.findall(
                            r"\[(?:image|document): ([^\]/\\]+)\]", rendered.read()):
                        artifact_names.add(media_name)
            try:
                remove_obsolete_managed_artifacts(
                    output_dir, previous_artifacts, artifact_names)
                write_cache_metadata(output_dir, f, a.truncate, a.truncate_user,
                                      grep_pattern, artifact_names,
                                      diagnostics_by_path.get(f), a.chain_window)
            except (OSError, VCCError) as exc:
                failures += 1
                print(f"error: {exc}", file=sys.stderr)
                if a.strict:
                    return 1
            protect_cache_tree(output_dir)
        if has_query:
            if a.format == "json":
                json_matches.extend(limit_matches(
                    collect_search_matches(res, patterns, a.match),
                    a.max_matches_per_input))
            else:
                emit_search_results(res, patterns, a.match, a.format,
                            a.max_matches_per_input)
        del res
    if a.format == "json" and has_query:
        print(json.dumps(json_matches, ensure_ascii=False, indent=2))
    return 1 if failures else 0
