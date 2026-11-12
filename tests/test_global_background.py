"""End-to-end: the released global ZIP installs background memory mode for Claude Code and Codex in
one step, records conversations from any folder, and its UNINSTALL removes everything including memory."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "tools/build_global_download.py"


@unittest.skipUnless(BUILDER.is_file(), "source checkout only")
class GlobalBackgroundModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        spec = importlib.util.spec_from_file_location("global_build_bg", BUILDER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with ZipFile(module.build(self.base / "g.zip")) as archive:
            archive.extractall(self.base / "unpacked")
        self.package = self.base / "unpacked/jason-memory-global"
        self.profile = self.base / "profile"
        (self.profile / ".codex").mkdir(parents=True)
        self.toml = self.profile / ".codex/config.toml"
        self.toml.write_bytes(b'model = "x"\n')
        self.home = self.base / "shared home"
        self.env = dict(os.environ, PYTHONUTF8="1")
        self.env.pop("JASON_DREAM", None)

    def run_py(self, script, *args, stdin=None, code=0):
        result = subprocess.run([sys.executable, str(script), *map(str, args)], input=stdin,
                                capture_output=True, text=True, encoding="utf-8", env=self.env, timeout=60)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def install(self, *extra):
        return self.run_py(self.package / "global/install.py", "--user-home", self.profile,
                           "--home", self.home, *extra)

    def test_one_install_background_mode_for_both_hosts_then_one_click_purge(self):
        self.install()
        config = json.loads((self.home / "framework/memory-config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["ledger"]["foreground_writes"], False)
        notify = self.home / "framework/tools/codex_notify.py"
        self.assertIn(notify.as_posix(), self.toml.read_text(encoding="utf-8"))
        self.assertIn("claude_memory_hook.py", (self.profile / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertIn("background memory mode", (self.profile / ".codex/AGENTS.md").read_text(encoding="utf-8"))

        # Any new folder works: Codex notify and the Claude hook both record into the global store.
        workspace = self.base / "Desktop" / "new project"
        workspace.mkdir(parents=True)
        payload = {"type": "agent-turn-complete", "thread-id": "t1", "cwd": str(workspace),
                   "input-messages": ["以後時間用 24 小時制"], "last-assistant-message": "好的"}
        self.run_py(notify, json.dumps(payload, ensure_ascii=False))
        hook = [self.home / "framework/tools/claude_memory_hook.py", "--config",
                self.home / "framework/memory-config.json", "--jason-hook"]
        start = self.run_py(*hook, stdin=json.dumps({"hook_event_name": "SessionStart", "session_id": "c1",
                                                    "cwd": str(workspace)}))
        self.assertIn("BACKGROUND MEMORY MODE", json.loads(start.stdout)["hookSpecificOutput"]["additionalContext"])
        self.run_py(*hook, stdin=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "c1",
                                            "cwd": str(workspace), "prompt": "報表標題用淡紫色"}))
        ledgers = list((self.home / "memory/projects").glob("*/.raw/ledger.jsonl"))
        self.assertEqual(len(ledgers), 1)
        text = ledgers[0].read_text(encoding="utf-8")
        self.assertIn("24 小時制", text)
        self.assertIn("淡紫色", text)

        # Foreground writes are refused in a conversation.
        runtime = self.home / "framework/tools/memory_runtime.py"
        refused = self.run_py(runtime, "--config", self.home / "framework/memory-config.json", "--project",
                              workspace, "remember", "--subject", "user", "--key", "k", "--value", "v",
                              "--why", "w", code=1)
        self.assertIn("background memory mode", refused.stderr)

        # One click removes framework, all memory (shared, projects, ledgers) and host wiring.
        self.run_py(self.package / "tools/purge_memory.py", "--project", self.package,
                    "--user-home", self.profile, "--home", self.home)
        self.assertFalse(self.home.exists())
        self.assertEqual(self.toml.read_bytes(), b'model = "x"\n')
        settings = self.profile / ".claude/settings.json"
        self.assertNotIn("claude_memory_hook.py", settings.read_text(encoding="utf-8") if settings.exists() else "")
        agents = self.profile / ".codex/AGENTS.md"
        self.assertNotIn("jason-memory", agents.read_text(encoding="utf-8") if agents.exists() else "")

    def test_foreground_mode_keeps_old_flow_without_notify(self):
        self.install("--mode", "foreground")
        config = json.loads((self.home / "framework/memory-config.json").read_text(encoding="utf-8"))
        self.assertNotIn("ledger", config)
        self.assertEqual(self.toml.read_bytes(), b'model = "x"\n')
        self.assertIn("remember --scope SCOPE", (self.profile / ".codex/AGENTS.md").read_text(encoding="utf-8"))

    def test_switching_back_to_foreground_removes_notify(self):
        self.install()
        self.install("--mode", "foreground")
        self.assertEqual(self.toml.read_bytes(), b'model = "x"\n')


if __name__ == "__main__":
    unittest.main()
