"""Release allowlist and unpacked installer smoke tests without a real profile."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "tools/memory_facts.py", "tools/memory_runtime.py", "tools/claude_memory_hook.py",
    "tools/hook_settings.py", "docs/memory-runtime.md", "tests/hooks-validation.md",
    "README.md", "INSTALL.cmd", "LICENSE", "global/install.py",
    "global/global_store.py", "global/SKILL.md", "templates/MEMORY.md",
    "tools/jason_check.py", "tools/jason_doctor.py",
}


class GlobalDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        builder = ROOT / "tools/build_global_download.py"
        self.assertTrue(builder.is_file(), "Global ZIP packager has not been implemented")
        spec = importlib.util.spec_from_file_location("global_download_under_test", builder)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def fixture_source(self):
        source = self.base / "source"
        for name in self.module.PACKAGE_FILES:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / name).read_bytes())
        for name in (".git/config", ".jason-memory/private.md", "AGENTS.md", "CLAUDE.md",
                     "SKILL.md", "global/.env", "credentials.json", "memory/shared/private.md"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE TEST SENTINEL", encoding="utf-8")
        return source

    def test_exact_allowlist_excludes_private_files_and_project_rules(self):
        output = self.base / "jason-memory-global.zip"
        self.module.build(output, source=self.fixture_source())
        with ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {"jason-memory-global/" + x for x in EXPECTED})
            for name in archive.namelist():
                self.assertNotIn(b"PRIVATE TEST SENTINEL", archive.read(name), name)
            self.assertEqual(archive.read("jason-memory-global/README.md"),
                             (ROOT / "global/README.md").read_bytes())

    def test_existing_output_is_not_overwritten(self):
        output = self.base / "jason-memory-global.zip"
        output.write_bytes(b"existing release")
        with self.assertRaises(FileExistsError):
            self.module.build(output)
        self.assertEqual(output.read_bytes(), b"existing release")

    def test_missing_source_prevents_partial_archive(self):
        source = self.fixture_source()
        (source / "global/SKILL.md").unlink()
        output = self.base / "missing" / "release.zip"
        with self.assertRaises(ValueError):
            self.module.build(output, source=source)
        self.assertFalse(output.parent.exists())

    def test_redirected_source_is_rejected(self):
        source = self.fixture_source()
        redirected = source / "global/SKILL.md"
        redirected.unlink()
        secret = self.base / "private.txt"
        secret.write_text("private", encoding="utf-8")
        try:
            redirected.symlink_to(secret)
        except OSError as exc:
            self.skipTest("Host does not permit test symlinks: " + str(exc))
        output = self.base / "release.zip"
        with self.assertRaises(ValueError):
            self.module.build(output, source=source)
        self.assertFalse(output.exists())

    def test_unpacked_install_context_and_uninstall_preserve_memories(self):
        output = self.module.build(self.base / "jason-memory-global.zip")
        unpacked = self.base / "unpacked"
        with ZipFile(output) as archive:
            archive.extractall(unpacked)
        package = unpacked / "jason-memory-global"
        profile = self.base / "fake-profile"
        home = self.base / "shared-home"
        installer = [sys.executable, str(package / "global/install.py"),
                     "--user-home", str(profile), "--home", str(home)]
        result = subprocess.run(installer, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = [sys.executable, str(home / "framework/global_store.py"), "--home", str(home)]
        project = self.base / "project A"
        project.mkdir()
        result = subprocess.run(runtime + ["context", "--project", str(project)],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((home / "memory/shared/MEMORY.md").is_file())
        modern = subprocess.run([sys.executable, str(home / 'framework/tools/memory_runtime.py'),
                                '--config', str(home / 'framework/memory-config.json'),
                                '--project', str(project), 'context'],
                               capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(modern.returncode, 0, modern.stderr)
        self.assertIn('semantic_review_needed', modern.stdout)
        note = home / "memory/shared/do-not-remove.md"
        note.write_text("user note", encoding="utf-8")
        result = subprocess.run(installer + ["--uninstall"], capture_output=True,
                                text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(note.read_text(encoding="utf-8"), "user note")
        self.assertNotIn("jason-memory-global:start", (profile / ".codex/AGENTS.md").read_text())
        self.assertNotIn("jason-memory-global:start", (profile / ".claude/CLAUDE.md").read_text())

    @unittest.skipUnless(os.name == "nt", "Windows launcher test")
    def test_windows_launcher_dry_run_uses_unpacked_installer(self):
        output = self.module.build(self.base / "launcher.zip")
        with ZipFile(output) as archive:
            archive.extractall(self.base / "launcher")
        launcher = self.base / "launcher/jason-memory-global/INSTALL.cmd"
        profile = self.base / "launcher-profile"
        home = self.base / "launcher-home"
        result = subprocess.run(["cmd", "/d", "/c", str(launcher), "--dry-run",
                                 "--user-home", str(profile), "--home", str(home)],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"dry_run": true', result.stdout)
        self.assertFalse(profile.exists())
        self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()
