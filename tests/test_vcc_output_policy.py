import json
import os
from pathlib import Path
import stat
import base64
import subprocess
import sys
import tempfile
import unittest
import importlib.util


ROOT = Path(__file__).resolve().parents[1]
VCC = ROOT / "skills" / "conversation-compiler" / "scripts" / "VCC.py"
HISTORY_SEARCH = VCC.with_name("history_search.py")
FIXTURES = ROOT / "tests" / "fixtures"


def write_session(path: Path, marker: str) -> None:
    records = [
        {"type": "user", "message": {"content": f"find {marker}"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"found {marker}"}
        ]}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def write_records(path: Path, records) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


class OutputPolicyTests(unittest.TestCase):
    def run_vcc(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(VCC), *args],
            check=False,
            text=True,
            capture_output=True,
        )

    def diagnostics_from(self, result: subprocess.CompletedProcess) -> dict:
        return json.loads(next(
            line.removeprefix("diagnostics: ") for line in result.stderr.splitlines()
            if line.startswith("diagnostics: ")
        ))

    def assert_source_accounting(self, diagnostics: dict) -> None:
        self.assertEqual(
            diagnostics["source_records_supported"] +
            diagnostics["source_records_ignored"] +
            diagnostics["source_records_unknown"],
            diagnostics["source_records_total"],
        )

    def test_representative_client_fixtures_and_recall_selection(self):
        with tempfile.TemporaryDirectory() as cache:
            codex = self.run_vcc(
                str(FIXTURES / "codex-compacted.jsonl"),
                "--cache-dir", cache, "--chain-window", "2", "--diagnostics",
            )
            self.assertEqual(codex.returncode, 0, codex.stderr)
            codex_diagnostics = self.diagnostics_from(codex)
            self.assert_source_accounting(codex_diagnostics)
            self.assertEqual(codex_diagnostics["compaction_boundaries"], 2)
            self.assertEqual(codex_diagnostics["chains_detected"], 3)
            self.assertEqual(codex_diagnostics["chains_emitted"], 2)
            selection = codex_diagnostics["recall_selection"]
            self.assertEqual(selection["pre_compaction_chain"]["chain"], 2)
            self.assertEqual(selection["latest_chain"]["chain"], 3)
            self.assertEqual(selection["older_chains_skipped_by_default"], 1)
            codex_entry = next(
                path for path in Path(cache).iterdir()
                if path.name.startswith("codex-compacted.jsonl")
            )
            self.assertFalse((codex_entry / "codex-compacted_1.txt").exists())
            self.assertTrue((codex_entry / "codex-compacted_2.txt").exists())
            self.assertTrue((codex_entry / "codex-compacted_3.txt").exists())

        for fixture, client in (
                ("copilot-events.jsonl", "copilot"),
                ("claude-compacted.jsonl", "claude")):
            with self.subTest(client=client), tempfile.TemporaryDirectory() as cache:
                result = self.run_vcc(
                    str(FIXTURES / fixture), "--cache-dir", cache, "--diagnostics"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                diagnostics = self.diagnostics_from(result)
                self.assertEqual(diagnostics["client"], client)
                self.assert_source_accounting(diagnostics)
                self.assertEqual(diagnostics["compaction_boundaries"], 1)

    def test_search_only_writes_no_views(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "session.jsonl"
            write_session(source, "frobnicator")
            before = set(Path(td).iterdir())
            result = self.run_vcc(str(source), "--grep", "frobnicator", "--search-only")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("session.jsonl::rendered", result.stdout)
            self.assertEqual(set(Path(td).iterdir()), before)

    def test_plain_compile_uses_private_managed_cache_not_source_directory(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "session.jsonl"
            write_session(source, "managed-default")
            before = set(Path(td).iterdir())
            env = os.environ.copy()
            env["VCC_CACHE_DIR"] = cache
            result = subprocess.run(
                [sys.executable, str(VCC), str(source)],
                check=False, text=True, capture_output=True, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(Path(td).iterdir()), before)
            entry = next(Path(cache).iterdir())
            self.assertTrue((entry / "session.txt").is_file())
            self.assertTrue((entry / "session.min.txt").is_file())
            self.assertTrue((entry / "metadata.json").is_file())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(Path(cache).stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o700)

    @unittest.skipUnless(os.name == "posix", "symlink semantics require POSIX")
    def test_managed_cache_rejects_symlink_entry(self):
        import hashlib

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "session.jsonl"
            write_session(source, "symlink-entry")
            digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
            entry = Path(cache) / f"session.jsonl-{digest}"
            target = Path(td) / "outside-cache"
            target.mkdir()
            entry.symlink_to(target, target_is_directory=True)
            result = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(result.returncode, 1)
            self.assertIn("managed cache entry is not a real directory", result.stderr)
            self.assertEqual(list(target.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "symlink semantics require POSIX")
    def test_source_path_aliases_share_one_canonical_cache_entry(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "session.jsonl"
            alias = Path(td) / "alias.jsonl"
            write_session(source, "canonical-source")
            alias.symlink_to(source)
            first = self.run_vcc(str(source), "--cache-dir", cache)
            second = self.run_vcc(str(alias), "--cache-dir", cache)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("cache hit", second.stdout)
            entries = list(Path(cache).iterdir())
            self.assertEqual(len(entries), 1)
            metadata = json.loads(
                (entries[0] / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["source"], str(source.resolve()))

    def test_search_only_does_not_decode_embedded_media(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "media.jsonl"
            write_records(source, [{
                "type": "user", "message": {"content": [
                    {"type": "text", "text": "media-marker"},
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": "not-valid-base64%%%",
                    }},
                ]},
            }])
            result = self.run_vcc(
                str(source), "--grep", "media-marker", "--search-only"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("media-marker", result.stdout)
            self.assertEqual(list(Path(td).glob("media_img_*")), [])

    def test_materialized_media_rejects_invalid_data_and_sanitizes_extension(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "invalid.jsonl"
            write_records(source, [{"type": "user", "message": {"content": [{
                "type": "image", "source": {"type": "base64", "data": "%%%bad"}
            }]}}])
            invalid = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(invalid.returncode, 1)
            self.assertIn("invalid embedded img base64", invalid.stderr)
            self.assertNotIn("Traceback", invalid.stderr)

            safe = Path(td) / "safe.jsonl"
            write_records(safe, [{"type": "user", "message": {"content": [{
                "type": "image", "source": {
                    "type": "base64", "media_type": "image/../../outside",
                    "data": base64.b64encode(b"safe-image").decode("ascii"),
                }
            }]}}])
            valid = self.run_vcc(str(safe), "--cache-dir", cache)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            entry = next(path for path in Path(cache).iterdir() if path.name.startswith("safe.jsonl"))
            self.assertEqual([path.suffix for path in entry.glob("safe_img_*")], [".png"])
            self.assertFalse((Path(td) / "outside").exists())

    @unittest.skipUnless(os.name == "posix", "symlink semantics require POSIX")
    def test_media_output_replaces_symlink_without_following_it(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "session.jsonl"
            target = base / "outside-target"
            target.write_bytes(b"do-not-touch")
            output = base / "session_img_0.png"
            output.symlink_to(target)
            write_records(source, [{"type": "user", "message": {"content": [{
                "type": "image", "source": {"type": "base64", "media_type": "image/png",
                    "data": base64.b64encode(b"safe-output").decode("ascii")}
            }]}}])
            result = self.run_vcc(str(source), "-o", str(base))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_bytes(), b"do-not-touch")
            self.assertFalse(output.is_symlink())
            self.assertEqual(output.read_bytes(), b"safe-output")

    def test_cache_separates_equal_basenames_and_records_provenance(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            first = Path(td) / "one" / "events.jsonl"
            second = Path(td) / "two" / "events.jsonl"
            first.parent.mkdir()
            second.parent.mkdir()
            write_session(first, "alpha")
            write_session(second, "beta")
            result = self.run_vcc(str(first), str(second), "--cache-dir", cache)
            self.assertEqual(result.returncode, 0, result.stderr)
            entries = sorted(p for p in Path(cache).iterdir() if p.is_dir())
            self.assertEqual(len(entries), 2)
            sources = set()
            for entry in entries:
                metadata = json.loads((entry / "metadata.json").read_text(encoding="utf-8"))
                sources.add(metadata["source"])
                self.assertTrue((entry / "events.txt").exists())
                self.assertTrue((entry / "events.min.txt").exists())
                if os.name == "posix":
                    mode = stat.S_IMODE((entry / "events.txt").stat().st_mode)
                    self.assertEqual(mode, 0o600)
            self.assertEqual(sources, {
                os.path.normcase(str(first.resolve())),
                os.path.normcase(str(second.resolve())),
            })

    def test_cache_reuse_is_versioned_and_invalidates_on_source_change(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "session.jsonl"
            write_session(source, "cache-v1")
            first = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(first.returncode, 0, first.stderr)
            entry = next(Path(cache).iterdir())
            metadata = json.loads((entry / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["vcc_version"], "2.3.0")
            self.assertIn("source_ctime_ns", metadata)
            self.assertIn("source_dev", metadata)
            self.assertIn("source_ino", metadata)
            self.assertIn("source_sha256", metadata)
            self.assertEqual(metadata["diagnostics"]["client"], "claude")
            full = entry / "session.txt"
            first_mtime = full.stat().st_mtime_ns
            second = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("cache hit", second.stdout)
            self.assertEqual(full.stat().st_mtime_ns, first_mtime)
            full.write_text("corrupted cache", encoding="utf-8")
            repaired = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            self.assertNotIn("cache hit", repaired.stdout)
            self.assertIn("cache-v1", full.read_text(encoding="utf-8"))
            metadata = json.loads((entry / "metadata.json").read_text(encoding="utf-8"))
            metadata["schema_version"] = 0
            (entry / "metadata.json").write_text(
                json.dumps(metadata), encoding="utf-8")
            schema_repaired = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(schema_repaired.returncode, 0, schema_repaired.stderr)
            self.assertNotIn("cache hit", schema_repaired.stdout)
            write_session(source, "cache-v2-changed")
            third = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertNotIn("cache hit", third.stdout)
            self.assertIn("cache-v2-changed", full.read_text(encoding="utf-8"))

    def test_cache_ctime_rejects_same_size_same_mtime_source_replacement(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "session.jsonl"
            write_session(source, "marker-one")
            first = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(first.returncode, 0, first.stderr)
            original = source.stat()
            before = source.read_text(encoding="utf-8")
            after = before.replace("marker-one", "marker-two")
            self.assertEqual(len(before), len(after))
            source.write_text(after, encoding="utf-8")
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(source.stat().st_size, original.st_size)
            self.assertEqual(source.stat().st_mtime_ns, original.st_mtime_ns)
            second = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("cache hit", second.stdout)
            full = next(Path(cache).glob("*/session.txt")).read_text(encoding="utf-8")
            self.assertIn("marker-two", full)

    def test_cache_refresh_removes_obsolete_managed_chains(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "events.jsonl"
            write_records(source, [
                {"type": "user", "message": {"content": "first chain"}},
                {"type": "system", "subtype": "compact_boundary"},
                {"type": "user", "message": {"content": "obsolete secret chain"}},
            ])
            first = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(first.returncode, 0, first.stderr)
            entry = next(Path(cache).iterdir())
            self.assertTrue((entry / "events_2.txt").exists())
            write_session(source, "replacement chain")
            refreshed = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
            self.assertTrue((entry / "events.txt").exists())
            self.assertFalse((entry / "events_1.txt").exists())
            self.assertFalse((entry / "events_2.txt").exists())
            remaining = "\n".join(path.read_text(encoding="utf-8")
                                  for path in entry.glob("*.txt"))
            self.assertNotIn("obsolete secret chain", remaining)

    def test_compiler_refuses_self_and_cross_input_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self_input = base / "self.txt"
            write_session(self_input, "authoritative self")
            original = self_input.read_bytes()
            self_result = self.run_vcc(str(self_input), "-o", str(base))
            self.assertEqual(self_result.returncode, 1)
            self.assertIn("refusing to overwrite authoritative input", self_result.stderr)
            self.assertEqual(self_input.read_bytes(), original)

            jsonl = base / "cross.jsonl"
            text_input = base / "cross.txt"
            write_session(jsonl, "jsonl authority")
            write_session(text_input, "text authority")
            text_original = text_input.read_bytes()
            cross_result = self.run_vcc(str(jsonl), str(text_input), "-o", str(base))
            self.assertEqual(cross_result.returncode, 1)
            self.assertEqual(text_input.read_bytes(), text_original)

            media_source = base / "media-source.jsonl"
            media_input = base / "media-source_img_0.png"
            write_records(media_source, [{"type": "user", "message": {"content": [{
                "type": "image", "source": {"type": "base64", "media_type": "image/png",
                    "data": base64.b64encode(b"decoded-image").decode("ascii")}
            }]}}])
            write_session(media_input, "authoritative media-named input")
            media_original = media_input.read_bytes()
            media_result = self.run_vcc(
                str(media_source), str(media_input), "-o", str(base))
            self.assertEqual(media_result.returncode, 1)
            self.assertIn("refusing to overwrite authoritative input with media",
                          media_result.stderr)
            self.assertEqual(media_input.read_bytes(), media_original)

    def test_shared_export_rejects_equal_input_basenames_before_writing(self):
        with (tempfile.TemporaryDirectory() as td,
              tempfile.TemporaryDirectory() as export):
            first = Path(td) / "one" / "events.jsonl"
            second = Path(td) / "two" / "events.jsonl"
            first.parent.mkdir()
            second.parent.mkdir()
            write_session(first, "first")
            write_session(second, "second")
            result = self.run_vcc(str(first), str(second), "-o", export)
            self.assertEqual(result.returncode, 1)
            self.assertIn("shared output directory collision", result.stderr)
            self.assertEqual(list(Path(export).iterdir()), [])

    def test_search_only_requires_grep(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "session.jsonl"
            write_session(source, "marker")
            result = self.run_vcc(str(source), "--search-only")
            self.assertEqual(result.returncode, 2)
            self.assertIn("--search-only requires --grep", result.stderr)

    def test_materialized_multifile_grep_keeps_all_results(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            older = Path(td) / "older.jsonl"
            newer = Path(td) / "newer.jsonl"
            write_session(older, "shared-marker older")
            write_session(newer, "shared-marker newer")
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))
            result = self.run_vcc(
                str(older), str(newer), "--grep", "shared-marker", "--cache-dir", cache
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("older.txt", result.stdout)
            self.assertIn("newer.txt", result.stdout)
            self.assertLess(result.stdout.index("newer.txt"), result.stdout.index("older.txt"))

    def test_codex_normalization_preserves_messages_and_tools(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "rollout.jsonl"
            write_records(source, [
                {"type": "response_item", "timestamp": "2026-01-01T00:00:00Z", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "codex-marker"}]}},
                {"type": "response_item", "payload": {
                    "type": "function_call", "call_id": "call-123", "name": "shell",
                    "arguments": "{\"cmd\": \"pwd\"}"}},
                {"type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": "call-123",
                    "output": "/workspace"}},
            ])
            result = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(result.returncode, 0, result.stderr)
            full = next(Path(cache).glob("*/rollout.txt")).read_text(encoding="utf-8")
            self.assertIn("codex-marker", full)
            self.assertIn(">>>tool_call shell", full)
            self.assertIn("/workspace", full)

    def test_codex_agent_messages_and_reasoning_summaries_are_supported(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "rollout.jsonl"
            write_records(source, [
                {"type": "response_item", "payload": {
                    "type": "agent_message", "author": "worker", "recipient": "root",
                    "content": [
                        {"type": "input_text", "text": "agent payload"},
                        {"type": "encrypted_content", "encrypted_content": "secret-ciphertext"},
                    ]}},
                {"type": "response_item", "payload": {
                    "type": "reasoning", "summary": [
                        {"type": "summary_text", "text": "reasoning summary"}
                    ], "encrypted_content": "opaque-reasoning"}},
            ])
            result = self.run_vcc(str(source), "--cache-dir", cache, "--diagnostics")
            self.assertEqual(result.returncode, 0, result.stderr)
            full = next(Path(cache).glob("*/rollout.txt")).read_text(encoding="utf-8")
            self.assertIn("[agent message worker -> root]", full)
            self.assertIn("agent payload", full)
            self.assertIn("reasoning summary", full)
            self.assertNotIn("secret-ciphertext", full)
            self.assertNotIn("opaque-reasoning", full)
            diagnostics = json.loads(next(
                line.removeprefix("diagnostics: ") for line in result.stderr.splitlines()
                if line.startswith("diagnostics: ")
            ))
            self.assertEqual(diagnostics["unknown_types"], [])

    def test_codex_compaction_records_split_chains_without_duplicate_boundaries(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "rollout.jsonl"
            write_records(source, [
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "before-compaction"}]}},
                {"type": "compacted", "timestamp": "2026-01-01T00:00:00Z", "payload": {
                    "message": "", "replacement_history": [], "window_number": 1}},
                {"type": "event_msg", "payload": {"type": "token_count"}},
                {"type": "event_msg", "payload": {"type": "context_compacted"}},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "after-compaction"}]}},
            ])
            result = self.run_vcc(str(source), "--cache-dir", cache, "--diagnostics")
            self.assertEqual(result.returncode, 0, result.stderr)
            entry = next(Path(cache).iterdir())
            self.assertIn("before-compaction", (entry / "rollout_1.txt").read_text())
            self.assertIn("after-compaction", (entry / "rollout_2.txt").read_text())
            self.assertFalse((entry / "rollout_3.txt").exists())
            diagnostics = json.loads(next(
                line.removeprefix("diagnostics: ") for line in result.stderr.splitlines()
                if line.startswith("diagnostics: ")
            ))
            self.assertEqual(diagnostics["compaction_boundaries"], 1)
            self.assertEqual(diagnostics["unknown_types"], [])

    def test_codex_context_compacted_event_is_boundary_fallback(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "rollout.jsonl"
            write_records(source, [
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "fallback-before"}]}},
                {"type": "event_msg", "payload": {"type": "context_compacted"}},
                {"type": "response_item", "payload": {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "fallback-after"}]}},
            ])
            result = self.run_vcc(str(source), "--cache-dir", cache, "--diagnostics")
            self.assertEqual(result.returncode, 0, result.stderr)
            entry = next(Path(cache).iterdir())
            self.assertIn("fallback-before", (entry / "rollout_1.txt").read_text())
            self.assertIn("fallback-after", (entry / "rollout_2.txt").read_text())
            diagnostics = json.loads(next(
                line.removeprefix("diagnostics: ") for line in result.stderr.splitlines()
                if line.startswith("diagnostics: ")
            ))
            self.assertEqual(diagnostics["compaction_boundaries"], 1)

    def test_copilot_normalization_preserves_compaction_and_tool_error(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "events.jsonl"
            write_records(source, [
                {"type": "user.message", "data": {"content": "before-compaction"}},
                {"type": "session.compaction_complete", "data": {
                    "success": True, "summaryContent": "summary"}},
                {"type": "assistant.message", "data": {"content": "after-compaction"}},
                {"type": "tool.execution_start", "data": {
                    "toolCallId": "tool-1", "toolName": "bash", "arguments": {"cmd": "false"}}},
                {"type": "tool.execution_complete", "data": {
                    "toolCallId": "tool-1", "success": False,
                    "error": {"message": "expected failure"}}},
            ])
            result = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(result.returncode, 0, result.stderr)
            entry = next(Path(cache).iterdir())
            self.assertTrue((entry / "events_1.txt").exists())
            second = (entry / "events_2.txt").read_text(encoding="utf-8")
            self.assertIn("after-compaction", second)
            self.assertIn("expected failure", second)
            self.assertIn("[tool_error]", second)

    def test_claude_records_remain_supported(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
            source = Path(td) / "claude.jsonl"
            write_records(source, [
                {"type": "user", "message": {"content": "claude-marker"}},
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "claude-answer"}
                ]}},
            ])
            result = self.run_vcc(str(source), "--cache-dir", cache)
            self.assertEqual(result.returncode, 0, result.stderr)
            full = next(Path(cache).glob("*/claude.txt")).read_text(encoding="utf-8")
            self.assertIn("claude-marker", full)
            self.assertIn("claude-answer", full)

    def test_claude_harness_metadata_is_known_and_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "claude.jsonl"
            write_records(source, [
                {"type": "attachment", "attachment": {"type": "skill_listing"}},
                {"type": "mode", "mode": "normal"},
                {"type": "user", "message": {"content": "metadata-marker"}},
            ])
            result = self.run_vcc(
                str(source), "--literal", "metadata-marker", "--search-only", "--diagnostics"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            diagnostics = json.loads(next(
                line.removeprefix("diagnostics: ") for line in result.stderr.splitlines()
                if line.startswith("diagnostics: ")
            ))
            self.assertEqual(diagnostics["unknown_types"], [])
            self.assertEqual(diagnostics["source_records_ignored"], 2)
            self.assertEqual(
                diagnostics["source_records_supported"] +
                diagnostics["source_records_ignored"] +
                diagnostics["source_records_unknown"],
                diagnostics["source_records_total"],
            )

    def test_incomplete_live_tail_is_tolerated_unless_strict(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "live.jsonl"
            write_session(source, "complete-marker")
            with source.open("a", encoding="utf-8") as f:
                f.write('{"type":"assistant"')
            tolerant = self.run_vcc(
                str(source), "--grep", "complete-marker", "--search-only"
            )
            self.assertEqual(tolerant.returncode, 0, tolerant.stderr)
            self.assertIn("ignored incomplete live-session tail", tolerant.stderr)
            strict = self.run_vcc(
                str(source), "--grep", "complete-marker", "--search-only", "--strict"
            )
            self.assertEqual(strict.returncode, 1)
            self.assertIn("invalid JSON", strict.stderr)

    def test_bad_file_does_not_hide_later_search_results(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.jsonl"
            good = Path(td) / "good.jsonl"
            bad.write_text("{broken\n", encoding="utf-8")
            write_session(good, "surviving-marker")
            result = self.run_vcc(
                str(bad), str(good), "--grep", "surviving-marker", "--search-only"
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("bad.jsonl:1", result.stderr)
            self.assertIn("surviving-marker", result.stdout)

    def test_invalid_limits_and_unmatched_glob_are_reported(self):
        bad_limit = self.run_vcc("missing.jsonl", "-t", "-1")
        self.assertEqual(bad_limit.returncode, 2)
        self.assertIn("must be non-negative", bad_limit.stderr)
        no_match = self.run_vcc("definitely-missing-*.jsonl", "--grep", "x", "--search-only")
        self.assertEqual(no_match.returncode, 1)
        self.assertIn("input pattern matched no files", no_match.stderr)

    def test_literal_and_multiterm_queries_have_explicit_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "query.jsonl"
            write_session(source, "VCC a.b cache")
            literal = self.run_vcc(
                str(source), "--literal", "a.b", "--search-only", "--format", "json"
            )
            self.assertEqual(literal.returncode, 0, literal.stderr)
            literal_matches = json.loads(literal.stdout)
            self.assertTrue(literal_matches)
            self.assertEqual(literal_matches[0]["schema_version"], 1)
            all_terms = self.run_vcc(
                str(source), "--term", "vcc", "--term", "CACHE", "--match", "all",
                "--ignore-case", "--search-only", "--format", "ndjson",
            )
            self.assertEqual(all_terms.returncode, 0, all_terms.stderr)
            matches = [json.loads(line) for line in all_terms.stdout.splitlines()]
            self.assertTrue(all(len(match["matched_patterns"]) == 2 for match in matches))
            self.assertGreaterEqual(max(match["score"] for match in matches), 14)

    def test_regex_resource_guard_is_conservative_and_overridable(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "session.jsonl"
            write_session(source, "aaaa")
            rejected = self.run_vcc(
                str(source), "--grep", "(a+)+", "--search-only"
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("nested unbounded repetition", rejected.stderr)

            literal = self.run_vcc(
                str(source), "--literal", "(a+)+", "--search-only"
            )
            self.assertEqual(literal.returncode, 0, literal.stderr)

            overridden = self.run_vcc(
                str(source), "--grep", "(a+)+", "--allow-unsafe-regex",
                "--search-only",
            )
            self.assertEqual(overridden.returncode, 0, overridden.stderr)

    def test_per_input_match_limit_retains_highest_scoring_block(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "rank.jsonl"
            write_session(source, "rank-marker")
            result = self.run_vcc(
                str(source), "--literal", "rank-marker", "--search-only",
                "--format", "json", "--max-matches-per-input", "1",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            matches = json.loads(result.stdout)
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["role"], "user")

    def test_diagnostics_report_client_and_unknown_events(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "rollout.jsonl"
            write_records(source, [
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "diagnostic-marker"}]}},
                {"type": "response_item", "payload": {"type": "future_event"}},
            ])
            result = self.run_vcc(
                str(source), "--literal", "diagnostic-marker", "--search-only", "--diagnostics"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            line = next(line for line in result.stderr.splitlines()
                        if line.startswith("diagnostics: "))
            diagnostics = json.loads(line.removeprefix("diagnostics: "))
            self.assertEqual(diagnostics["client"], "codex")
            self.assertEqual(diagnostics["schema_version"], 2)
            self.assertEqual(diagnostics["source_records_total"], 2)
            self.assertEqual(diagnostics["source_records_supported"], 1)
            self.assertEqual(diagnostics["source_records_unknown"], 1)
            self.assertEqual(diagnostics["normalized_records_emitted"], 1)
            self.assertIn("future_event", diagnostics["unknown_types"])

    def test_history_search_uses_current_client_then_expands_on_weak_match(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            codex = base / "codex" / "2026" / "01" / "01"
            claude = base / "claude" / "project"
            copilot = base / "copilot"
            codex.mkdir(parents=True)
            claude.mkdir(parents=True)
            copilot.mkdir()
            write_records(codex / "rollout-current.jsonl", [
                {"type": "assistant", "message": {"content": [{
                    "type": "tool_use", "id": "weak-1", "name": "search", "input": {}}]}},
                {"type": "user", "message": {"content": [{
                    "type": "tool_result", "tool_use_id": "weak-1",
                    "content": "VCC cache publish"}]}},
            ])
            write_session(claude / "strong.jsonl", "VCC cache publish")
            result = self.run_vcc(
                "history-search", "VCC cache publish", "--current-client", "codex",
                "--root", f"codex={base / 'codex'}",
                "--root", f"claude={base / 'claude'}",
                "--root", f"copilot={copilot}", "--format", "json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["tiers_searched"], ["codex", "copilot", "claude"])
            self.assertIn("weak", report["expansion_reason"])
            self.assertEqual(report["results"][0]["client"], "claude")

    def test_history_search_stops_after_strong_current_client_match(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            codex = base / "codex" / "2026" / "01" / "01"
            claude = base / "claude"
            copilot = base / "copilot"
            codex.mkdir(parents=True)
            claude.mkdir()
            copilot.mkdir()
            write_session(codex / "rollout-current.jsonl", "VCC cache publish")
            result = self.run_vcc(
                "history-search", "VCC cache publish", "--current-client", "codex",
                "--root", f"codex={base / 'codex'}",
                "--root", f"claude={claude}", "--root", f"copilot={copilot}",
                "--format", "json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["tiers_searched"], ["codex"])
            self.assertIsNone(report["expansion_reason"])

    def test_history_search_prefers_exact_current_session(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            current = base / "current.jsonl"
            write_session(current, "exact compaction anchor")
            result = self.run_vcc(
                "history-search", "exact compaction anchor",
                "--current-client", "codex", "--current-session", str(current),
                "--root", f"codex={base / 'absent-codex'}",
                "--root", f"claude={base / 'absent-claude'}",
                "--root", f"copilot={base / 'absent-copilot'}", "--format", "json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["tiers_searched"], ["current-session"])
            self.assertEqual(report["results"][0]["tier"], "current-session")
            self.assertEqual(report["absent_roots"], [])

    def test_history_search_unknown_client_discloses_all_source_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            codex = base / "codex" / "2026" / "01" / "01"
            codex.mkdir(parents=True)
            write_session(codex / "rollout-one.jsonl", "fallback anchor")
            result = self.run_vcc(
                "history-search", "fallback anchor",
                "--root", f"codex={base / 'codex'}",
                "--root", f"claude={base / 'missing-claude'}",
                "--root", f"copilot={base / 'missing-copilot'}", "--format", "json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["scope"], "auto")
            self.assertIsNone(report["current_client"])
            self.assertEqual(report["tiers_searched"], ["copilot", "codex", "claude"])
            self.assertEqual(set(report["absent_roots"]), {"copilot", "claude"})

    def test_history_search_explicit_scope_overrides_current_session(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            current = base / "current-codex.jsonl"
            claude = base / "claude" / "project"
            claude.mkdir(parents=True)
            write_session(current, "scope anchor")
            write_session(claude / "claude.jsonl", "scope anchor")
            result = self.run_vcc(
                "history-search", "scope anchor", "--client", "claude",
                "--current-client", "codex", "--current-session", str(current),
                "--root", f"claude={base / 'claude'}", "--format", "json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["tiers_searched"], ["claude"])
            self.assertTrue(all(item["client"] == "claude" for item in report["results"]))
            self.assertTrue(any("explicit client scope" in warning
                                for warning in report["warnings"]))

    def test_history_search_batches_respect_portable_argv_bound(self):
        spec = importlib.util.spec_from_file_location("history_search", HISTORY_SEARCH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        paths = [Path("C:/") / ("long-segment-" * 20) / f"session-{i}.jsonl"
                 for i in range(200)]
        batches = list(module.chunks(paths, max_items=64, max_chars=24000))
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(len(batch) for batch in batches), len(paths))
        for batch in batches:
            self.assertLessEqual(len(batch), 64)
            self.assertLessEqual(sum(len(str(path)) + 3 for path in batch), 24000)


if __name__ == "__main__":
    unittest.main()
