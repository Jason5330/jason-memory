"""Regression tests for issues #1-#10: raw ledger, mirror, dreaming, recall cost and hook attribution."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import memory_runtime as runtime  # noqa: E402
import memory_ledger as ledger  # noqa: E402
import memory_facts  # noqa: E402
import claude_memory_hook as hook  # noqa: E402

SECRET = 'TEST_ONLY_NOT_A_REAL_SECRET_7319'


class Base(unittest.TestCase):
    ledger_cfg = None

    def setUp(self):
        # Never start a real background worker (or a real agent CLI) from unit tests.
        self.spawned_workers, original_spawn = [], ledger._spawn
        ledger._spawn = self.spawned_workers.append
        self.addCleanup(setattr, ledger, '_spawn', original_spawn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve()
        self.config = self.project / '.jason-memory.json'
        cfg = {'version': 1, 'mode': 'project', 'memory_root': '.jason-memory'}
        if self.ledger_cfg:
            cfg['ledger'] = self.ledger_cfg
        self.config.write_text(json.dumps(cfg), encoding='utf-8')
        with runtime.opened(self.config) as (_, _, roots):
            self.root = roots['project']

    def event(self, name, session='A', **kwargs):
        return hook.handle({'hook_event_name': name, 'session_id': session, 'cwd': str(self.project), **kwargs},
                           self.config)

    def remember(self, key, value, session='A', kind=None):
        with runtime.opened(self.config):
            saved = runtime.remember(self.root, 'user', key, value, '使用者要求', kind=kind)
        self.event('PostToolUse', session=session, tool_name='Bash', tool_response={'stdout': json.dumps(saved)})
        return saved


class CredentialTests(Base):
    """Issue #1: credential values are never written to notes or the index."""

    def test_remember_rejects_token_value_and_leaves_store_clean(self):
        with runtime.opened(self.config):
            with self.assertRaisesRegex(ValueError, 'NOT SAVED'):
                runtime.remember(self.root, 'user', 'api.token', '測試用 API token：' + SECRET, '使用者要求')
        on_disk = ''.join(p.read_text(encoding='utf-8') for p in self.root.rglob('*') if p.is_file())
        self.assertNotIn(SECRET, on_disk)

    def test_env_var_name_can_be_remembered(self):
        saved = self.remember('api.token.location', 'API token 放在環境變數 OPENAI_API_KEY')
        self.assertTrue(saved['changed'])

    def test_apply_rejects_secret_in_note_body(self):
        body = ('---\nname: creds\ndescription: creds\ntype: reference\ncreated: 2026-11-12\n'
                'updated: 2026-11-12\n---\n\npassword=hunter2abc9\n')
        with runtime.opened(self.config):
            with self.assertRaisesRegex(ValueError, 'NOT SAVED'):
                runtime.apply(self.root, {'expected_revision': runtime.revision(self.root),
                                          'updates': {'creds.md': body}, 'retire': []})

    def test_detector_ignores_ordinary_numbers(self):
        for text in ['token 數量上限 4096 tokens', '輸出的時間全部使用 24 小時制', 'GitHub token 存放在 1Password']:
            self.assertEqual(memory_facts.secret_values(text), [], text)


class TypeTests(Base):
    """Issue #6: remember files identity and project constraints under the right type."""

    def test_type_controls_folder_and_frontmatter(self):
        for kind, key in [('user', 'role'), ('project', 'offline.only')]:
            saved = self.remember(key, '值 ' + key, kind=kind)
            path = Path(saved['paths'][0])
            self.assertEqual(path.parent.name, kind)
            self.assertIn('\ntype: ' + kind + '\n', path.read_text(encoding='utf-8'))
        self.assertIn('## project\n\n- [project-', runtime.read_index(self.root))

    def test_wrong_type_is_moved_not_duplicated(self):
        first = self.remember('role', '物流主管')
        self.assertIn('/feedback/', Path(first['paths'][0]).as_posix())
        moved = self.remember('role', '物流主管', kind='user')
        notes = runtime.notes(self.root)
        self.assertEqual([n for n in notes if 'role' in notes[n] or '物流' in notes[n]],
                         [n for n in notes if n.startswith('user/')])
        self.assertEqual(len(notes), 1)
        self.assertTrue(moved['changed'])


