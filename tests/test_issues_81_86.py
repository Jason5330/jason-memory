"""Regression tests for issues #81-#86 and the #73 follow-up, on stores large enough to hit every
budget (the #72 hint was only ever tested below the 6000-byte index budget - see #82)."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import memory_runtime as runtime  # noqa: E402
import memory_ledger as ledger  # noqa: E402
import memory_dream as dream  # noqa: E402
import claude_memory_hook as hook  # noqa: E402
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
        self.write_config(self.ledger_cfg)
        with runtime.opened(self.config) as (_, _, roots):
            self.root = roots['project']
        self.env = dict(os.environ, PYTHONUTF8='1')
        for key in ('JASON_DREAM', 'JASON_DREAM_OWNER'):
            self.env.pop(key, None)

    def write_config(self, ledger_cfg):
        self.config.write_text(json.dumps({'version': 1, 'mode': 'project', 'memory_root': '.jason-memory',
                                           'ledger': ledger_cfg}), encoding='utf-8')

    def cli(self, script, *args, env=None):
        return subprocess.run([sys.executable, str(ROOT / 'tools' / script), '--config', str(self.config), *args],
                              capture_output=True, text=True, encoding='utf-8', env=env or self.env, timeout=120)

    def remember(self, key, value, kind='feedback'):
        with as_worker(self.config, env={}), runtime.opened(self.config):
            return runtime.remember(self.root, 'user', key, value, '使用者要求', kind=kind)

    def snapshot(self):
        return hook.handle({'hook_event_name': 'SessionStart', 'session_id': 'qa', 'cwd': str(self.project)},
                           self.config)['hookSpecificOutput']['additionalContext']

    def index_rows(self):
        return [line for line in (self.root / 'MEMORY.md').read_text(encoding='utf-8').splitlines()
                if line.startswith('- [')]


class LargeStoreTests(Base):
    """60 notes with long summaries: index far above 6000 bytes, always-apply bodies above the snapshot budget."""

    @classmethod
    def setUpClass(cls):
        # Built once: the tests below only read the store (snapshots write hook state only).
        cls.shared = Base('run')
        cls.shared.setUp()
        for i in range(56):
            kind = ('project', 'reference', 'feedback')[i % 3]
            cls.shared.remember('qa.large' + str(i), 'QA large store rule ' + str(i) + ' ' + '很長的摘要內容 ' * 12, kind)
        for i in range(2):
            cls.shared.remember('qa.identity' + str(i), 'QA_USER_IDENTITY_' + str(i) + ' 物流主管', 'user')
        cls.shared.remember('qa.zz', 'QA last feedback', 'feedback')
        cls.shared.remember('qa.zz2', 'QA last feedback 2', 'feedback')

    @classmethod
    def tearDownClass(cls):
        cls.shared.doCleanups()

    def setUp(self):
        for name in ('project', 'config', 'root', 'env', 'spawned'):
            setattr(self, name, getattr(self.shared, name))
        self.assertGreater(len((self.root / 'MEMORY.md').read_bytes()), 6000)

    def test_the_snapshot_hint_command_really_returns_every_row(self):
        """#82: run exactly what the snapshot says, and get the full list."""
        snap = self.snapshot()
        self.assertIn('INDEX TRUNCATED', snap)
        prefix = shlex.split(re.search(r'^Command prefix \(Bash\): (.+)$', snap, re.M).group(1))
        hint = re.search(r'for the full list run: Command prefix \+ (index[^(]*?) \(', snap).group(1).split()
        result = subprocess.run(prefix + hint, capture_output=True, text=True, encoding='utf-8', env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        store = json.loads(result.stdout)['stores']['project']
        rows = [line for line in store['index'].splitlines() if line.startswith('- [')]
        self.assertEqual((len(rows), store['truncated']), (len(self.index_rows()), False))

    def test_bounded_index_says_how_many_rows_are_missing(self):
        """#82"""
        store = json.loads(self.cli('memory_runtime.py', 'index').stdout)['stores']['project']
        shown = sum(1 for line in store['index'].splitlines() if line.startswith('- ['))
        self.assertTrue(store['truncated'])
        self.assertEqual(shown + store['rows_not_shown'], len(self.index_rows()))
        self.assertIn('--full', store['full_list'])

    def test_user_notes_are_inlined_before_feedback(self):
        """#83"""
        snap = self.snapshot()
        inlined = re.findall(r'^NOTE .*?[/\\](user|feedback)[/\\]', snap, re.M)
        self.assertEqual(inlined[:2], ['user', 'user'])
        self.assertIn('QA_USER_IDENTITY_0', snap)
        self.assertIn('QA_USER_IDENTITY_1', snap)
        partial = re.search(r'PARTIAL SNAPSHOT: (\d+) always-apply note bodies omitted \(([^)]*)\)', snap)
        self.assertIsNotNone(partial)
        self.assertNotIn('user', partial.group(2))


class StaleRunTests(Base):
    def test_pending_does_not_show_a_dead_reservation_as_running(self):
        """#84"""
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': 'QA'})
            ledger.claim_job(self.root, 'dream', 1)
            state = ledger.dream_state(self.root)
            state['running']['started'] -= 16 * 3600
            ledger._save_state(self.root, state)
        result = json.loads(self.cli('memory_ledger.py', 'pending').stdout)
        self.assertIsNone(result['running'])
        self.assertIn('older than', result['stale_running']['reason'])
        self.assertGreaterEqual(result['stale_running']['age_seconds'], 16 * 3600)
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': 'QA 2'})
            state = ledger.dream_state(self.root)
            state['running'] = {'owner': 'x' * 32, 'kind': 'dream', 'started': time.time(), 'upto': 2, 'pid': None}
            ledger._save_state(self.root, state)
        live = json.loads(self.cli('memory_ledger.py', 'pending').stdout)
        self.assertEqual(live['running']['owner'], 'x' * 32)  # Still starting: shown as running.
        self.assertNotIn('stale_running', live)


class ChunkProgressTests(Base):
    def test_finished_chunks_are_kept_when_a_later_chunk_fails(self):
        """#73 follow-up: a timeout in chunk 2 must not send chunk 1 back."""
        with runtime.opened(self.config):
            for i in range(6):
                ledger.append(self.root, 'prompt', {'text': 'QA chunk record ' + str(i) + ' ' + 'x' * 300})
            owner = ledger.claim_job(self.root, 'dream', 6)
        calls = []

        def runner(command, text):
            calls.append(text)
            if len(calls) == 2:
                raise subprocess.TimeoutExpired(command, 1)
            return 0
        with mock.patch.object(dream, 'CHUNK_CAP', 700), mock.patch.dict(os.environ, {'JASON_DREAM_OWNER': owner}):
            code = dream.consolidate(self.config, self.project, 1, 6, ledger.settings(self.config), runner)
        self.assertEqual(code, 124)
        with runtime.opened(self.config):
            state = ledger.dream_state(self.root)
        first_chunk_last = max(int(seq) for seq in re.findall(r'\[ref local:(\d+):', calls[0]))
        self.assertEqual(state['cursor'], first_chunk_last)
        self.assertLess(state['cursor'], 6)
        self.assertEqual(state['running']['owner'], owner)  # Progress only; the job is still the owner's.

    def test_manual_prompt_and_rebuild_do_not_move_the_cursor(self):
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': 'QA'})
        with mock.patch.dict(os.environ, {'JASON_DREAM_OWNER': 'not-the-owner'}):
            dream.consolidate(self.config, self.project, 1, 1, ledger.settings(self.config), lambda c, t: 0)
        with runtime.opened(self.config):
            self.assertEqual(ledger.dream_state(self.root)['cursor'], 0)


class WorkerIdentityTests(Base):
    def test_leftover_or_wrong_worker_variables_unlock_nothing(self):
        """#81"""
        args = ('remember', '--subject', 'user', '--key', 'qa', '--value', 'QA_81', '--why', 'QA')
        for label, extra in (('bare', {'JASON_DREAM': '1'}),
                             ('wrong owner', {'JASON_DREAM': '1', 'JASON_DREAM_OWNER': 'f' * 32})):
            with self.subTest(case=label):
                result = self.cli('memory_runtime.py', *args, env=dict(self.env, **extra))
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('background memory mode', result.stderr)
        with as_worker(self.config, env=dict(self.env)) as worker:
            self.assertEqual(self.cli('memory_runtime.py', *args, env=worker).returncode, 0)

    def test_worker_hooks_still_block_direct_memory_edits(self):
        """#81: the worker skips recording and recall, not the edit guard."""
        target = str(self.root / 'feedback/qa-direct.md')
        with mock.patch.dict(os.environ, {'JASON_DREAM': '1'}):
            for tool in ('Write', 'Edit', 'MultiEdit'):
                with self.subTest(tool=tool):
                    out = hook.handle({'hook_event_name': 'PreToolUse', 'tool_name': tool, 'cwd': str(self.project),
                                       'session_id': 'w', 'tool_input': {'file_path': target}}, self.config)
                    self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')
            elsewhere = hook.handle({'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'cwd': str(self.project),
                                     'session_id': 'w', 'tool_input': {'file_path': str(self.project / 'x.txt')}},
                                    self.config)
            self.assertEqual(elsewhere, {})
            self.assertEqual(hook.handle({'hook_event_name': 'UserPromptSubmit', 'session_id': 'w', 'prompt': 'x',
                                          'cwd': str(self.project)}, self.config), {})
        with runtime.opened(self.config):
            self.assertEqual(ledger.records(self.root), [])  # The worker's own turns are not recorded.


class SettingsDisclosureTests(Base):
    def test_consolidation_switched_off_by_settings_is_disclosed(self):
        """#85"""
        self.assertNotIn('SETTINGS', self.snapshot())
        self.assertNotIn('settings', json.loads(self.cli('memory_ledger.py', 'pending').stdout))
        self.write_config(dict(self.ledger_cfg, dream_every=10000))
        self.assertIn('ledger.dream_every=10000', self.snapshot())
        notes = json.loads(self.cli('memory_ledger.py', 'pending').stdout)['settings']
        self.assertTrue(any('rarely or never run' in note for note in notes))
        self.write_config(dict(self.ledger_cfg, dream_agent=[sys.executable, '-c', 'pass']))
        self.assertIn('custom command', self.snapshot())


class ExternalEditTests(Base):
    def test_note_edited_outside_the_runtime_is_reported(self):
        """#86"""
        saved = self.remember('qa.style', 'QA_ORIGINAL_STYLE')
        with runtime.opened(self.config) as (_, _, roots):
            self.assertEqual(runtime.context(roots)['project']['audit']['structural_errors'], '')
        note = Path(saved['paths'][0])
        name = note.relative_to(self.root).as_posix()
        text = note.read_text(encoding='utf-8')
        note.write_text(re.sub(r'^description: .*$', 'description: "QA rewritten outside"', text, count=1, flags=re.M),
                        encoding='utf-8')
        with runtime.opened(self.config) as (_, _, roots):
            errors = runtime.context(roots)['project']['audit']['structural_errors']
        self.assertIn('index out of sync', errors)
        self.assertIn(name, errors)
        snap = self.snapshot()
        self.assertIn('STORE CHECK', snap)
        self.assertNotIn('CONFLICT:', snap)
        self.remember('qa.other', 'QA_OTHER')  # The next save rewrites the index from the notes.
        with runtime.opened(self.config) as (_, _, roots):
            self.assertNotIn('index out of sync', runtime.context(roots)['project']['audit']['structural_errors'])


if __name__ == '__main__':
    unittest.main()
