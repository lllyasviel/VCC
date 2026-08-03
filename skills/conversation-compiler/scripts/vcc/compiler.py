"""Application service that compiles one session into semantic views."""

import os

from .cache import atomic_write_bytes, atomic_write_text
from .common import DEFAULT_MAX_MEDIA_BYTES, VCCError, tokenize
from .parser import collect_stats, load_records, merge_chunks, build_ir, split_chains
from .renderer import assign_lines, render_lines, build_brief_view, build_search_view

def compile_session(input_path, output_dir, truncate=128, truncate_user=256,
            grep_pattern=None, quiet=False, write_outputs=True,
            tolerate_partial_tail=True, diagnostics=None, protected_inputs=None,
            max_media_bytes=DEFAULT_MAX_MEDIA_BYTES, chain_window=0):
    if not output_dir:
        raise VCCError("compile_session requires an explicit managed or export output directory")
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(input_path))[0]

    recs = merge_chunks(load_records(input_path, tolerate_partial_tail, diagnostics))
    chains = split_chains(recs)
    if not chains:
        raise VCCError(f"{input_path}: no supported conversation records found")
    total_chains = len(chains)
    indexed_chains = list(enumerate(chains, 1))
    if chain_window:
        indexed_chains = indexed_chains[-chain_window:]

    results, paths = [], []

    for chain_index, chain in indexed_chains:
        sfx = f"_{chain_index}" if total_chains > 1 else ""
        ffn = f"{base}{sfx}.txt"
        mfn = f"{base}{sfx}.min.txt"
        vfn = f"{base}{sfx}.view.txt"
        fp = os.path.join(output_dir, ffn)
        mp = os.path.join(output_dir, mfn)
        vp = os.path.join(output_dir, vfn)
        protected = {os.path.realpath(path) for path in (protected_inputs or [input_path])}
        for candidate in (fp, mp, vp):
            if os.path.realpath(candidate) in protected:
                raise VCCError(
                    f"refusing to overwrite authoritative input with output: {candidate}"
                )
        data_ctr = [0]

        ir = build_ir(chain, output_dir, f"{base}{sfx}", data_ctr,
                   extract_media=write_outputs, max_media_bytes=max_media_bytes,
                   protected_inputs=protected, media_writer=atomic_write_bytes)
        assign_lines(ir)
        if write_outputs:
            build_brief_view(ir, truncate, ffn, truncate_user)
            full = render_lines(ir, "content")
            brief = render_lines(ir, "content_brief")
            stats_footer = collect_stats(chain)
            if stats_footer:
                full.extend([""] + stats_footer)
            atomic_write_text(fp, "\n".join(full))
            atomic_write_text(mp, "\n".join(brief))

        if grep_pattern and write_outputs:
            build_search_view(ir, ffn, grep_pattern)
            view = render_lines(ir, "content_view")
            atomic_write_text(vp, "\n".join(view))

        result_ref = fp if write_outputs else os.path.abspath(input_path) + "::rendered"
        results.append((result_ref, ir, os.path.abspath(input_path)))
        if write_outputs:
            ft, bt = "\n".join(full), "\n".join(brief)
            _cnt = lambda s: sum(1 for t in tokenize(s) if t.strip())
            paths.append((fp, mp, vp if grep_pattern else None,
                          len(full), _cnt(ft), len(brief), _cnt(bt)))

    if not quiet:
        for fp, _, _, fl, fw, _, _ in paths:
            print(f"  {fp}  ({fl} lines, {fw} words)")
        for _, mp, _, _, _, bl, bw in paths:
            print(f"  {mp}  ({bl} lines, {bw} words)")
        if grep_pattern:
            for _, _, vp, _, _, _, _ in paths:
                if vp:
                    print(f"  {vp}")

    if diagnostics is not None:
        chain_views = []
        for (chain_index, _), (result_ref, _, _) in zip(indexed_chains, results):
            full = result_ref if write_outputs else None
            brief = (result_ref.removesuffix(".txt") + ".min.txt"
                     if write_outputs else None)
            chain_views.append({
                "chain": chain_index,
                "full_view": full,
                "brief_view": brief,
            })
        diagnostics["chains_detected"] = total_chains
        diagnostics["chains_emitted"] = len(chain_views)
        diagnostics["recall_selection"] = {
            "pre_compaction_chain": chain_views[-2] if len(chain_views) > 1 else None,
            "latest_chain": chain_views[-1],
            "older_chains_skipped_by_default": max(0, total_chains - 2),
        }

    return results

# ── main ──
