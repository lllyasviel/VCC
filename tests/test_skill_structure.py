import ast
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
EXPECTED = {"conversation-compiler", "readchat", "recall", "searchchat"}


class SkillStructureTests(unittest.TestCase):
    def test_all_companion_skills_are_self_contained_and_named(self):
        self.assertEqual({path.name for path in SKILLS.iterdir() if path.is_dir()}, EXPECTED)
        for name in EXPECTED:
            skill = SKILLS / name
            text = (skill / "SKILL.md").read_text(encoding="utf-8")
            match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
            self.assertIsNotNone(match, name)
            frontmatter = match.group(1)
            keys = [line.split(":", 1)[0] for line in frontmatter.splitlines()
                    if ":" in line]
            self.assertEqual(keys, ["name", "description"], name)
            self.assertIn(f"name: {name}", frontmatter)
            description = next(
                line.split(":", 1)[1].strip().strip('"')
                for line in frontmatter.splitlines()
                if line.startswith("description:")
            )
            self.assertLessEqual(len(description), 200, name)
            self.assertTrue((skill / "agents" / "openai.yaml").is_file(), name)

    def test_companion_runtime_and_public_docs_exist(self):
        scripts = SKILLS / "conversation-compiler" / "scripts"
        self.assertTrue((scripts / "VCC.py").is_file())
        self.assertTrue((scripts / "history_search.py").is_file())
        modules = scripts / "vcc"
        for name in ("__init__.py", "common.py", "normalizers.py", "parser.py", "renderer.py",
                     "query.py", "cache.py", "compiler.py", "cli.py"):
            self.assertTrue((modules / name).is_file(), name)
        self.assertLessEqual(len((scripts / "VCC.py").read_text().splitlines()), 30)
        for name in ("README.md", "README_cn.md", "README_jp.md", "INSTALL.md", "SKILLS.md", "LICENSE"):
            self.assertTrue((ROOT / name).is_file(), name)

        for name in ("README.md", "README_cn.md", "README_jp.md", "INSTALL.md"):
            self.assertIn("SKILLS.md", (ROOT / name).read_text(encoding="utf-8"), name)

    def test_public_docs_report_release_status_and_roadmap(self):
        markers = {
            "README.md": ("## Current status and roadmap", "single-pass"),
            "README_cn.md": ("## 当前状态和后续方向", "单遍"),
            "README_jp.md": ("## 現在の状態とロードマップ", "single-pass"),
        }
        for filename, (heading, roadmap_marker) in markers.items():
            text = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn(heading, text, filename)
            self.assertIn("VCC 2.3.1", text, filename)
            self.assertIn(roadmap_marker, text, filename)

    def test_entry_skills_use_portable_sibling_runtime_reference(self):
        for name in ("readchat", "recall", "searchchat"):
            text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("../conversation-compiler/scripts/VCC.py", text, name)
            self.assertNotIn(".claude/skills/conversation-compiler", text, name)
            self.assertNotIn(".codex/skills/conversation-compiler", text, name)

    def test_runtime_dependency_direction(self):
        modules = SKILLS / "conversation-compiler" / "scripts" / "vcc"
        forbidden = {
            "common.py": {"normalizers", "parser", "renderer", "query", "cache", "compiler", "cli"},
            "normalizers.py": {"parser", "renderer", "query", "cache", "compiler", "cli"},
            "cache.py": {"normalizers", "parser", "renderer", "query", "compiler", "cli"},
            "parser.py": {"renderer", "query", "cache", "compiler", "cli"},
            "renderer.py": {"query", "cache", "compiler", "cli"},
            "query.py": {"parser", "renderer", "cache", "compiler", "cli"},
            "compiler.py": {"cli"},
        }
        for filename, blocked in forbidden.items():
            tree = ast.parse((modules / filename).read_text(encoding="utf-8"))
            imported = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.level and node.module
            }
            self.assertTrue(imported.isdisjoint(blocked), (filename, imported & blocked))

    def test_benchmark_smoke_emits_machine_readable_report(self):
        benchmark = ROOT / "benchmarks" / "benchmark_vcc.py"
        result = subprocess.run([
            sys.executable, str(benchmark), "--records-per-chain", "10",
            "--chains", "2", "--payload-size", "8", "--repeat", "1",
        ], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["source_records"], 21)
        self.assertIn("seconds_median", report["search_only"])


if __name__ == "__main__":
    unittest.main()
