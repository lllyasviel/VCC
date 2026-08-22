#!/usr/bin/env python3
"""Generate deterministic Codex-style logs and benchmark VCC compile/search paths."""

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
VCC = ROOT / "skills" / "conversation-compiler" / "scripts" / "VCC.py"


def write_fixture(path, records, chains, payload_size):
    text = "x" * payload_size
    with path.open("w", encoding="utf-8") as stream:
        sequence = 0
        for chain in range(chains):
            for index in range(records):
                marker = " benchmark-target" if index == records - 1 else ""
                record = {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user" if index % 2 == 0 else "assistant",
                        "content": [{
                            "type": "input_text" if index % 2 == 0 else "output_text",
                            "text": f"chain={chain} record={sequence} {text}{marker}",
                        }],
                    },
                }
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                sequence += 1
            if chain + 1 < chains:
                stream.write(json.dumps({
                    "type": "compacted",
                    "payload": {"message": "", "replacement_history": [],
                                "window_number": chain + 1},
                }) + "\n")


def measure(command, repeats):
    durations = []
    for _ in range(repeats):
        started = time.perf_counter()
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        durations.append(time.perf_counter() - started)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
    return {
        "runs": repeats,
        "seconds_min": min(durations),
        "seconds_median": statistics.median(durations),
        "seconds_max": max(durations),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--records-per-chain", type=int, default=1000)
    parser.add_argument("--chains", type=int, default=3)
    parser.add_argument("--payload-size", type=int, default=256)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)
    if min(args.records_per_chain, args.chains, args.repeat) < 1 or args.payload_size < 0:
        parser.error("record, chain, and repeat counts must be positive; payload size non-negative")

    with tempfile.TemporaryDirectory(prefix="vcc-benchmark-") as directory:
        source = Path(directory) / "rollout.jsonl"
        cache = Path(directory) / "cache"
        write_fixture(source, args.records_per_chain, args.chains, args.payload_size)
        common = [sys.executable, str(VCC), str(source)]
        search = measure(common + [
            "--literal", "benchmark-target", "--search-only", "--format", "json"
        ], args.repeat)
        materialize = measure(common + [
            "--cache-dir", str(cache), "--cache-policy", "refresh", "--chain-window", "2"
        ], args.repeat)
        cache_hit = measure(common + [
            "--cache-dir", str(cache), "--chain-window", "2"
        ], args.repeat)
        report = {
            "schema_version": 1,
            "vcc": str(VCC),
            "input_bytes": source.stat().st_size,
            "source_records": args.records_per_chain * args.chains + args.chains - 1,
            "records_per_chain": args.records_per_chain,
            "chains": args.chains,
            "payload_size": args.payload_size,
            "search_only": search,
            "materialize_latest_two": materialize,
            "cache_hit_latest_two": cache_hit,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