class RecallCostTests(Base):
    """Issues #5/#8 and #10 V6: small index first, selected notes on demand."""

    def setUp(self):
        super().setUp()
        for i in range(30):
            self.remember('rule.' + str(i), '規則 ' + str(i) + ' ' + '內容' * 150, kind='project' if i % 2 else 'feedback')

    def test_index_is_bounded_and_read_selects_notes(self):
        with runtime.opened(self.config) as (_, _, roots):
            small = runtime.bounded_index(roots, 2000)['project']
            self.assertLessEqual(small['bytes'], 2000)
            self.assertTrue(small['truncated'])
            name = sorted(runtime.notes(self.root))[0]
            picked = runtime.read_notes(self.root, [name])
        self.assertEqual(list(picked['contents']), [name])

    def test_partial_snapshot_points_to_selective_read_not_full_context(self):
        text = self.event('SessionStart')['hookSpecificOutput']['additionalContext']
        self.assertLessEqual(len(text), hook.LIMIT)
        self.assertIn('PARTIAL SNAPSHOT', text)
        self.assertIn('read --scope', text)
        self.assertIn('INDEX project', text)
        # Project-type notes are listed, never inlined.
        self.assertNotIn('type: project', text)

    def test_unchanged_turn_uses_fast_path(self):
        self.event('SessionStart')
        calls = []
        original = runtime.context
        runtime.context = lambda roots: calls.append(1) or original(roots)
        try:
            self.assertEqual(self.event('UserPromptSubmit', prompt='2+2=?'), {})
            self.assertEqual(self.event('Stop', last_assistant_message='4'), {})
        finally:
            runtime.context = original
        self.assertEqual(calls, [])


class HookAttributionTests(Base):
    """Issues #2, #3, #4."""

    def notice(self, path):
        return '已將此要求記憶在 [筆記](' + str(path) + ')，健檢通過。'

    def test_other_session_write_does_not_block_plain_answer(self):
        for session in 'AB':
            self.event('SessionStart', session=session)
            self.event('UserPromptSubmit', session=session)
        saved = self.remember('clock', '24 小時制', session='B')
        self.assertEqual(self.event('Stop', session='B', last_assistant_message=self.notice(saved['paths'][0])), {})
        self.assertEqual(self.event('Stop', session='A', last_assistant_message='4'), {})

    def test_same_session_missing_notice_still_blocked(self):
        self.event('SessionStart', session='B')
        self.event('SessionStart')
        self.remember('clock', '24 小時制')
        self.assertEqual(self.event('Stop', last_assistant_message='4')['decision'], 'block')

    def test_compact_keeps_receipts_and_pending_notice(self):
        self.event('SessionStart', session='D')
        saved = self.remember('clock', '24 小時制', session='D')
        self.event('SessionStart', session='D', source='compact')
        self.assertEqual(self.event('Stop', session='D', last_assistant_message='Done.')['decision'], 'block')
        self.assertEqual(self.event('Stop', session='D', last_assistant_message=self.notice(saved['paths'][0])), {})

    def test_compact_receipt_not_reused_after_next_prompt(self):
        self.event('SessionStart', session='D')
        saved = self.remember('clock', '24 小時制', session='D')
        self.event('SessionStart', session='D', source='compact')
        self.event('UserPromptSubmit', session='D')
        self.assertEqual(self.event('Stop', session='D', last_assistant_message=self.notice(saved['paths'][0]))['decision'],
                         'block')

    def test_document_and_program_memory_work_are_not_memory_claims(self):
        for phrase in ['需求文件已更新，請查看 PRD.md。', '已將需求保存到 PRD.md。', '已保存你的筆記整理檔案。',
                       'I updated the memory allocator to fix the leak.', 'The memory leak was removed.']:
            with self.subTest(phrase=phrase):
                self.assertFalse(hook.success_claim(phrase))
        for phrase in ['偏好已保存到記憶系統。', 'I have saved your preference.', 'Memory saved.', '已記住。']:
            with self.subTest(phrase=phrase):
                self.assertTrue(hook.success_claim(phrase))


