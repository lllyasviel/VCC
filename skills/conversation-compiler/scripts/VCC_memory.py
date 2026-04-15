#!/usr/bin/env python3
"""VCC Memory — pointer-based memory index for VCC conversation logs.

Stores only metadata and pointers (file paths + line ranges) into original JSONL/compiled .txt.
No content summaries, no embeddings, no LLM calls. Zero external dependencies.

Usage:
  python VCC_memory.py index <jsonl ...>          # index specific files
  python VCC_memory.py index --all                # index all JSONL in ~/.claude/projects/
  python VCC_memory.py search "query"             # keyword search
  python VCC_memory.py search "query" --fuzzy     # fuzzy search (token overlap)
  python VCC_memory.py timeline "topic"           # topic evolution over time
  python VCC_memory.py mark <jsonl> --line 450 --tag "label"
  python VCC_memory.py unmark <jsonl> --line 450
  python VCC_memory.py gc                         # remove entries for deleted JSONLs
  python VCC_memory.py stats                      # index statistics
"""

import argparse
import glob as globmod
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

# ── paths ──

MEMORY_DIR = os.path.join(os.path.expanduser("~"), ".claude", "vcc_memory")
INDEX_PATH = os.path.join(MEMORY_DIR, "index.json")
MARKERS_PATH = os.path.join(MEMORY_DIR, "markers.json")

# ── stop words for keyword extraction ──

_STOP_WORDS = frozenset(
    "a an the is are was were be been being have has had do does did will would "
    "shall should may might can could of in to for on with at by from as into "
    "through during before after above below between out off over under again "
    "further then once here there when where why how all each every both few "
    "more most other some such no nor not only own same so than too very just "
    "don t s d ll ve re m let get got also i me my we our you your he him his "
    "she her it its they them their what which who whom this that these those "
    "and but or if because about up".split()
)

_IDENT_RE = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]{2,}')
_PATH_RE = re.compile(r'[\w./\\-]+\.\w{1,10}')


# ── keyword extraction (no LLM) ──

def _extract_keywords(text):
    """Extract meaningful tokens from text: identifiers, file paths, multi-word terms."""
    if not text:
        return set()
    keywords = set()
    # file paths
    for m in _PATH_RE.finditer(text):
        p = m.group()
        if '/' in p or '\\' in p or '.' in p:
            keywords.add(p.lower())
            # also add basename
            base = os.path.basename(p)
            if base:
                keywords.add(base.lower())
    # identifiers and words
    for m in _IDENT_RE.finditer(text):
        w = m.group().lower()
        if w not in _STOP_WORDS and len(w) > 2:
            keywords.add(w)
            # split camelCase / snake_case
            parts = re.split(r'[_]|(?<=[a-z])(?=[A-Z])', m.group())
            for part in parts:
                pl = part.lower()
                if pl and pl not in _STOP_WORDS and len(pl) > 2:
                    keywords.add(pl)
    return keywords


def _extract_files_touched(ir):
    """Extract file paths from tool call nodes in IR."""
    files = set()
    for o in ir:
        if o.get("type") == "tool_call":
            for line in o.get("content", []):
                if line.strip().startswith("file_path:"):
                    fp = line.split(":", 1)[1].strip()
                    if fp:
                        files.add(fp)
                elif line.strip().startswith("pattern:"):
                    pat = line.split(":", 1)[1].strip()
                    if pat:
                        files.add(pat)
    return sorted(files)


def _extract_tools_used(ir):
    """Extract tool names from meta nodes."""
    tools = set()
    for o in ir:
        if o.get("type") == "meta" and o.get("_tool_summary"):
            summary = o["_tool_summary"]
            # "* ToolName ..." -> ToolName
            parts = summary.split()
            if len(parts) >= 2:
                tools.add(parts[1].strip('"'))
    return sorted(tools)


def _count_user_turns(ir):
    seen_secs = set()
    for o in ir:
        if o.get("type") == "user":
            s = o.get("_sec")
            if s is not None:
                seen_secs.add(s)
    return len(seen_secs)


# ── IndexBuilder ──

