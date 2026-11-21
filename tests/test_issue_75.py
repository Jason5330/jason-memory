"""#75: a conversation never waits for a background save. The hook reads the store without the writer
lock (and checks it read one consistent state); recording uses the separate ledger lock."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import memory_runtime as runtime  # noqa: E402
import memory_ledger as ledger  # noqa: E402
from worker_identity import as_worker  # noqa: E402


class ForegroundNeverWaitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve()
        self.config = self.project / '.jason-memory.json'
        self.config.write_text(json.dumps({'version': 1, 'mode': 'project', 'memory_root': '.jason-memory',
                                           'ledger': {'enabled': True, 'foreground_writes': False,
                                                      'dream_every': 100000}}), encoding='utf-8')
        with runtime.opened(self.config) as (_, _, roots):
            self.root = roots['project']
        self.env = dict(os.environ, PYTHONUTF8='1')
        for key in ('JASON_DREAM', 'JASON_DREAM_OWNER'):
            self.env.pop(key, None)
        with as_worker(self.config, env={}), runtime.opened(self.config):
            for i in range(40):
                runtime.remember(self.root, 'qa', 'rule-' + str(i), 'QA_RULE_' + str(i), '使用者要求')

    def background(self, script):
        process = subprocess.Popen([sys.executable, '-c', textwrap.dedent(script), str(ROOT), str(self.config)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        return process

    def hook(self, name, **extra):
        event = dict({'hook_event_name': name, 'session_id': 'qa-' + name, 'cwd': str(self.project)}, **extra)
        start = time.monotonic()
        result = subprocess.run([sys.executable, str(ROOT / 'tools/claude_memory_hook.py'), '--config',
                                 str(self.config), '--jason-hook'], input=json.dumps(event), capture_output=True,
                                text=True, encoding='utf-8', env=self.env, timeout=120)
        return result, time.monotonic() - start

    def test_a_held_writer_lock_does_not_delay_recall_or_recording(self):
        holder = self.background('''
            import sys, time
            from pathlib import Path
            sys.path.insert(0, sys.argv[1] + '/tools')
            import memory_runtime as runtime
            cfg, project, lock_root, roots = runtime.configuration(Path(sys.argv[2]))
            with runtime.storelib.locked(lock_root):
                print('held', flush=True)
                time.sleep(14)
        ''')
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        before = len(ledger.records(self.root))
        result, seconds = self.hook('SessionStart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(seconds, 8, 'recall waited for the writer lock')
        self.assertIn(r'\nNOTE ', result.stdout)  # Notes are inlined (the output is JSON).
        self.assertNotIn('MEMORY UNAVAILABLE', result.stdout)
        result, seconds = self.hook('UserPromptSubmit', prompt='QA prompt during a save')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(seconds, 8, 'recording waited for the writer lock')
        self.assertEqual(len(ledger.records(self.root)), before + 1)
        self.assertIsNone(holder.poll(), 'the lock holder finished before the checks; the test proved nothing')

    def test_a_save_in_progress_is_not_mistaken_for_a_repair(self):
        """CI case: the journal of a running save plus a busy lock must not cost the turn its memory."""
        holder = self.background('''
            import sys, time
            from pathlib import Path
            sys.path.insert(0, sys.argv[1] + '/tools')
            import memory_runtime as runtime
            cfg, project, lock_root, roots = runtime.configuration(Path(sys.argv[2]))
            with runtime.storelib.locked(lock_root):
                (roots['project'] / '.transaction.json').write_text('{"in": "progress"}', encoding='utf-8')
                print('held', flush=True)
                time.sleep(14)
        ''')
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        result, seconds = self.hook('SessionStart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('MEMORY UNAVAILABLE', result.stdout)
        self.assertNotIn('STORE CHECK', result.stdout)
        self.assertIn(r'\nNOTE ', result.stdout)
        self.assertLess(seconds, 10)
        self.assertIsNone(holder.poll(), 'the lock holder finished before the check; the test proved nothing')

    def test_codex_turns_are_recorded_while_a_save_holds_the_lock(self):
        holder = self.background('''
            import sys, time
            from pathlib import Path
            sys.path.insert(0, sys.argv[1] + '/tools')
            import memory_runtime as runtime
            cfg, project, lock_root, roots = runtime.configuration(Path(sys.argv[2]))
            with runtime.storelib.locked(lock_root):
                print('held', flush=True)
                time.sleep(14)
        ''')
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        before = len(ledger.records(self.root))
        payload = json.dumps({'type': 'agent-turn-complete', 'turn-id': 't75', 'thread-id': 'th75',
                              'cwd': str(self.project), 'input-messages': ['QA codex prompt'],
                              'last-assistant-message': 'QA codex reply'})
        start = time.monotonic()
        result = subprocess.run([sys.executable, str(ROOT / 'tools/codex_notify.py'), payload], capture_output=True,
                                text=True, encoding='utf-8', env=self.env, cwd=str(self.project), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.strip(), '')
        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(len(ledger.records(self.root)), before + 2)
        self.assertIsNone(holder.poll(), 'the lock holder finished before the check; the test proved nothing')

    def test_snapshots_taken_during_saves_are_consistent(self):
        saver = self.background('''
            import os, sys, time
            from pathlib import Path
            sys.path.insert(0, sys.argv[1] + '/tools')
            sys.path.insert(0, sys.argv[1] + '/tests')
            import memory_runtime as runtime
            from worker_identity import as_worker
            config = Path(sys.argv[2])
            deadline = time.monotonic() + 20
            i = 0
            with as_worker(config, env={}):
                while time.monotonic() < deadline:
                    with runtime.opened(config) as (_, _, roots):
                        runtime.remember(roots['project'], 'qa', 'busy-' + str(i), 'QA_BUSY_' + str(i), 'x')
                    i += 1
            print(i, flush=True)
        ''')
        seen = 0
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result, _ = self.hook('SessionStart')
            self.assertEqual(result.returncode, 0, result.stderr)
            context = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
            # A torn read would show index rows and notes from different saves.
            self.assertNotIn('STORE CHECK', context)
            self.assertNotIn('MEMORY UNAVAILABLE', context)
            seen += 1
        saver.wait(timeout=60)
        self.assertGreater(int(saver.stdout.read().strip() or 0), 2, 'the saver never saved')
        self.assertGreater(seen, 2)

    def test_a_store_that_needs_repair_is_still_repaired(self):
        (self.root / 'MEMORY.md').unlink()
        result, _ = self.hook('SessionStart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'MEMORY.md').is_file())
        self.assertIn(r'\nNOTE ', result.stdout)  # Notes are inlined (the output is JSON).


class UnsettledReadTests(unittest.TestCase):
    """The fast-machine case CI hit: saves finish during every read and the lock is never free."""

    def test_recall_still_gets_committed_notes_and_no_false_structural_verdict(self):
        import itertools
        from unittest import mock
        import claude_memory_hook as hook
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            config = project / '.jason-memory.json'
            config.write_text(json.dumps({'version': 1, 'mode': 'project', 'memory_root': '.jason-memory'}),
                              encoding='utf-8')
            with runtime.opened(config) as (_, _, roots):
                runtime.remember(roots['project'], 'qa', 'rule', 'QA_RULE_UNSETTLED', 'x')
            real_context = runtime.context

            def torn_context(roots):
                snapshot = real_context(roots)
                for info in snapshot.values():  # What doctor reports when it sees a save half done.
                    info['audit']['structural_errors'] = 'ISSUE: unfinished save journal present: x'
                return snapshot
            changing = itertools.count()
            with mock.patch.object(hook, 'store_fingerprint', side_effect=lambda roots: next(changing)), \
                    mock.patch.object(runtime, 'context', side_effect=torn_context), \
                    mock.patch.object(runtime, 'opened', side_effect=ValueError('Memory busy; retry later.')), \
                    mock.patch.object(hook.time, 'sleep'):
                snapshot, _ = hook.consistent_snapshot(config, project, roots)
            info = snapshot['project']
            self.assertIn('QA_RULE_UNSETTLED', ''.join(info['notes'].values()))
            self.assertEqual(info['audit']['structural_errors'], '')

    def test_a_note_retired_mid_read_is_retried_not_fatal(self):
        import itertools
        from unittest import mock
        import claude_memory_hook as hook
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            config = project / '.jason-memory.json'
            config.write_text(json.dumps({'version': 1, 'mode': 'project', 'memory_root': '.jason-memory'}),
                              encoding='utf-8')
            with runtime.opened(config) as (_, _, roots):
                pass
            calls = itertools.count()
            real_context = runtime.context

            def flaky(roots):
                if next(calls) == 0:
                    raise FileNotFoundError('retired while listing')
                return real_context(roots)
            with mock.patch.object(runtime, 'context', side_effect=flaky), mock.patch.object(hook.time, 'sleep'):
                snapshot, _ = hook.consistent_snapshot(config, project, roots)
            self.assertIn('project', snapshot)


class LedgerLockTests(unittest.TestCase):
    def test_ledger_lock_is_reentrant_and_separate_from_the_store_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / 'store'
            root.mkdir()
            with runtime.storelib.locked(root):
                with ledger.ledger_lock(root), ledger.ledger_lock(root):
                    ledger.append(root, 'prompt', {'text': 'nested'})
            self.assertEqual(len(ledger.records(root)), 1)


if __name__ == '__main__':
    unittest.main()