class LedgerTests(Base):
    """Issue #10 V1, V2, V4."""

    ledger_cfg = {'enabled': True, 'dream_every': 3, 'dream_command': [sys.executable, '-c', 'pass']}

    def test_ledger_is_append_only_replayable_and_tamper_evident(self):
        self.event('SessionStart')
        self.event('UserPromptSubmit', prompt='只答 45')
        self.event('PostToolUse', tool_name='Bash', tool_input={'command': 'echo 45'}, tool_response={'stdout': '45'})
        self.event('Stop', last_assistant_message='45')
        with runtime.opened(self.config):
            first = [r['hash'] for r in ledger.replay(self.root, 'A')]
            self.assertEqual([r['kind'] for r in ledger.replay(self.root, 'A')], ['prompt', 'tool', 'assistant'])
            ledger.append(self.root, 'correction', {'text': '更正'}, 'A')
            self.assertEqual([r['hash'] for r in ledger.replay(self.root, 'A')][:3], first)
        path = self.root / '.raw/ledger.jsonl'
        lines = path.read_text(encoding='utf-8').splitlines()
        lines[0] = lines[0].replace('只答 45', '只答 46')
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'chain broken'):
            ledger.verify(self.root)

    def test_ledger_redacts_secrets(self):
        self.event('UserPromptSubmit', prompt='API token 記住：' + SECRET)
        self.assertNotIn(SECRET, (self.root / '.raw/ledger.jsonl').read_text(encoding='utf-8'))

    def test_mirror_is_byte_exact(self):
        versions = [b'\xef\xbb\xbf\xe4\xb8\xad\xe6\x96\x87\r\nline\r\n', b'\x00\x01\xff binary \r\n v2']
        doc = self.project / 'doc.bin'
        with runtime.opened(self.config):
            for content in versions:
                doc.write_bytes(content)
                result = ledger.mirror(self.root, doc)
                self.assertEqual(Path(result['blob']).read_bytes(), content)
        self.assertEqual(len(list((self.root / '.raw/blobs').iterdir())), 2)

    @staticmethod
    def owner(command):
        return command[command.index('--owner') + 1]

    def test_dream_triggers_on_count_retries_and_never_reprocesses(self):
        spawned = []
        opts = ledger.settings(self.config)
        with runtime.opened(self.config):
            for i in range(2):
                ledger.append(self.root, 'prompt', {'text': str(i)})
            self.assertIsNone(ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append))
            ledger.append(self.root, 'prompt', {'text': '2'})
            self.assertEqual(ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append), 3)
            # A running worker is not started twice.
            self.assertIsNone(ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append))
        self.assertEqual(len(spawned), 1)
        failing = dict(opts, dream_command=[sys.executable, '-c', 'raise SystemExit(3)'])
        self.assertEqual(ledger.dream_run(self.config, self.project, 3, failing, self.owner(spawned[-1])), 3)
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root)['cursor'], 0)
            self.assertEqual(ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append), 3)
        self.assertEqual(ledger.dream_run(self.config, self.project, 3, opts, self.owner(spawned[-1])), 0)
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root), dict(ledger.pending(self.root), cursor=3, pending=0))
            # No new records: nothing runs again.
            self.assertIsNone(ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append))
        self.assertEqual(len(spawned), 2)

    # --- Issues #12-#16 -------------------------------------------------------

    def test_threshold_reached_on_tool_or_stop_starts_worker(self):
        """#12: the last appended record of a turn can trigger consolidation."""
        self.config.write_text(json.dumps({'version': 1, 'mode': 'project', 'memory_root': '.jason-memory',
                                           'ledger': {'enabled': True, 'dream_every': 2,
                                                      'dream_command': [sys.executable, '-c', 'pass']}}),
                               encoding='utf-8')
        spawned, original = [], ledger._spawn
        ledger._spawn = spawned.append
        try:
            self.event('SessionStart')
            self.event('UserPromptSubmit', prompt='hello')
            self.assertEqual(spawned, [])
            self.event('PostToolUse', tool_name='Bash', tool_input={'command': 'echo hi'},
                       tool_response={'stdout': 'hi'})
            self.assertEqual(len(spawned), 1)
            with runtime.opened(self.config):
                ledger.release_job(self.root, self.owner(spawned[0]), 2)
            self.event('UserPromptSubmit', prompt='again')
            self.event('Stop', last_assistant_message='done')
            self.assertEqual(len(spawned), 2)
        finally:
            ledger._spawn = original

    def test_missing_executable_records_error_and_allows_retry(self):
        """#13: a command that cannot start clears its job and records last_error."""
        opts = dict(ledger.settings(self.config), dream_every=1,
                    dream_command=[str(self.project / 'missing-synthetic-executable')])
        spawned = []
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': 'x'})
            ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append)
        self.assertEqual(ledger.dream_run(self.config, self.project, 1, opts, self.owner(spawned[0])), 127)
        with runtime.opened(self.config):
            state = ledger.pending(self.root)
            self.assertEqual((state['cursor'], state['running']), (0, None))
            self.assertEqual(state['last_error']['code'], 127)
            self.assertEqual(ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append), 1)

    def test_spawn_failure_clears_reservation(self):
        """#13: the outer spawn failing must not leave a running marker."""
        opts = dict(ledger.settings(self.config), dream_every=1)

        def broken(command):
            raise PermissionError('denied')
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': 'x'})
            self.assertIsNone(ledger.maybe_dream(self.root, self.config, self.project, opts, broken))
            state = ledger.pending(self.root)
        self.assertIsNone(state['running'])
        self.assertEqual(state['last_error']['code'], 126)

    def test_dead_worker_does_not_block_for_an_hour(self):
        """#13: liveness, not only age, decides whether a job still runs."""
        import subprocess
        import time
        finished = subprocess.Popen([sys.executable, '-c', 'pass'])
        finished.wait()
        opts = dict(ledger.settings(self.config), dream_every=1)
        spawned = []
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': 'x'})
            state = ledger.dream_state(self.root)
            state['running'] = {'owner': 'gone', 'kind': 'dream', 'started': time.time(),
                                'upto': 1, 'pid': finished.pid}
            ledger._save_state(self.root, state)
            self.assertEqual(ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append), 1)

    def test_worker_never_clears_another_owners_job(self):
        """#15: a stale/replaced reservation cannot finish someone else's job."""
        opts = dict(ledger.settings(self.config), dream_every=1)
        spawned = []
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': 'x'})
            ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append)
        self.assertEqual(ledger.dream_run(self.config, self.project, 1, opts, 'not-the-owner'), 75)
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root)['running']['owner'], self.owner(spawned[0]))

    def test_corrupted_ledger_is_never_sent_to_the_model(self):
        """#16: edited, deleted and reordered records stop consolidation before the agent runs."""
        import memory_dream as dream
        path = self.root / '.raw/ledger.jsonl'
        with runtime.opened(self.config):
            for text in ('Please use short answers', 'second', 'third'):
                ledger.append(self.root, 'prompt', {'text': text})
        original = path.read_text(encoding='utf-8').splitlines()
        tampered = {
            'edit': [original[0].replace('short', 'long')] + original[1:],
            'delete': [original[0], original[2]],
            'reorder': [original[1], original[0], original[2]],
        }
        command = dict(ledger.settings(self.config), dream_command=[sys.executable, '-c', 'pass'])
        for name, lines in tampered.items():
            with self.subTest(name=name):
                path.write_text(chr(10).join(lines) + chr(10), encoding='utf-8')
                captured = []
                code = dream.consolidate(self.config, self.project, 1, 3, ledger.settings(self.config),
                                         lambda cmd, text: captured.append(text) or 0)
                self.assertEqual((code, captured), (65, []))
                self.assertEqual(ledger.dream_run(self.config, self.project, 3, command), 65)
                with runtime.opened(self.config):
                    self.assertEqual(ledger.pending(self.root)['cursor'], 0)

    def test_truncation_below_cursor_is_detected(self):
        """#16 follow-up: the checkpoint taken when the cursor advanced detects tail truncation."""
        with runtime.opened(self.config):
            for text in ('a', 'b', 'c'):
                ledger.append(self.root, 'prompt', {'text': text})
            owner = ledger.claim_job(self.root, 'dream', 3)
            ledger.release_job(self.root, owner, 3)
        path = self.root / '.raw/ledger.jsonl'
        path.write_text(chr(10).join(path.read_text(encoding='utf-8').splitlines()[:2]) + chr(10), encoding='utf-8')
        with runtime.opened(self.config):
            with self.assertRaisesRegex(ValueError, 'truncated'):
                ledger.verified_records(self.root)

    def test_records_arriving_during_a_run_start_the_next_run(self):
        """#12 follow-up: the tail batch does not wait for another external event."""
        opts = dict(ledger.settings(self.config), dream_every=2, dream_command=[sys.executable, '-c', 'pass'])
        spawned = self.spawned_workers
        with runtime.opened(self.config):
            for text in ('a', 'b'):
                ledger.append(self.root, 'prompt', {'text': text})
            self.assertEqual(ledger.maybe_dream(self.root, self.config, self.project, opts), 2)
            for text in ('c', 'd'):
                ledger.append(self.root, 'prompt', {'text': text})
        self.assertEqual(ledger.dream_run(self.config, self.project, 2, opts, self.owner(spawned[0])), 0)
        self.assertEqual(len(spawned), 2)
        self.assertIn('4', spawned[1])
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root)['cursor'], 2)

    def test_stale_owner_cannot_commit_cursor(self):
        """#15 follow-up: a replaced worker changes neither cursor, error nor the new marker."""
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': 'x'})
            stale = ledger.claim_job(self.root, 'dream', 1)
            state = ledger.dream_state(self.root)
            state['running']['owner'] = 'replacement'
            ledger._save_state(self.root, state)
            self.assertFalse(ledger.release_job(self.root, stale, 1))
            self.assertFalse(ledger.release_job(self.root, stale, 1, {'code': 3, 'error': 'x'}))
            after = ledger.pending(self.root)
        self.assertEqual((after['cursor'], after['last_error'], after['running']['owner']), (0, None, 'replacement'))

    def test_disabled_ledger_writes_nothing(self):
        self.config.write_text('{"version":1,"mode":"project","memory_root":".jason-memory"}', encoding='utf-8')
        self.event('UserPromptSubmit', prompt='hi')
        self.assertFalse((self.root / '.raw/ledger.jsonl').exists())