class IndexBuilder:
    """Build index entries from VCC IR."""

    @staticmethod
    def build_entry(jsonl_path, chain_index, ir, chain, txt_file):
        """Build one index entry from a compiled chain's IR.

        Args:
            jsonl_path: absolute path to source JSONL
            chain_index: which chain (0-based) within the JSONL
            ir: the IR list from VCC parse+assign_lines
            chain: the raw chain records (for stats extraction)
            txt_file: filename of the compiled .txt output
        """
        # collect all text for keyword extraction
        all_text_parts = []
        for o in ir:
            if o.get("searchable") and o.get("content"):
                all_text_parts.append("\n".join(o["content"]))

        all_text = "\n".join(all_text_parts)
        topics = sorted(_extract_keywords(all_text))
        # keep top 50 most distinctive keywords to control index size
        if len(topics) > 50:
            # prefer longer tokens (more specific)
            topics.sort(key=lambda t: -len(t))
            topics = sorted(topics[:50])

        files_touched = _extract_files_touched(ir)
        tools_used = _extract_tools_used(ir)
        user_turns = _count_user_turns(ir)

        # line count from IR
        max_line = 0
        for o in ir:
            el = o.get("end_line")
            if el is not None and el > max_line:
                max_line = el
        line_count = max_line + 1

        # duration from chain timestamps
        duration_sec = None
        timestamps = []
        for r in chain:
            ts = r.get("timestamp")
            if ts:
                timestamps.append(ts)
        if len(timestamps) >= 2:
            try:
                t0 = datetime.fromisoformat(min(timestamps).replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(max(timestamps).replace("Z", "+00:00"))
                duration_sec = int((t1 - t0).total_seconds())
            except Exception:
                pass

        # highlights: user messages with high keyword density
        highlights = []
        for o in ir:
            if o.get("type") == "user" and o.get("searchable"):
                text = "\n".join(o.get("content", []))
                kws = _extract_keywords(text)
                if len(kws) >= 3:
                    sl = o.get("start_line", 0) + 1
                    el = o.get("end_line", sl) + 1
                    highlights.append({
                        "keywords": sorted(list(kws)[:10]),
                        "txt_lines": [sl, el],
                    })

        mtime = None
        try:
            mtime = os.path.getmtime(jsonl_path)
        except OSError:
            pass

        return {
            "jsonl": os.path.realpath(jsonl_path),
            "chain_index": chain_index,
            "compiled_at": datetime.now(timezone.utc).isoformat(),
            "jsonl_mtime": mtime,
            "topics": topics,
            "tools_used": tools_used,
            "files_touched": files_touched,
            "user_turns": user_turns,
            "line_count": line_count,
            "duration_sec": duration_sec,
            "txt_file": txt_file,
            "highlights": highlights,
        }


# ── MemoryStore ──

class MemoryStore:
    """Read/write the index and markers JSON files."""

    def __init__(self, memory_dir=MEMORY_DIR):
        self.memory_dir = memory_dir
        self.index_path = os.path.join(memory_dir, "index.json")
        self.markers_path = os.path.join(memory_dir, "markers.json")

    def _ensure_dir(self):
        os.makedirs(self.memory_dir, exist_ok=True)

    def load_index(self):
        if os.path.exists(self.index_path):
            with open(self.index_path, encoding="utf-8") as f:
                return json.load(f)
        return []

    def save_index(self, entries):
        self._ensure_dir()
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=1)

    def load_markers(self):
        if os.path.exists(self.markers_path):
            with open(self.markers_path, encoding="utf-8") as f:
                return json.load(f)
        return []

    def save_markers(self, markers):
        self._ensure_dir()
        with open(self.markers_path, "w", encoding="utf-8") as f:
            json.dump(markers, f, ensure_ascii=False, indent=1)

    def upsert_entry(self, entry):
        """Insert or update an index entry (keyed by jsonl + chain_index)."""
        entries = self.load_index()
        key = (os.path.realpath(entry["jsonl"]), entry["chain_index"])
        found = False
        for i, e in enumerate(entries):
            if (os.path.realpath(e["jsonl"]), e["chain_index"]) == key:
                entries[i] = entry
                found = True
                break
        if not found:
            entries.append(entry)
        self.save_index(entries)
        return entries

    def needs_reindex(self, jsonl_path, chain_index):
        """Check if a JSONL chain needs reindexing based on mtime."""
        real_path = os.path.realpath(jsonl_path)
        try:
            current_mtime = os.path.getmtime(real_path)
        except OSError:
            return True
        entries = self.load_index()
        for e in entries:
            if os.path.realpath(e["jsonl"]) == real_path and e["chain_index"] == chain_index:
                stored_mtime = e.get("jsonl_mtime")
                if stored_mtime is not None and stored_mtime >= current_mtime:
                    return False
        return True

    def gc(self):
        """Remove entries whose JSONL files no longer exist. Returns count removed."""
        entries = self.load_index()
        before = len(entries)
        entries = [e for e in entries if os.path.exists(e["jsonl"])]
        self.save_index(entries)

        markers = self.load_markers()
        markers = [m for m in markers if os.path.exists(m["jsonl"])]
        self.save_markers(markers)

        return before - len(entries)


