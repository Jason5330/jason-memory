"""End-to-end: the released global ZIP installs background memory mode for Claude Code and Codex in
one step, records conversations from any folder, and its UNINSTALL removes everything including memory."""
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
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

    def test_foreign_notifier_is_kept_and_reported_not_silently_skipped(self):
        """#21: background mode must not claim Codex recording that is not wired up."""
        self.toml.write_bytes(b'notify = ["my-existing-notifier"]\n')
        plan = json.loads(self.install().stdout)
        self.assertTrue(plan["codex_notify"].startswith("skipped"))
        self.assertIn("NOT connected", plan["warnings"][0])
        self.assertEqual(self.toml.read_bytes(), b'notify = ["my-existing-notifier"]\n')
        self.assertIn("NOT recorded yet", (self.profile / ".codex/AGENTS.md").read_text(encoding="utf-8"))

    def test_global_takes_over_a_project_dispatcher(self):
        project_tools = self.base / "project A/tools"
        project_tools.mkdir(parents=True)
        for name in ("codex_notify.py", "memory_ledger.py"):
            (project_tools / name).write_text("", encoding="utf-8")
        self.toml.write_bytes(('notify = ' + json.dumps(["python", (project_tools / "codex_notify.py").as_posix()])
                               + '\n').encode())
        plan = json.loads(self.install().stdout)
        self.assertEqual(plan["codex_notify"], "installed")
        self.assertEqual([w for w in plan["warnings"] if "no agent CLI" not in w], [])  # CLI probe depends on host
        data = self.toml.read_text(encoding="utf-8")
        self.assertIn((self.home / "framework/tools/codex_notify.py").as_posix(), data)
        self.assertNotIn("project A", data)

    def test_resume_uses_new_workspace_without_moving_nested_tool_events(self):
        self.install()
        a, b = self.base / 'workspace a', self.base / 'workspace b'
        nested = a / 'subfolder'
        nested.mkdir(parents=True)
        b.mkdir()
        config = self.home / 'framework/memory-config.json'
        tools = config.parent / 'tools'
        self.env['JASON_DREAM'] = '1'
        for project, value in ((a, 'QA-PRIVATE-ALPHA'), (b, 'QA-PRIVATE-BETA')):
            self.run_py(tools / 'memory_runtime.py', '--config', config, '--project', project,
                        'remember', '--scope', 'project', '--subject', 'qa', '--key', 'setting',
                        '--value', value, '--why', 'Synthetic test')
        self.env.pop('JASON_DREAM')

        def event(project, name, **data):
            payload = dict(data, cwd=str(project), session_id='qa-resume', hook_event_name=name)
            return self.run_py(tools / 'claude_memory_hook.py', '--config', config, '--jason-hook',
                               stdin=json.dumps(payload)).stdout

        self.assertIn('QA-PRIVATE-ALPHA', event(a, 'SessionStart', source='startup'))
        event(nested, 'UserPromptSubmit', prompt='QA-ORIGINAL-WORKSPACE')
        resumed = event(b, 'SessionStart', source='resume')
        self.assertIn('QA-PRIVATE-BETA', resumed)
        self.assertNotIn('QA-PRIVATE-ALPHA', resumed)
        event(b, 'UserPromptSubmit', prompt='QA-RESUMED-WORKSPACE')
        for project, marker in ((a, 'QA-ORIGINAL-WORKSPACE'), (b, 'QA-RESUMED-WORKSPACE')):
            replay = self.run_py(tools / 'memory_ledger.py', '--config', config, '--project', project, 'replay')
            self.assertEqual([record['data']['text'] for record in json.loads(replay.stdout)], [marker])

    def test_shared_note_source_resolves_to_the_right_project_ledger(self):
        """#30: both projects have a seq 1; a reference still finds B's record from A."""
        self.install()
        config = self.home / "framework/memory-config.json"
        tools = self.home / "framework/tools"
        a, b = self.base / "A", self.base / "B"
        a.mkdir()
        b.mkdir()
        for folder, text in ((a, "QA: keep timestamps in UTC"), (b, "Across all projects, use QA_SHARED_UTC.")):
            self.run_py(tools / "memory_ledger.py", "--config", config, "--project", folder, "append",
                        "--kind", "prompt", "--data", json.dumps({"text": text}))
        prompt = self.run_py(tools / "memory_dream.py", "--config", config, "--project", b, "prompt").stdout
        ref = prompt.split("[ref ")[1].split(" ")[0]
        store, seq, digest = ref.split(":")
        self.assertEqual(seq, "1")
        self.assertEqual(len(store), 16)
        found = json.loads(self.run_py(tools / "memory_ledger.py", "--config", config, "--project", a,
                                       "source", "--ref", ref).stdout)
        self.assertEqual(found["record"]["data"]["text"], "Across all projects, use QA_SHARED_UTC.")
        self.assertIn(store, found["ledger"])
        bad = self.run_py(tools / "memory_ledger.py", "--config", config, "--project", a, "source",
                          "--ref", store + ":1:000000000000", code=1)
        self.assertIn("hash does not match", bad.stderr)

    def test_export_cannot_write_into_another_project_store(self):
        """#29: global mode guards every store under <home>/memory, even through '..'."""
        self.install()
        config = self.home / "framework/memory-config.json"
        workspace = self.base / "W"
        workspace.mkdir()
        sneaky = self.base / "W" / ".." / "shared home" / "memory" / "shared" / "x.md"
        result = self.run_py(self.home / "framework/tools/memory_runtime.py", "--config", config, "--project",
                             workspace, "export", "--output", sneaky, code=1)
        self.assertIn("outside the memory store", result.stderr)
        self.assertFalse((self.home / "memory/shared/x.md").exists())

    def test_rebuild_upgrades_legacy_shared_source_to_a_full_reference(self):
        """#32: a legacy `ledger seq 1` shared note gets the full store:seq:hash reference on rebuild,
        and another project can resolve it with source --ref."""
        worker = self.base / 'ref_worker.py'
        worker.write_text('''
import re
import subprocess
import sys
from pathlib import Path

text = sys.stdin.buffer.read().decode('utf-8')
prefix = next(line for line in text.splitlines() if line.startswith('指令前綴（Bash）：'))
target = re.search(r"--config (?:'([^']+)'|(\\S+))", prefix)
config = next(value for value in target.groups() if value)
ref = re.search(r'\\[ref (\\S+) ', text).group(1)
subprocess.run([sys.executable, sys.argv[1], '--config', config, '--project', str(Path.cwd()),
                'remember', '--scope', 'global', '--subject', 'qa', '--key', 'legacy',
                '--value', 'QA_SHARED_LEGACY_UTC', '--why', '來源：ledger ' + ref], check=True)
print('JASON_DREAM_STATUS: ok')
''', encoding='utf-8')
        self.install()
        framework = self.home / 'framework'
        config = framework / 'memory-config.json'
        runtime, ledger, dream = (framework / 'tools' / n for n in ('memory_runtime.py', 'memory_ledger.py', 'memory_dream.py'))
        cfg = json.loads(config.read_text(encoding='utf-8'))
        cfg['ledger']['dream_agent'] = [sys.executable, str(worker), str(runtime)]
        config.write_text(json.dumps(cfg), encoding='utf-8')
        a, b = self.base / 'A', self.base / 'B'
        a.mkdir()
        b.mkdir()
        self.run_py(ledger, '--config', config, '--project', b, 'append', '--kind', 'prompt',
                    '--data', json.dumps({'text': 'Across all projects, use QA_SHARED_LEGACY_UTC.'}))
        self.env['JASON_DREAM'] = '1'
        try:
            self.run_py(runtime, '--config', config, '--project', b, 'remember', '--scope', 'global',
                        '--subject', 'qa', '--key', 'legacy', '--value', 'QA_SHARED_LEGACY_UTC',
                        '--why', '來源：ledger seq 1')
        finally:
            del self.env['JASON_DREAM']
        rebuilt = json.loads(self.run_py(dream, '--config', config, '--project', b, 'rebuild', '--yes').stdout.splitlines()[-1])
        self.assertTrue(rebuilt['rebuilt'], rebuilt)
        shared = ''.join(p.read_text(encoding='utf-8') for p in (self.home / 'memory/shared').rglob('*.md')
                         if p.name != 'MEMORY.md' and 'archive' not in p.parts)
        ref = re.search(r'ledger ((?:[0-9a-f]{16}):1:[0-9a-f]{12})', shared).group(1)
        self.assertNotIn('來源：ledger seq 1', shared)
        found = json.loads(self.run_py(ledger, '--config', config, '--project', a, 'source', '--ref', ref).stdout)
        self.assertEqual(found['record']['data']['text'], 'Across all projects, use QA_SHARED_LEGACY_UTC.')
        # #33: a wrong hash is refused and the shared note keeps the verified source.
        bad = ref[:-1] + ('0' if ref[-1] != '0' else '1')
        self.env['JASON_DREAM'] = '1'
        try:
            failed = self.run_py(runtime, '--config', config, '--project', b, 'remember', '--scope', 'global',
                                 '--subject', 'qa', '--key', 'legacy', '--value', 'QA_SHARED_LEGACY_UTC',
                                 '--why', '來源：ledger ' + bad, code=1)
        finally:
            del self.env['JASON_DREAM']
        self.assertIn('does not resolve', failed.stdout + failed.stderr)
        after = ''.join(p.read_text(encoding='utf-8') for p in (self.home / 'memory/shared').rglob('*.md')
                        if p.name != 'MEMORY.md' and 'archive' not in p.parts)
        self.assertIn(ref, after)
        self.assertNotIn(bad, after)
        self.run_py(ledger, '--config', config, '--project', a, 'source', '--ref', ref)

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

    def test_rebuild_restores_shared_facts_and_preserves_other_projects(self):
        worker = self.base / 'worker.py'
        worker.write_text('''
import re
import subprocess
import sys
from pathlib import Path

text = sys.stdin.buffer.read().decode('utf-8')
prefix = next(line for line in text.splitlines() if line.startswith('指令前綴（Bash）：'))
target = re.search(r"--config (?:'([^']+)'|(\\S+))", prefix)
config = next(value for value in target.groups() if value)
scope = 'global' if '全局版範圍' in text else 'project'
for scope, key, value in ((scope, 'shared', 'QA_REBUILT_SHARED'), ('project', 'local', 'QA_REBUILT_LOCAL')):
    subprocess.run([sys.executable, sys.argv[1], '--config', config, '--project', str(Path.cwd()),
                    'remember', '--scope', scope, '--type', 'feedback', '--subject', 'qa', '--key', key,
                    '--value', value, '--why', 'ledger seq 1'], check=True)
print('JASON_DREAM_STATUS: ok')
''', encoding='utf-8')
        self.install()
        framework = self.home / 'framework'
        config = framework / 'memory-config.json'
        runtime = framework / 'tools/memory_runtime.py'
        ledger = framework / 'tools/memory_ledger.py'
        dream = framework / 'tools/memory_dream.py'
        cfg = json.loads(config.read_text(encoding='utf-8'))
        cfg['ledger']['dream_agent'] = [sys.executable, str(worker), str(runtime)]
        config.write_text(json.dumps(cfg), encoding='utf-8')
        a, b = self.base / 'project A', self.base / 'project B'
        a.mkdir()
        b.mkdir()

        def run(script, project, *args):
            return self.run_py(script, '--config', config, '--project', project, *args)

        def context(project):
            return json.loads(run(runtime, project, 'context').stdout)['stores']

        self.env['JASON_DREAM'] = '1'
        try:
            run(runtime, b, 'remember', '--scope', 'global', '--subject', 'qa', '--key', 'unrelated',
                '--value', 'QA_SHARED_FROM_B', '--why', 'B source')
            run(runtime, b, 'remember', '--scope', 'project', '--subject', 'qa', '--key', 'b-local',
                '--value', 'QA_B_LOCAL', '--why', 'B source')
        finally:
            del self.env['JASON_DREAM']
        run(ledger, a, 'append', '--kind', 'prompt', '--data',
            json.dumps({'text': 'Across all projects use QA_REBUILT_SHARED. In A only use QA_REBUILT_LOCAL.'}))
        original = run(ledger, a, 'replay').stdout
        rebuilt = json.loads(run(dream, a, 'rebuild', '--yes').stdout.splitlines()[-1])
        self.assertTrue(rebuilt['rebuilt'])
        for stores in (context(a), context(b)):
            self.assertIn('QA_REBUILT_SHARED', str(stores['global']['notes']))
            self.assertIn('QA_SHARED_FROM_B', str(stores['global']['notes']))
            self.assertNotIn('QA_REBUILT_SHARED', str(stores['project']['notes']))
        self.assertIn('QA_REBUILT_LOCAL', str(context(a)['project']['notes']))
        self.assertIn('QA_B_LOCAL', str(context(b)['project']['notes']))
        self.assertNotIn('QA_REBUILT_LOCAL', str(context(b)['project']['notes']))
        self.assertEqual(run(ledger, a, 'replay').stdout, original)

    def test_global_rebuild_recovers_both_scopes_from_an_interrupted_publish(self):
        sys.path.insert(0, str(ROOT / 'tools'))
        self.addCleanup(sys.path.remove, str(ROOT / 'tools'))
        import memory_runtime as runtime

        self.install()
        config = self.home / 'framework/memory-config.json'
        a, b = self.base / 'project A', self.base / 'project B'
        a.mkdir()
        b.mkdir()
        with runtime.opened(config, a) as (_, _, roots):
            plans = {}
            for scope, root in roots.items():
                stage = self.base / ('seed-' + scope)
                stage.mkdir()
                (stage / 'MEMORY.md').write_text(runtime.read_index(root), encoding='utf-8')
                runtime.remember(stage, 'qa', 'scope', 'QA_' + scope.upper(), 'Synthetic test', scope)
                plans[scope] = {'expected_revision': runtime.revision(root),
                                'updates': runtime.notes(stage), 'retire': []}

            recover = runtime.recover

            def interrupt(root, _prepared=None):
                if root == roots['project']:
                    raise OSError('simulated interruption')
                return recover(root, _prepared=_prepared)

            with patch.object(runtime, 'recover', side_effect=interrupt):
                with self.assertRaisesRegex(OSError, 'simulated interruption'):
                    runtime.commit_global_rebuild(self.home, roots, plans)

        # A different project opening the shared store also completes the pending rebuild.
        with runtime.opened(config, b) as (_, _, reopened):
            self.assertIn('QA_GLOBAL', str(runtime.notes(reopened['global'])))
            self.assertNotIn('QA_PROJECT', str(runtime.notes(reopened['project'])))
            self.assertIn('QA_PROJECT', str(runtime.notes(roots['project'])))
        self.assertFalse((self.home / '.rebuild-transaction.json').exists())

    def test_global_rebuild_keeps_a_concurrent_shared_update(self):
        sys.path.insert(0, str(ROOT / 'tools'))
        self.addCleanup(sys.path.remove, str(ROOT / 'tools'))
        import memory_dream as dream
        import memory_ledger as ledger
        import memory_runtime as runtime

        self.install()
        config = self.home / 'framework/memory-config.json'
        a, b = self.base / 'project A', self.base / 'project B'
        a.mkdir()
        b.mkdir()
        with runtime.opened(config, a) as (_, _, roots):
            ledger.append(roots['project'], 'prompt', {'text': 'Synthetic test'})

        def concurrent_write(command, prompt):
            with runtime.opened(config, b) as (_, _, roots):
                runtime.remember(roots['global'], 'qa', 'concurrent', 'QA_CONCURRENT', 'B source', 'global')
            return 0

        result = dream.rebuild(config, a, ledger.settings(config), concurrent_write)
        self.assertEqual(result['exit_code'], 75)
        self.assertIn('changed during rebuild', result['error'])
        with runtime.opened(config, b) as (_, _, roots):
            self.assertIn('QA_CONCURRENT', str(runtime.notes(roots['global'])))


if __name__ == "__main__":
    unittest.main()
