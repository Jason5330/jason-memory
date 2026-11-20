"""Regression tests for #95-#99 and the #86/#87 follow-ups. Invariants: a damaged progress file never stops
recording; a rebuild never publishes an empty memory; the uninstaller never wipes its own source checkout;
hook commands work in every shell Claude Code uses; a path Windows cannot open is refused up front."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import memory_runtime as runtime  # noqa: E402
import memory_ledger as ledger  # noqa: E402
import memory_dream as dream  # noqa: E402
import claude_memory_hook as hook  # noqa: E402
import hook_settings  # noqa: E402
import install_hooks  # noqa: E402
from worker_identity import as_worker  # noqa: E402


class Base(unittest.TestCase):
    ledger_cfg = {'enabled': True, 'foreground_writes': False, 'dream_every': 20}

    def setUp(self):
        self.spawned, original = [], ledger._spawn
        ledger._spawn = self.spawned.append
        self.addCleanup(setattr, ledger, '_spawn', original)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve()
        self.config = self.project / '.jason-memory.json'
        self.config.write_text(json.dumps({'version': 1, 'mode': 'project', 'memory_root': '.jason-memory',
                                           'ledger': self.ledger_cfg}), encoding='utf-8')
        with runtime.opened(self.config) as (_, _, roots):
            self.root = roots['project']
        self.env = dict(os.environ, PYTHONUTF8='1')
        for key in ('JASON_DREAM', 'JASON_DREAM_OWNER'):
            self.env.pop(key, None)

    def cli(self, script, *args):
        return subprocess.run([sys.executable, str(ROOT / 'tools' / script), '--config', str(self.config), *args],
                              capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=120)

    def remember(self, key, value, kind='feedback'):
        with as_worker(self.config, env={}), runtime.opened(self.config):
            return runtime.remember(self.root, 'user', key, value, '使用者要求', kind=kind)

    def records(self):
        return len(ledger.records(self.root))


class DamagedProgressFileTests(Base):
    def test_unreadable_state_file_is_kept_and_recording_continues(self):
        """#95"""
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': 'before'})
        state = ledger.raw_root(self.root) / 'dream.json'
        for damage in ('{not json', '[1, 2]', '{"cursor": "x"}', '{"cursor": -3}'):
            with self.subTest(damage=damage):
                state.write_text(damage, encoding='utf-8')
                before = self.records()
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'tools/claude_memory_hook.py'), '--config', str(self.config),
                     '--jason-hook'], input=json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 'qa',
                                                        'cwd': str(self.project), 'prompt': 'after ' + damage}),
                    capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.records(), before + 1, result.stdout + result.stderr)
                kept = sorted(state.parent.glob('dream.json.corrupt-*'))
                self.assertTrue(any(p.read_text(encoding='utf-8') == damage for p in kept), kept)
                report = json.loads(self.cli('memory_ledger.py', 'pending').stdout)
                self.assertEqual(report['cursor'], 0)
                self.assertIn('dream.json.corrupt-', report['state_reset']['kept'])

    def test_a_disk_error_is_not_mistaken_for_damage(self):
        state = ledger.raw_root(self.root) / 'dream.json'
        state.write_text('{"cursor": 0}', encoding='utf-8')
        with mock.patch.object(runtime, 'read_json', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                ledger.dream_state(self.root)
        self.assertTrue(state.exists())
        self.assertEqual(list(state.parent.glob('dream.json.corrupt-*')), [])


class RebuildNeverEmptiesMemoryTests(Base):
    def notes(self):
        return sorted(runtime.notes(self.root))

    def test_empty_ledger_refuses_and_keeps_notes(self):
        """#96"""
        self.remember('qa.keep', 'QA_KEEP_96')
        before = self.notes()
        result = dream.rebuild(self.config, self.project, ledger.settings(self.config), runner=lambda c, t: 0)
        self.assertNotEqual(result['exit_code'], 0)
        self.assertIn('nothing to replay', result['error'])
        self.assertFalse(result['rebuilt'])
        self.assertEqual(self.notes(), before)

    def test_replay_that_reproduces_nothing_refuses_and_keeps_notes(self):
        self.remember('qa.keep', 'QA_KEEP_96')
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': 'something'})
        before = self.notes()
        ok = lambda command, text: (print(dream.STATUS_OK), 0)[1]  # noqa: E731 - an agent that saves nothing
        result = dream.rebuild(self.config, self.project, ledger.settings(self.config), runner=ok)
        self.assertNotEqual(result['exit_code'], 0, result)
        self.assertIn('produced no notes', result['error'])
        self.assertEqual(self.notes(), before)


class SourceCheckoutUninstallTests(unittest.TestCase):
    def test_uninstall_refuses_inside_the_framework_source_checkout(self):
        """#97: only in a temporary copy - never the real checkout."""
        work = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, work, True)
        checkout = work / 'checkout'
        for name in ('tools', 'global', 'templates'):
            shutil.copytree(ROOT / name, checkout / name, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy(ROOT / 'uninstall-manifest.json', checkout)
        (checkout / '.git').mkdir()
        (checkout / '.github/release-notes').mkdir(parents=True)
        (checkout / '.jason-memory.json').write_text(json.dumps({'version': 1, 'mode': 'project',
                                                                 'memory_root': '.jason-memory'}), encoding='utf-8')
        (checkout / '.jason-memory/feedback').mkdir(parents=True)
        keep = checkout / '.jason-memory/feedback/keep.md'
        keep.write_text('QA_KEEP_97', encoding='utf-8')
        shutil.copy(ROOT / 'templates/MEMORY.md', checkout / '.jason-memory/MEMORY.md')
        home = work / 'profile'
        home.mkdir()
        run = lambda *extra: subprocess.run(  # noqa: E731
            [sys.executable, str(checkout / 'tools/purge_memory.py'), '--user-home', str(home), *extra],
            capture_output=True, text=True, encoding='utf-8', timeout=120)
        refused = run()
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn('source checkout', refused.stderr)
        self.assertTrue(keep.exists())
        self.assertTrue((checkout / 'tools/memory_runtime.py').exists())
        listed = run('--dry-run')
        self.assertIn(listed.returncode, (0, 2), listed.stderr)  # 2: the running tool itself remains.
        self.assertIn(str(keep), listed.stdout.replace('\\\\', '\\'))
        self.assertTrue(keep.exists())
        # An installed package (no release notes) still uninstalls on a double click.
        shutil.rmtree(checkout / '.github')
        done = run()
        self.assertIn(done.returncode, (0, 2), done.stderr)
        self.assertFalse(keep.exists())


class ShellQuotingTests(unittest.TestCase):
    def test_hook_commands_use_double_quotes(self):
        """#98: no POSIX single quotes, which cmd.exe does not understand."""
        for path in ('C:/Users/John Doe/proj', 'D:/我的專案/x', 'C:/a (b)/c & d', '/home/u/p'):
            with self.subTest(path=path):
                quoted = hook_settings.shell_quote(path + '/tools/claude_memory_hook.py')
                self.assertNotIn("'", quoted)
        self.assertEqual(hook_settings.shell_quote('C:/plain/x.py'), 'C:/plain/x.py')
        self.assertEqual(hook_settings.shell_quote('/a/$HOME/`x`'), '"/a/\\$HOME/\\`x\\`"')
        with self.assertRaises(ValueError):
            hook_settings.shell_quote('a"b')

    def test_quoted_command_runs_through_a_real_shell(self):
        bash = shutil.which('bash')
        if os.name == 'nt' or bash is None:
            self.skipTest('Git Bash is checked in the release QA with real Claude Code')
        folder = Path(tempfile.mkdtemp()).resolve() / 'a b 中文 (x) & y'
        self.addCleanup(shutil.rmtree, folder.parent, True)
        folder.mkdir()
        script = folder / 'echo.py'
        script.write_text('import sys\nprint(sys.argv[1])\n', encoding='utf-8')
        command = hook_settings.shell_join([sys.executable, script.as_posix(), 'ok $HOME'])
        result = subprocess.run([bash, '-c', command], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.stdout.strip(), 'ok $HOME', result.stderr)

    def test_command_prefix_prefers_a_short_python_when_it_is_the_same_interpreter(self):
        """#87 follow-up: sandboxed agents ask for approval of an absolute python.exe path."""
        same = mock.patch('shutil.which', return_value=sys.executable)
        with same:
            self.assertEqual(hook_settings.python_command(), 'python')
        with mock.patch('shutil.which', return_value=None):
            self.assertEqual(hook_settings.python_command(), Path(sys.executable).as_posix())


class LongPathTests(unittest.TestCase):
    def test_paths_windows_cannot_open_are_refused_up_front(self):
        """#99"""
        with mock.patch.object(hook_settings, 'long_paths_enabled', return_value=False):
            self.assertIsNone(hook_settings.path_too_long('C:/short', ['tools/claude_memory_hook.py']))
            problem = hook_settings.path_too_long('C:/' + 'x' * 240, ['tools/claude_memory_hook.py'])
            self.assertIn('Path too long for Windows', problem)
            self.assertIn('LongPathsEnabled', problem)
        with mock.patch.object(hook_settings, 'long_paths_enabled', return_value=True):
            self.assertIsNone(hook_settings.path_too_long('C:/' + 'x' * 240, ['tools/claude_memory_hook.py']))

    def test_installer_refuses_a_project_too_deep_for_its_store(self):
        work = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, work, True)
        (work / '.jason-memory.json').write_text(json.dumps({'version': 1, 'mode': 'project',
                                                             'memory_root': '.jason-memory'}), encoding='utf-8')
        output = io.StringIO()
        with mock.patch.object(hook_settings, 'long_paths_enabled', return_value=False), \
                mock.patch.object(hook_settings, 'MAX_PATH', len(str(work)) + 20), \
                contextlib.redirect_stderr(output), contextlib.redirect_stdout(io.StringIO()):
            code = install_hooks.main(['--project', str(work), '--agent', 'claude', '--dry-run'])
        self.assertEqual(code, 1)
        self.assertIn('Path too long for Windows', output.getvalue())

    def test_runtime_names_the_windows_limit_instead_of_a_bare_error(self):
        error = OSError(206, 'The filename or extension is too long')
        error.winerror = 206
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['memory_runtime.py', '--config', 'x.json', 'index']), \
                mock.patch.object(runtime, 'opened', side_effect=error), \
                mock.patch.object(sys, 'stderr', out):
            self.assertEqual(runtime.main(), 1)
        self.assertIn('LongPathsEnabled', out.getvalue())