# ── Retriever ──

def _tokenize_query(query):
    """Split query into searchable tokens."""
    tokens = set()
    for m in _IDENT_RE.finditer(query):
        w = m.group().lower()
        if w not in _STOP_WORDS and len(w) > 2:
            tokens.add(w)
    for m in _PATH_RE.finditer(query):
        tokens.add(m.group().lower())
    # if no tokens extracted, fall back to simple split
    if not tokens:
        for w in query.lower().split():
            w = w.strip(".,;:!?\"'()[]{}").lower()
            if w and w not in _STOP_WORDS:
                tokens.add(w)
    return tokens


class Retriever:
    """Search the memory index."""

    def __init__(self, store=None):
        self.store = store or MemoryStore()

    def search_keyword(self, query):
        """Exact keyword match against topics, files_touched, highlights."""
        tokens = _tokenize_query(query)
        if not tokens:
            return []
        entries = self.store.load_index()
        hits = []
        for e in entries:
            score, matched_on = self._score_entry(e, tokens, fuzzy=False)
            if score > 0:
                hits.append(self._make_hit(e, score, matched_on))
        hits.sort(key=lambda h: -h["relevance"])
        return hits

    def search_fuzzy(self, query, threshold=0.3):
        """Fuzzy match using Jaccard similarity on token sets."""
        tokens = _tokenize_query(query)
        if not tokens:
            return []
        entries = self.store.load_index()
        hits = []
        for e in entries:
            score, matched_on = self._score_entry(e, tokens, fuzzy=True)
            if score >= threshold:
                hits.append(self._make_hit(e, score, matched_on))
        hits.sort(key=lambda h: -h["relevance"])
        return hits

    def search_timeline(self, topic):
        """Find all conversations mentioning a topic, sorted by time."""
        hits = self.search_fuzzy(topic, threshold=0.15)
        # sort by compiled_at ascending
        hits.sort(key=lambda h: h.get("compiled_at", ""))
        return hits

    def _score_entry(self, entry, query_tokens, fuzzy=False):
        """Score an entry against query tokens. Returns (score, matched_on)."""
        matched_on = []
        total_score = 0.0

        # check topics
        entry_topics = set(entry.get("topics", []))
        topic_score = self._match_score(query_tokens, entry_topics, fuzzy)
        if topic_score > 0:
            matched_on.append("topics")
            total_score += topic_score

        # check files_touched
        entry_files = set()
        for fp in entry.get("files_touched", []):
            entry_files.add(fp.lower())
            entry_files.add(os.path.basename(fp).lower())
        file_score = self._match_score(query_tokens, entry_files, fuzzy)
        if file_score > 0:
            matched_on.append("files_touched")
            total_score += file_score * 0.8

        # check highlights
        for h in entry.get("highlights", []):
            h_kws = set(h.get("keywords", []))
            h_score = self._match_score(query_tokens, h_kws, fuzzy)
            if h_score > 0:
                if "highlights" not in matched_on:
                    matched_on.append("highlights")
                total_score += h_score * 1.2  # highlights get a boost

        # check markers
        markers = self.store.load_markers()
        for m in markers:
            if m["jsonl"] == entry["jsonl"]:
                tag_tokens = _tokenize_query(m.get("tag", ""))
                m_score = self._match_score(query_tokens, tag_tokens, fuzzy)
                if m_score > 0:
                    if "markers" not in matched_on:
                        matched_on.append("markers")
                    total_score += m_score * 1.5  # markers get highest boost

        return total_score, matched_on

    @staticmethod
    def _match_score(query_tokens, target_tokens, fuzzy):
        """Compute match score between two token sets."""
        if not query_tokens or not target_tokens:
            return 0.0
        if fuzzy:
            # Jaccard-like: also count partial substring matches
            matches = 0
            for qt in query_tokens:
                for tt in target_tokens:
                    if qt == tt:
                        matches += 1
                        break
                    elif qt in tt or tt in qt:
                        matches += 0.5
                        break
            union = len(query_tokens | target_tokens)
            return matches / union if union else 0.0
        else:
            # exact: count how many query tokens appear in target
            overlap = query_tokens & target_tokens
            # also check substring containment
            for qt in query_tokens - overlap:
                for tt in target_tokens:
                    if qt in tt or tt in qt:
                        overlap.add(qt)
                        break
            return len(overlap) / len(query_tokens) if query_tokens else 0.0

    @staticmethod
    def _make_hit(entry, score, matched_on):
        hit = {
            "jsonl": entry["jsonl"],
            "chain_index": entry["chain_index"],
            "relevance": round(score, 3),
            "matched_on": matched_on,
            "txt_file": entry.get("txt_file", ""),
            "compiled_at": entry.get("compiled_at", ""),
            "user_turns": entry.get("user_turns", 0),
            "line_count": entry.get("line_count", 0),
        }
        # collect suggested lines from highlights
        suggested = []
        for h in entry.get("highlights", []):
            suggested.extend(h.get("txt_lines", []))
        if suggested:
            hit["suggested_lines"] = suggested
        return hit


