"""Installer integration tests; all targets are disposable isolated profiles."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
START = b"<!-- jason-memory-global:start -->"
END = b"<!-- jason-memory-global:end -->"


class GlobalInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.package = self.base / "package"
        self.profile = self.base / "profile"
        self.home = self.base / "shared home"
        for relative in ("global/SKILL.md", "global/global_store.py", "templates/MEMORY.md",
                         "tools/jason_check.py", "tools/jason_doctor.py"):
            path = self.package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture: " + relative, encoding="utf-8")
        installer = ROOT / "global/install.py"
        if installer.exists():
            shutil.copyfile(installer, self.package / "global/install.py")

    def run_install(self, *args, ok=True, explicit_profile=True, env=None):
        cmd = [sys.executable, str(self.package / "global/install.py"), "--home", str(self.home)]
        if explicit_profile:
            cmd += ["--user-home", str(self.profile)]
        result = subprocess.run(cmd + list(args), capture_output=True, text=True,
                                encoding="utf-8", env=env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def write_config(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_install_both_with_stable_absolute_paths_and_generic_export(self):
        self.run_install()
        for target in (self.profile / ".codex/AGENTS.md", self.profile / ".claude/CLAUDE.md"):
            data = target.read_text(encoding="utf-8")
            self.assertIn(str(self.home.resolve()).replace("\\", "\\\\"), data)
            self.assertIn("context --project", data)
            self.assertEqual(data.count(START.decode()), 1)
        self.assertTrue((self.home / "framework/global_store.py").is_file())
        self.assertTrue((self.home / "framework/tools/jason_check.py").is_file())
        self.assertTrue((self.home / "GENERIC_INSTRUCTIONS.md").is_file())

    def test_claude_import_resolves_spaces_unicode_and_existing_memory_survives(self):
        self.home = self.base / "共享 記憶"
        self.profile = self.base / "同事 profile"
        self.run_install()
        target = self.profile / ".claude/CLAUDE.md"
        index = self.home / "memory/shared/MEMORY.md"
        line = next(x for x in target.read_text(encoding="utf-8").splitlines() if x.startswith("@"))
        imported = (target.parent / line[1:].replace("\\ ", " ")).resolve()
        self.assertEqual(imported, index.resolve())
        self.assertEqual(index.read_bytes(), (self.package / "templates/MEMORY.md").read_bytes())
        self.assertNotIn("@", (self.profile / ".codex/AGENTS.md").read_text(encoding="utf-8"))
        index.write_bytes(b"existing index must survive")
        self.run_install()
        self.assertEqual(index.read_bytes(), b"existing index must survive")
        self.assertFalse((self.home / "memory/projects").exists())

    def test_upgrade_moves_legacy_block_to_front_and_uninstall_restores_user_bytes(self):
        target = self.profile / ".claude/CLAUDE.md"
        prefix = b"\xef\xbb\xbf# My rules\r\nKeep this first user rule.\r\n"
        suffix = b"\r\nKeep this last user rule."
        self.write_config(target, prefix + START + b"\nlegacy\n" + END + suffix)
        self.run_install("--agent", "claude")
        installed = target.read_bytes()
        self.assertTrue(installed.startswith(b"\xef\xbb\xbf" + START))
        self.assertIn(END + b"\n# My rules", installed)
        self.run_install("--agent", "claude")
        self.assertEqual(target.read_bytes(), installed)
        self.run_install("--agent", "claude", "--uninstall")
        self.assertEqual(target.read_bytes(), prefix + suffix)

    def test_unicode_leading_relative_import_has_dot_prefix(self):
        self.home = self.profile / '.claude' / '共享 memory'
        self.run_install('--agent', 'claude')
        data = (self.profile / '.claude/CLAUDE.md').read_text(encoding='utf-8')
        self.assertIn('@./共享\\ memory/memory/shared/MEMORY.md\n', data)

    def test_invalid_shared_index_prevents_host_edits(self):
        index = self.home / "memory/shared/MEMORY.md"
        index.mkdir(parents=True)
        self.run_install(ok=False)
        self.assertFalse(self.profile.exists())

    def test_idempotent_install_backup_and_exact_uninstall_preserve_notes(self):
        target = self.profile / ".codex/AGENTS.md"
        original = b"\xef\xbb\xbf# user rules\r\n\r\nKeep all bytes: \xe4\xb8\xad\xe6\x96\x87"
        self.write_config(target, original)
        self.run_install("--agent", "codex")
        installed = target.read_bytes()
        backups = list(target.parent.glob("AGENTS.md.jason-memory-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.run_install("--agent", "codex")
        self.assertEqual(target.read_bytes(), installed)
        self.assertEqual(len(list(target.parent.glob("*.bak"))), 1)
        note = self.home / "memory/shared/user.md"
        self.write_config(note, b"private memory")
        self.run_install("--agent", "codex", "--uninstall")
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(note.read_bytes(), b"private memory")
        self.assertTrue((self.home / "framework/SKILL.md").exists())

    def test_dry_run_makes_no_directories(self):
        result = self.run_install("--dry-run")
        self.assertIn("dry_run", json.loads(result.stdout))
        self.assertFalse(self.home.exists())
        self.assertFalse(self.profile.exists())

    def test_malformed_second_target_rejects_before_first_target_changes(self):
        codex = self.profile / ".codex/AGENTS.md"
        claude = self.profile / ".claude/CLAUDE.md"
        for bad in (START, END, END + START, START + END + START + END):
            with self.subTest(bad=bad):
                self.write_config(codex, b"original")
                self.write_config(claude, bad)
                self.run_install(ok=False)
                self.assertEqual(codex.read_bytes(), b"original")
                self.assertFalse(self.home.exists())
                self.assertFalse(list(codex.parent.glob("*.bak")))

    def test_nonempty_codex_override_selected_and_empty_override_ignored(self):
        normal = self.profile / ".codex/AGENTS.md"
        override = normal.with_name("AGENTS.override.md")
        self.write_config(normal, b"normal")
        self.write_config(override, b"override")
        self.run_install("--agent", "codex")
        self.assertEqual(normal.read_bytes(), b"normal")
        self.assertIn(START, override.read_bytes())
        self.run_install("--agent", "codex", "--uninstall")
        self.write_config(override, b"")
        self.run_install("--agent", "codex")
        self.assertIn(START, normal.read_bytes())
        self.assertEqual(override.read_bytes(), b"")

    def test_explicit_profile_ignores_environment_config_homes(self):
        env = dict(os.environ, CODEX_HOME=str(self.base / "avoid-codex"),
                   CLAUDE_CONFIG_DIR=str(self.base / "avoid-claude"))
        self.run_install(env=env)
        self.assertFalse((self.base / "avoid-codex").exists())
        self.assertFalse((self.base / "avoid-claude").exists())

    def test_environment_config_homes_and_explicit_overrides(self):
        env = dict(os.environ, CODEX_HOME=str(self.base / "env-codex"),
                   CLAUDE_CONFIG_DIR=str(self.base / "env-claude"))
        self.run_install(explicit_profile=False, env=env)
        self.assertTrue((self.base / "env-codex/AGENTS.md").exists())
        self.assertTrue((self.base / "env-claude/CLAUDE.md").exists())
        self.run_install("--codex-home", str(self.base / "custom-codex"),
                         "--claude-home", str(self.base / "custom-claude"), env=env)
        self.assertTrue((self.base / "custom-codex/AGENTS.md").exists())
        self.assertTrue((self.base / "custom-claude/CLAUDE.md").exists())

    def test_generic_never_mutates_host_configuration(self):
        self.run_install("--agent", "generic")
        self.assertFalse(self.profile.exists())
        self.assertTrue((self.home / "GENERIC_INSTRUCTIONS.md").exists())

    def test_uninstall_removes_managed_blocks_from_both_codex_precedence_files(self):
        normal = self.profile / ".codex/AGENTS.md"
        override = normal.with_name("AGENTS.override.md")
        self.write_config(normal, b"original normal")
        self.run_install("--agent", "codex")
        self.write_config(override, b"new user override")
        self.run_install("--agent", "codex")
        self.run_install("--agent", "codex", "--uninstall")
        self.assertEqual(normal.read_bytes(), b"original normal")
        self.assertEqual(override.read_bytes(), b"new user override")

    def test_missing_framework_source_prevents_any_target_mutation(self):
        (self.package / "global/SKILL.md").unlink()
        self.run_install(ok=False)
        self.assertFalse(self.home.exists())
        self.assertFalse(self.profile.exists())

    def test_unicode_paths_work_with_ascii_console_and_default_fake_profile_home(self):
        self.home = self.base / "共享記憶 🧠"
        env = dict(os.environ, PYTHONIOENCODING="ascii")
        result = self.run_install("--agent", "generic", env=env)
        self.assertEqual(Path(json.loads(result.stdout)["home"]), self.home.resolve())
        cmd = [sys.executable, str(self.package / "global/install.py"),
               "--user-home", str(self.profile), "--agent", "generic"]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.profile / ".jason-memory-global/GENERIC_INSTRUCTIONS.md").exists())


if __name__ == "__main__":
    unittest.main()