class FactDriftTests(Base):
    def test_body_that_no_longer_states_its_fact_is_reported_without_blocking_saves(self):
        """#86 follow-up: prose edited to the opposite of the recorded fact."""
        saved = self.remember('qa.exceptions', '禁止靜默吞掉例外。')
        path = Path(saved['paths'][0])
        text = path.read_text(encoding='utf-8')
        head, fence, facts = text.partition('```jason-facts')
        path.write_text(head.replace('禁止靜默吞掉例外。', '可以放心用 except: pass。').replace(
            'description: "可以放心用 except: pass。"', 'description: "禁止靜默吞掉例外。"') + fence + facts,
            encoding='utf-8')
        with runtime.opened(self.config) as (_, _, roots):
            audit = runtime.context(roots)['project']['audit']
        self.assertEqual([d['path'] for d in audit['fact_drift']], [path.relative_to(self.root).as_posix()])
        snap = hook.handle({'hook_event_name': 'SessionStart', 'session_id': 'qa', 'cwd': str(self.project)},
                           self.config)['hookSpecificOutput']['additionalContext']
        self.assertIn('FACT DRIFT', snap)
        self.remember('qa.other', 'QA_OTHER_SAVE')  # Other saves still work.

    def test_hand_written_and_unedited_notes_are_not_flagged(self):
        self.remember('qa.fine', 'QA fine value')
        with as_worker(self.config, env={}), runtime.opened(self.config) as (_, _, roots):
            index = runtime.read_index(self.root)
            runtime.apply(self.root, {'expected_revision': runtime.revision_for(runtime.notes(self.root), index),
                                      'updates': {'feedback/hand.md': (
                                          '---\nname: hand\ndescription: 色彩偏好\ntype: feedback\ncreated: 2026-01-01\n'
                                          'updated: 2026-01-01\n---\n\n喜歡藍色系。\n\nWhy: 使用者說\n\nHow to apply: 配色\n\n'
                                          '```jason-facts\n[{"subject": "user", "predicate": "color", "value": "blue"}]\n'
                                          '```\n')}, 'retire': []})
            audit = runtime.context(roots)['project']['audit']
        self.assertEqual(audit['fact_drift'], [])


if __name__ == '__main__':
    unittest.main()