# ── Marker ──

class Marker:
    """Manage user-explicit memory marks."""

    def __init__(self, store=None):
        self.store = store or MemoryStore()

    def add(self, jsonl_path, line, tag):
        markers = self.store.load_markers()
        real_path = os.path.realpath(jsonl_path)
        # deduplicate
        markers = [m for m in markers
                   if not (os.path.realpath(m["jsonl"]) == real_path and m["line"] == line)]
        markers.append({
            "jsonl": real_path,
            "line": line,
            "tag": tag,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        self.store.save_markers(markers)
        return len(markers)

    def remove(self, jsonl_path, line):
        markers = self.store.load_markers()
        real_path = os.path.realpath(jsonl_path)
        before = len(markers)
        markers = [m for m in markers
                   if not (os.path.realpath(m["jsonl"]) == real_path and m["line"] == line)]
        self.store.save_markers(markers)
        return before - len(markers)

    def list_all(self, jsonl_path=None):
        markers = self.store.load_markers()
        if jsonl_path:
            real_path = os.path.realpath(jsonl_path)
            markers = [m for m in markers if os.path.realpath(m["jsonl"]) == real_path]
        return markers


# ── index command: compile + index ──

def _find_all_jsonl():
    """Find all JSONL files under ~/.claude/projects/."""
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if not os.path.isdir(base):
        return []
    return sorted(globmod.glob(os.path.join(base, "**", "*.jsonl"), recursive=True))


def index_files(paths, store=None, force=False):
    """Index JSONL files by compiling them with VCC and extracting metadata."""
    # import VCC from same directory
    vcc_dir = os.path.dirname(os.path.abspath(__file__))
    vcc_path = os.path.join(vcc_dir, "VCC.py")
    if not os.path.exists(vcc_path):
        print(f"Error: VCC.py not found at {vcc_path}", file=sys.stderr)
        return 0

    import importlib.util
    spec = importlib.util.spec_from_file_location("VCC", vcc_path)
    vcc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vcc)

    store = store or MemoryStore()
    indexed = 0

    for jsonl_path in paths:
        abs_path = os.path.realpath(jsonl_path)
        if not os.path.exists(abs_path):
            print(f"  skip (not found): {jsonl_path}")
            continue

        # lex + merge + split chains
        try:
            recs = vcc.merge_chunks(vcc.lex(abs_path))
            chains = vcc.split_chains(recs)
        except Exception as e:
            print(f"  skip (parse error): {jsonl_path}: {e}")
            continue

        if not chains:
            continue

        base = os.path.splitext(os.path.basename(abs_path))[0]
        outdir = os.path.dirname(abs_path) or "."

        for i, chain in enumerate(chains):
            if not force and not store.needs_reindex(abs_path, i):
                continue

            sfx = f"_{i+1}" if len(chains) > 1 else ""
            txt_file = f"{base}{sfx}.txt"
            data_ctr = [0]

            try:
                ir = vcc.parse(chain, outdir, f"{base}{sfx}", data_ctr)
                vcc.assign_lines(ir)
            except Exception as e:
                print(f"  skip chain {i} (IR error): {jsonl_path}: {e}")
                continue

            entry = IndexBuilder.build_entry(abs_path, i, ir, chain, txt_file)
            store.upsert_entry(entry)
            indexed += 1

    return indexed


# ── hook for VCC.py (called after compile_pass) ──

def index_from_ir(jsonl_path, chain_index, ir, chain, txt_file):
    """Hook called from VCC.py after compilation to incrementally update index.

    This is the lightweight path — no re-parsing needed since IR is already available.
    """
    store = MemoryStore()
    if not store.needs_reindex(jsonl_path, chain_index):
        return
    entry = IndexBuilder.build_entry(jsonl_path, chain_index, ir, chain, txt_file)
    store.upsert_entry(entry)


# ── CLI ──

def _fmt_hit(hit, verbose=False):
    """Format a search hit for display."""
    rel = f"{hit['relevance']:.2f}"
    jsonl = hit["jsonl"]
    try:
        jsonl = os.path.relpath(jsonl)
    except ValueError:
        pass
    chain = hit["chain_index"]
    matched = ", ".join(hit["matched_on"])
    line = f"  [{rel}] {jsonl} (chain {chain}) matched: {matched}"
    if verbose:
        line += f"  turns={hit['user_turns']} lines={hit['line_count']}"
        if hit.get("suggested_lines"):
            line += f"  jump_to={hit['suggested_lines']}"
    return line


def main():
    p = argparse.ArgumentParser(description="VCC Memory — pointer-based memory index")
    sub = p.add_subparsers(dest="command")

    # index
    idx = sub.add_parser("index", help="Index JSONL files")
    idx.add_argument("files", nargs="*", help="JSONL files to index")
    idx.add_argument("--all", action="store_true", help="Index all JSONL in ~/.claude/projects/")
    idx.add_argument("--force", action="store_true", help="Re-index even if up to date")

    # search
    srch = sub.add_parser("search", help="Search the memory index")
    srch.add_argument("query", help="Search query")
    srch.add_argument("--fuzzy", action="store_true", help="Use fuzzy matching")
    srch.add_argument("-n", type=int, default=20, help="Max results (default 20)")
    srch.add_argument("-v", "--verbose", action="store_true")

    # timeline
    tl = sub.add_parser("timeline", help="Track topic evolution over time")
    tl.add_argument("topic", help="Topic to track")
    tl.add_argument("-n", type=int, default=50, help="Max results")
    tl.add_argument("-v", "--verbose", action="store_true")

    # mark
    mk = sub.add_parser("mark", help="Mark a memory point")
    mk.add_argument("jsonl", help="JSONL file path")
    mk.add_argument("--line", type=int, required=True, help="Line number in compiled .txt")
    mk.add_argument("--tag", required=True, help="Label for this mark")

    # unmark
    umk = sub.add_parser("unmark", help="Remove a memory mark")
    umk.add_argument("jsonl", help="JSONL file path")
    umk.add_argument("--line", type=int, required=True, help="Line number to unmark")

    # gc
    sub.add_parser("gc", help="Clean up entries for deleted JSONL files")

    # stats
    sub.add_parser("stats", help="Show index statistics")

    a = p.parse_args()

    if a.command == "index":
        if a.all:
            files = _find_all_jsonl()
            print(f"Found {len(files)} JSONL files")
        elif a.files:
            files = []
            for f in a.files:
                files.extend(globmod.glob(f, recursive=True))
        else:
            idx.print_help()
            return
        count = index_files(files, force=a.force)
        print(f"Indexed {count} conversation chains")

    elif a.command == "search":
        r = Retriever()
        if a.fuzzy:
            hits = r.search_fuzzy(a.query)
        else:
            hits = r.search_keyword(a.query)
        hits = hits[:a.n]
        if not hits:
            print("No results found.")
        else:
            print(f"{len(hits)} result(s):")
            for h in hits:
                print(_fmt_hit(h, a.verbose))

    elif a.command == "timeline":
        r = Retriever()
        hits = r.search_timeline(a.topic)[:a.n]
        if not hits:
            print("No results found.")
        else:
            print(f"{len(hits)} conversation(s) mentioning '{a.topic}':")
            for h in hits:
                ts = h.get("compiled_at", "?")[:10]
                print(f"  {ts}  {_fmt_hit(h, a.verbose).strip()}")

    elif a.command == "mark":
        m = Marker()
        total = m.add(a.jsonl, a.line, a.tag)
        print(f"Marked line {a.line} as '{a.tag}' ({total} total markers)")

    elif a.command == "unmark":
        m = Marker()
        removed = m.remove(a.jsonl, a.line)
        if removed:
            print(f"Removed mark at line {a.line}")
        else:
            print(f"No mark found at line {a.line}")

    elif a.command == "gc":
        store = MemoryStore()
        removed = store.gc()
        print(f"Removed {removed} stale entries")

    elif a.command == "stats":
        store = MemoryStore()
        entries = store.load_index()
        markers = store.load_markers()
        jsonls = set(e["jsonl"] for e in entries)
        total_lines = sum(e.get("line_count", 0) for e in entries)
        total_turns = sum(e.get("user_turns", 0) for e in entries)
        print(f"Index: {len(entries)} chains from {len(jsonls)} conversations")
        print(f"Total: {total_lines} lines, {total_turns} user turns")
        print(f"Markers: {len(markers)}")
        if os.path.exists(INDEX_PATH):
            size = os.path.getsize(INDEX_PATH)
            print(f"Index size: {size:,} bytes")

    else:
        p.print_help()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