class BackgroundModeTests(Base):
    """Issue #11 V3, V7, V8, V9: foreground never writes memory; background worker does."""

    ledger_cfg = {'enabled': True, 'foreground_writes': False, 'dream_every': 3}

    def cli(self, *args, env=None):
        import os
        import subprocess
        environment = dict(os.environ, PYTHONUTF8='1')
        environment.pop('JASON_DREAM', None)
        environment.update(env or {})
        return subprocess.run([sys.executable, str(ROOT / 'tools/memory_runtime.py'), '--config', str(self.config),
                               *args], capture_output=True, text=True, encoding='utf-8', env=environment)

    def test_foreground_runtime_write_is_refused_worker_write_allowed(self):
        args = ['remember', '--subject', 'user', '--key', 'clock', '--value', '24 小時制', '--why', 'seq 1']
        refused = self.cli(*args)
        self.assertEqual(refused.returncode, 1)
        self.assertIn('background memory mode', refused.stderr)
        self.assertEqual(runtime.notes(self.root), {})
        allowed = self.cli(*args, env={'JASON_DREAM': '1'})
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(len(runtime.notes(self.root)), 1)

    def test_snapshot_tells_foreground_not_to_write(self):
        text = self.event('SessionStart')['hookSpecificOutput']['additionalContext']
        self.assertIn('BACKGROUND MEMORY MODE', text)
        self.assertNotIn('Append: remember', text)
        self.assertIn('search --text', text)

    def test_stop_needs_no_notice_but_blocks_false_save_claim(self):
        self.event('SessionStart')
        self.event('UserPromptSubmit', prompt='以後時間都用 24 小時制')
        import os
        os.environ['JASON_DREAM'] = '1'
        try:
            with runtime.opened(self.config):
                runtime.remember(self.root, 'user', 'clock', '24 小時制', '來源：ledger seq 1')
            self.assertEqual(self.event('Stop', last_assistant_message='ignored'), {})  # worker hooks are inert
        finally:
            del os.environ['JASON_DREAM']
        self.assertEqual(self.event('Stop', last_assistant_message='好的，之後都用 24 小時制。'), {})
        blocked = self.event('Stop', last_assistant_message='已記住你的偏好。')
        self.assertEqual(blocked['decision'], 'block')
        self.assertIn('Background memory mode', blocked['reason'])

    def test_worker_prompt_contains_records_and_sources(self):
        import memory_dream as dream
        self.event('UserPromptSubmit', prompt='報表標題都用淡紫色')
        self.event('Stop', last_assistant_message='好的。')
        seen = []
        code = dream.consolidate(self.config, self.project, 1, 2, dict(ledger.settings(self.config), dream_agent=['x']),
                                 lambda cmd, text: seen.append((cmd, text)) or 0)
        self.assertEqual(code, 0)
        self.assertIn('[seq 1', seen[0][1])
        self.assertIn('報表標題都用淡紫色', seen[0][1])
        self.assertIn('來源：ledger seq', seen[0][1])

    def test_missing_agent_keeps_cursor_for_retry(self):
        import memory_dream as dream
        self.event('UserPromptSubmit', prompt='x')
        self.assertEqual(dream.consolidate(self.config, self.project, 1, 1,
                                           dict(ledger.settings(self.config), dream_agent='no-such-agent')), 127)

    def test_rebuild_archives_derived_notes_and_replays_ledger(self):
        import os
        import memory_dream as dream
        self.event('UserPromptSubmit', prompt='標題用淺藍色')
        self.event('UserPromptSubmit', prompt='更正：標題改用淡紫色')
        os.environ['JASON_DREAM'] = '1'
        try:
            with runtime.opened(self.config):
                runtime.remember(self.root, 'user', 'header.color', '淺藍色', '來源：ledger seq 1')
        finally:
            del os.environ['JASON_DREAM']
        before = (self.root / '.raw/ledger.jsonl').read_bytes()

        def worker(cmd, text):
            # The agent writes to the staging store named in its prompt, never the live one.
            import re
            self.assertIn('更正：標題改用淡紫色', text)
            staging = Path(re.search(r"--config '?([^'\s]+\.jason-memory\.json)", text).group(1))
            self.assertNotEqual(staging.resolve(), self.config.resolve())
            with runtime.opened(staging) as (_, _, roots):
                runtime.remember(roots['project'], 'user', 'header.color', '淡紫色', '來源：ledger seq 2')
            return 0
        result = dream.rebuild(self.config, self.project, ledger.settings(self.config), worker)
        os.environ.pop('JASON_DREAM', None)
        self.assertEqual(result['exit_code'], 0)
        current = ''.join(runtime.notes(self.root).values())
        self.assertIn('淡紫色', current)
        self.assertNotIn('淺藍色', current)
        self.assertIn('ledger seq 2', current)
        self.assertEqual((self.root / '.raw/ledger.jsonl').read_bytes(), before)
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root)['cursor'], 2)

    def test_rebuild_failure_keeps_current_notes_and_exits_nonzero(self):
        """#14: archive/swap happens only after a successful rebuild; failure is reported."""
        import os
        import subprocess
        import memory_dream as dream
        self.event('UserPromptSubmit', prompt='標題用淺藍色')
        os.environ['JASON_DREAM'] = '1'
        try:
            with runtime.opened(self.config):
                runtime.remember(self.root, 'user', 'header.color', '淺藍色', '來源：ledger seq 1')
        finally:
            del os.environ['JASON_DREAM']
        before = runtime.notes(self.root)
        cfg = json.loads(self.config.read_text(encoding='utf-8'))
        cfg['ledger']['dream_agent'] = [sys.executable, '-c',
                                        'import sys; print("JASON_DREAM_STATUS: failed"); sys.exit(7)']
        self.config.write_text(json.dumps(cfg), encoding='utf-8')
        cli = subprocess.run([sys.executable, str(ROOT / 'tools/memory_dream.py'), '--config', str(self.config),
                              '--project', str(self.project), 'rebuild', '--yes'],
                             capture_output=True, text=True, encoding='utf-8', env=dict(os.environ, PYTHONUTF8='1'))
        self.assertEqual(cli.returncode, 7, cli.stdout + cli.stderr)
        self.assertIn('current notes kept', cli.stdout)
        self.assertEqual(runtime.notes(self.root), before)
        with runtime.opened(self.config):
            state = ledger.pending(self.root)
        self.assertIsNone(state['running'])
        self.assertEqual(state['last_error']['code'], 7)
        self.assertEqual(list((self.root / '.raw').glob('rebuild-*')), [])
        for agent, code in [('no-such-agent', 127), ([sys.executable, '-c', 'print("done")'], 75)]:
            with self.subTest(agent=agent):
                result = dream.rebuild(self.config, self.project, dict(ledger.settings(self.config), dream_agent=agent))
                self.assertEqual(result['exit_code'], code)
                self.assertEqual(runtime.notes(self.root), before)

    def test_rebuild_and_dream_worker_are_mutually_exclusive(self):
        """#15: rebuild holds the consolidation slot; a running worker blocks rebuild."""
        import memory_dream as dream
        self.event('UserPromptSubmit', prompt='x')
        opts = dict(ledger.settings(self.config), dream_every=1)
        spawned = []

        def probe(cmd, text):
            with runtime.opened(self.config):
                self.assertEqual(ledger.pending(self.root)['running']['kind'], 'rebuild')
                self.assertIsNone(ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append))
            return 0
        self.assertEqual(dream.rebuild(self.config, self.project, opts, probe)['exit_code'], 0)
        self.assertEqual(spawned, [])
        with runtime.opened(self.config):
            self.assertIsNone(ledger.pending(self.root)['running'])
            ledger.append(self.root, 'prompt', {'text': 'y'})
            ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append)
        blocked = dream.rebuild(self.config, self.project, opts, lambda cmd, text: 0)
        self.assertEqual(blocked['exit_code'], 75)
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root)['running']['kind'], 'dream')

    def test_rebuild_does_not_undo_notes_written_meanwhile(self):
        """#15 follow-up: if the live store changes during rebuild, the swap is refused."""
        import os
        import memory_dream as dream
        self.event('UserPromptSubmit', prompt='x')

        def concurrent_write(cmd, text):
            os.environ['JASON_DREAM'] = '1'
            with runtime.opened(self.config):
                runtime.remember(self.root, 'user', 'written.meanwhile', '保留這則', '來源：前台')
            return 0
        result = dream.rebuild(self.config, self.project, ledger.settings(self.config), concurrent_write)
        self.assertEqual(result['exit_code'], 75)
        self.assertIn('changed during rebuild', result['error'])
        self.assertIn('保留這則', ''.join(runtime.notes(self.root).values()))
        with runtime.opened(self.config):
            self.assertIsNone(ledger.pending(self.root)['running'])

    def test_rebuild_with_lost_reservation_does_not_swap(self):
        """#15 follow-up 2: an expired, replaced rebuild never swaps or reports success."""
        import memory_dream as dream
        self.event('UserPromptSubmit', prompt='x')
        before = runtime.notes(self.root)

        def replaced(cmd, text):
            import re
            staging = Path(re.search(r"--config '?([^'\s]+\.jason-memory\.json)", text).group(1))
            with runtime.opened(staging) as (_, _, roots):
                runtime.remember(roots['project'], 'user', 'rebuilt', '新值', '來源：ledger seq 1')
            with runtime.opened(self.config):
                state = ledger.dream_state(self.root)
                state['running']['owner'] = 'replacement'
                ledger._save_state(self.root, state)
            return 0
        result = dream.rebuild(self.config, self.project, ledger.settings(self.config), replaced)
        self.assertEqual(result['exit_code'], 75)
        self.assertFalse(result['rebuilt'])
        self.assertIn('reservation lost', result['error'])
        self.assertEqual(runtime.notes(self.root), before)
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root)['running']['owner'], 'replacement')

    def test_exact_lookup_in_raw_ledger(self):
        self.event('UserPromptSubmit', prompt='錯誤碼是 E-4096-XY，報價 12,345.67 元')
        with runtime.opened(self.config):
            hits = ledger.search(self.root, 'E-4096-XY')
        self.assertEqual(hits[0]['data']['text'], '錯誤碼是 E-4096-XY，報價 12,345.67 元')

    def test_exact_lookup_rejects_tampered_records(self):
        self.event('UserPromptSubmit', prompt='報價 12,345.6700 元')
        path = self.root / '.raw/ledger.jsonl'
        path.write_bytes(path.read_bytes().replace(b'12,345.6700', b'99,345.6700'))
        with runtime.opened(self.config):
            with self.assertRaisesRegex(ValueError, 'Ledger chain broken'):
                ledger.search(self.root, '99,345.6700')

    def test_worker_prompt_preserves_tool_evidence_within_record_cap(self):
        import memory_dream as dream
        self.event('PostToolUse', tool_name='Bash', tool_input={'command': 'x' * 9000},
                   tool_response={'stdout': 'QA-EVIDENCE-7B9A amount 12,345.6700', 'exit_code': 0})
        prompts = []
        code = dream.consolidate(self.config, self.project, 1, 1, ledger.settings(self.config),
                                 lambda cmd, text: prompts.append(text) or 0)
        self.assertEqual(code, 0)
        self.assertIn('QA-EVIDENCE-7B9A amount 12,345.6700', prompts[0])
        line = next(line for line in prompts[0].splitlines() if line.startswith('[seq 1 '))
        self.assertLessEqual(len(line), dream.RECORD_CAP)

    def test_independent_reader_verifies_ledger_format(self):
        """Spec in docs/ledger-format.md, reimplemented with json + hashlib only."""
        import hashlib
        self.event('UserPromptSubmit', prompt='一')
        self.event('Stop', last_assistant_message='二')
        previous = '0' * 64
        for number, line in enumerate((self.root / '.raw/ledger.jsonl').read_text(encoding='utf-8').splitlines(), 1):
            record = json.loads(line)
            body = {k: v for k, v in record.items() if k != 'hash'}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
            self.assertEqual((record['seq'], record['prev'], record['hash']), (number, previous, digest))
            previous = record['hash']

    def test_agent_must_report_ok_status_or_run_counts_as_failed(self):
        import os
        import memory_dream as dream
        env, cases = dict(os.environ), [("print('saved')", 75), ("print('x'); print('JASON_DREAM_STATUS: failed')", 75),
                                       ("print('JASON_DREAM_STATUS: ok')", 0), ("raise SystemExit(4)", 4)]
        for code, expected in cases:
            with self.subTest(code=code):
                self.assertEqual(dream._run_agent([sys.executable, '-c', code], '', env, self.project), expected)

    def test_codex_notify_records_turn(self):
        import codex_notify
        codex_notify.handle({'type': 'agent-turn-complete', 'thread-id': 't1', 'cwd': str(self.project),
                             'input-messages': ['只答 45'], 'last-assistant-message': '45'})
        with runtime.opened(self.config):
            kinds = [(r['kind'], r['data']['text']) for r in ledger.replay(self.root, 't1')]
        self.assertEqual(kinds, [('prompt', '只答 45'), ('assistant', '45')])


if __name__ == '__main__':
    unittest.main()
