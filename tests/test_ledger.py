"""Regression tests for issues #1-#10: raw ledger, mirror, dreaming, recall cost and hook attribution."""
import json
from pathlib import Path
import subprocess
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

    def test_plaintext_passwords_mask_short_numbers_but_keep_environment_references(self):
        text = '\n'.join(['password="0427"', 'password=581', '--password 7391', 'password=2',
                          'retry_count=3', 'password="$QA_VAR"', 'password=$env:QA_VAR',
                          'password="%literal"'])
        expected = '\n'.join(['password="[REDACTED]"', 'password=[REDACTED]', '--password [REDACTED]',
                              'password=[REDACTED]', 'retry_count=3', 'password="$QA_VAR"',
                              'password=$env:QA_VAR', 'password="[REDACTED]"'])
        self.assertEqual(memory_facts.redact(text), expected)

    def test_numeric_credentials_are_refused_in_notes_but_metrics_can_be_saved(self):
        saved = self.remember('retry_count', 'retry_count=3')
        name = Path(saved['paths'][0]).relative_to(self.root).as_posix()
        with runtime.opened(self.config):
            before = runtime.notes(self.root)
            with self.assertRaisesRegex(ValueError, 'NOT SAVED'):
                runtime.remember(self.root, 'qa', 'credential', 'password="0427"', 'Synthetic example')
            plan = {'expected_revision': runtime.revision(self.root), 'retire': [],
                    'updates': {name: before[name] + '\n\n--password 7391\n'}}
            with self.assertRaisesRegex(ValueError, 'NOT SAVED'):
                runtime.apply(self.root, plan)
            self.assertEqual(runtime.notes(self.root), before)


class TypeTests(Base):
    """Note types and safe paths used by batch writes."""

    def test_apply_rejects_note_alias_before_retiring_existing_note(self):
        saved = self.remember('format', 'QA old value')
        name = Path(saved['paths'][0]).relative_to(self.root).as_posix()
        with runtime.opened(self.config):
            before = runtime.notes(self.root)
            index = runtime.read_index(self.root)
            for alias in (name.replace('/', '/./'), name.replace('/', '//')):
                with self.subTest(alias=alias):
                    plan = {'expected_revision': runtime.revision(self.root), 'retire': [name],
                            'updates': {alias: before[name].replace('QA old value', 'QA new value')}}
                    with self.assertRaisesRegex(ValueError, 'canonical relative note path'):
                        runtime.apply(self.root, plan)
                    self.assertEqual(runtime.notes(self.root), before)
                    self.assertEqual(runtime.read_index(self.root), index)
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

    def test_rejects_new_bad_event_and_still_consolidates_legacy_data_shape(self):
        import memory_dream as dream
        with runtime.opened(self.config):
            legacy = ledger.append(self.root, 'prompt', {'text': 'initial shape'})
            path = ledger.raw_root(self.root) / ledger.LEDGER
            old = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'must be a JSON object'):
                ledger.append(self.root, 'prompt', ['new bad data'])
            self.assertEqual(path.read_bytes(), old)
            legacy['data'] = ['legacy payload']
            legacy['hash'] = ledger._hash(legacy)
            path.write_bytes((json.dumps(legacy) + '\n').encode('utf-8'))
            ledger.append(self.root, 'prompt', {'text': 'next valid event'})
        prompts = []
        self.assertEqual(dream.consolidate(self.config, self.project, 1, 2, ledger.settings(self.config),
                                          lambda cmd, text: prompts.append(text) or 0), 0)
        self.assertTrue(any('legacy payload' in prompt and 'next valid event' in prompt for prompt in prompts))

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
        leftover = self.cli(*args, env={'JASON_DREAM': '1'})  # #81: a stray variable unlocks nothing.
        self.assertEqual(leftover.returncode, 1)
        self.assertIn('background memory mode', leftover.stderr)
        self.assertEqual(runtime.notes(self.root), {})
        from worker_identity import as_worker
        with as_worker(self.config, env={}) as worker:
            allowed = self.cli(*args, env=worker)
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
        self.assertIn('[ref local:1:', seen[0][1])
        self.assertIn('報表標題都用淡紫色', seen[0][1])
        self.assertIn('來源：ledger 3d9d6dc09bcfaf35:12:ab12cd34ef56', seen[0][1])

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

    def test_successful_rebuild_hands_queued_records_to_the_next_run(self):
        """#31: records that reached the threshold during a rebuild start a run right after it."""
        import memory_dream as dream
        self.event('UserPromptSubmit', prompt='x')
        opts = dict(ledger.settings(self.config), dream_every=2)

        def busy(cmd, text):
            with runtime.opened(self.config):
                for value in ('a', 'b'):
                    ledger.append(self.root, 'prompt', {'text': value})
                # The rebuild still holds the slot, so this check cannot start a worker yet.
                self.assertIsNone(ledger.maybe_dream(self.root, self.config, self.project, opts))
            return 0
        self.assertEqual(dream.rebuild(self.config, self.project, opts, busy)['exit_code'], 0)
        self.assertEqual(len(self.spawned_workers), 1)
        with runtime.opened(self.config):
            state = ledger.pending(self.root)
        self.assertEqual((state['cursor'], state['head'], state['running']['upto']), (1, 3, 3))

    def test_exact_lookup_in_raw_ledger(self):
        self.event('UserPromptSubmit', prompt='錯誤碼是 E-4096-XY，報價 12,345.67 元')
        with runtime.opened(self.config):
            hits = ledger.search(self.root, 'E-4096-XY')
        self.assertEqual(hits[0]['data']['text'], '錯誤碼是 E-4096-XY，報價 12,345.67 元')

    def test_search_matches_original_nested_text_and_not_json_escapes(self):
        literals = ['QA-QUOTE expected "ready"', 'QA-LINES line one\nline two', r'QA-PATH C:\build\run.log']
        with runtime.opened(self.config):
            record = ledger.append(self.root, 'tool', {'nested': [{'stdout': '\n'.join(literals), 'exit_code': 73}]})
            for text in literals + ['EXIT_CODE', '73']:
                with self.subTest(text=text):
                    self.assertEqual(ledger.search(self.root, text), [record])
            self.assertEqual(ledger.search(self.root, r'line one\nline two'), [])

    def test_append_after_complete_final_record_without_a_newline(self):
        with runtime.opened(self.config):
            first = ledger.append(self.root, 'prompt', {'text': 'first'})
            path = ledger.raw_root(self.root) / ledger.LEDGER
            original = path.read_bytes()
            path.write_bytes(original[:-1])
            self.assertEqual(ledger.verify(self.root)['head'], first['hash'])
            second = ledger.append(self.root, 'assistant', {'text': 'second'})
            self.assertEqual(ledger.replay(self.root), [first, second])
            self.assertEqual(ledger.verify(self.root)['head'], second['hash'])
            self.assertTrue(path.read_bytes().startswith(original))

    def test_short_append_preserves_retryable_record_boundary(self):
        from unittest.mock import patch
        with runtime.opened(self.config):
            first = ledger.append(self.root, 'prompt', {'text': 'first'})
            path = ledger.raw_root(self.root) / ledger.LEDGER
            original = path.read_bytes()[:-1]
            path.write_bytes(original)
            write = ledger.os.write
            with patch.object(ledger.os, 'write', side_effect=lambda fd, data: write(fd, data[:10])):
                with self.assertRaisesRegex(OSError, 'Incomplete ledger append'):
                    ledger.append(self.root, 'assistant', {'text': 'second'})
            self.assertEqual(path.read_bytes(), original)
            second = ledger.append(self.root, 'assistant', {'text': 'second'})
            self.assertEqual(ledger.replay(self.root), [first, second])

    def test_dream_rejects_ledger_truncation_during_worker_execution(self):
        from unittest.mock import patch
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': 'first'})
            ledger.append(self.root, 'prompt', {'text': 'second'})
        path = ledger.raw_root(self.root) / ledger.LEDGER
        first_line, second_line = path.read_bytes().splitlines(keepends=True)

        opts = ledger.settings(self.config)
        for tail in (b'', b'{', b'null\n'):
            with self.subTest(tail=tail):
                path.write_bytes(first_line + second_line)

                def complete_after_truncation(*args, **kwargs):
                    path.write_bytes(first_line + tail)
                    return 0

                with patch.object(ledger.subprocess, 'call', side_effect=complete_after_truncation):
                    self.assertEqual(ledger.dream_run(self.config, self.project, 2, opts), 65)
                with runtime.opened(self.config):
                    state = ledger.dream_state(self.root)
                    self.assertEqual((state['cursor'], state.get('running'), state['last_error']['code']), (0, None, 65))
        path.write_bytes(first_line + second_line)
        with patch.object(ledger.subprocess, 'call', return_value=0):
            self.assertEqual(ledger.dream_run(self.config, self.project, 2, opts), 0)
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root)['cursor'], 2)

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
        line = next(line for line in prompts[0].splitlines() if line.startswith('[ref local:1:'))
        self.assertLessEqual(len(line), dream.RECORD_CAP)

    def test_tool_failure_reaches_ledger_and_background_prompt(self):
        import memory_dream as dream
        command = {'command': 'exit 73'}
        error = 'Exit code 73\nQA-FAILED-STATE-73'
        self.assertEqual(self.event('PostToolUseFailure', tool_name='Bash', tool_input=command,
                                    error=error, is_interrupt=False), {})
        with runtime.opened(self.config):
            records = ledger.replay(self.root, 'A')
        self.assertEqual([(r['kind'], r['data']) for r in records], [
            ('tool', {'tool': 'Bash', 'input': command, 'result': {'error': error, 'is_interrupt': False}})])
        prompts = []
        self.assertEqual(dream.consolidate(self.config, self.project, 1, 1, ledger.settings(self.config),
                                          lambda cmd, text: prompts.append(text) or 0), 0)
        self.assertIn('QA-FAILED-STATE-73', prompts[0])

    def test_codex_worker_supports_ordinary_directories(self):
        import memory_dream as dream
        arguments = dream.AGENTS['codex']
        self.assertIn('--skip-git-repo-check', arguments)
        self.assertEqual(arguments[arguments.index('--sandbox') + 1], 'workspace-write')
        command = dream.agent_command({'dream_agent': 'codex'}, self.root)
        if command:
            # The memory home is opened for writing, not only the project cwd (#69).
            self.assertEqual(command[command.index('--add-dir') + 1], str(self.root))
            command = command[:-1]
            parsed = subprocess.run([*command, '--help'], cwd=self.project, capture_output=True, timeout=30)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)

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




class SourceUpgradeTests(Base):
    def refs(self, *texts):
        with runtime.opened(self.config):
            return [ledger.reference('local', ledger.append(self.root, 'prompt', {'text': t})) for t in texts]

    def test_same_fact_with_full_reference_upgrades_once_then_is_idempotent(self):
        """#32: re-sending the same fact with a full source reference updates only the Why line."""
        good, = self.refs('我習慣 24 小時制')
        with runtime.opened(self.config):
            runtime.remember(self.root, 'user', 'clock', '24 小時制', '來源：ledger seq 1')
            upgraded = runtime.remember(self.root, 'user', 'clock', '24 小時制', '來源：ledger ' + good)
            self.assertEqual(len(upgraded['source_upgraded']), 1)
            text = Path(upgraded['paths'][0]).read_text(encoding='utf-8')
            self.assertIn('Why: 來源：ledger ' + good, text)
            self.assertNotIn('ledger seq 1', text)
            self.assertIn('24 小時制', text)
            again = runtime.remember(self.root, 'user', 'clock', '24 小時制', '來源：ledger ' + good)
            self.assertFalse(again['changed'])
            plain = runtime.remember(self.root, 'user', 'clock', '24 小時制', '使用者再次確認')
            self.assertFalse(plain['changed'])

    def test_unresolvable_reference_is_refused_and_keeps_the_verified_source(self):
        """#33: a well-shaped reference with a wrong hash (or seq) never replaces a verified source."""
        good, = self.refs('我習慣 24 小時制')
        bad_hash = good[:-1] + ('0' if good[-1] != '0' else '1')
        bad_seq = 'local:9:' + good.rsplit(':', 1)[1]
        with runtime.opened(self.config):
            saved = runtime.remember(self.root, 'user', 'clock', '24 小時制', '來源：ledger ' + good)
            path = Path(saved['paths'][0])
            before = path.read_bytes()
            for bad in (bad_hash, bad_seq):
                with self.assertRaisesRegex(ValueError, 'does not resolve'):
                    runtime.remember(self.root, 'user', 'clock', '24 小時制', '來源：ledger ' + bad)
            self.assertEqual(path.read_bytes(), before)

    def test_multi_fact_note_is_not_rewritten_by_one_fact_upgrade(self):
        """#34: one Why line shared by two facts keeps both sources; apply migrates such notes."""
        date_ref, time_ref = self.refs('日期用 ISO', '時間用 UTC')
        with runtime.opened(self.config):
            saved = runtime.remember(self.root, 'qa', 'multifact-date', 'ISO', '來源：ledger seq 1')
            name = Path(saved['paths'][0]).relative_to(self.root).as_posix()
            content = runtime.notes(self.root)[name]
            content = content.replace('來源：ledger seq 1', '日期：ledger seq 1；時間：ledger ' + time_ref)
            content = content.replace('"value": "ISO"}', '"value": "ISO"}, {"subject": "qa", "predicate": '
                                      '"multifact-time", "value": "UTC"}')
            runtime.apply(self.root, {'expected_revision': runtime.revision(self.root),
                                      'updates': {name: content}, 'retire': []})
            before = (self.root / name).read_bytes()
            result = runtime.remember(self.root, 'qa', 'multifact-date', 'ISO', '來源：ledger ' + date_ref)
            self.assertFalse(result['changed'])
            self.assertNotIn('source_upgraded', result)
            self.assertEqual((self.root / name).read_bytes(), before)
            self.assertIn(time_ref, (self.root / name).read_text(encoding='utf-8'))

    def test_overlong_or_short_reference_is_refused_like_source_ref(self):
        """#35: a 13- or 11-character hash is not a valid reference; it never replaces a source."""
        first, second = self.refs('時間用 24 小時制', '再確認一次 24 小時制')
        with runtime.opened(self.config):
            saved = runtime.remember(self.root, 'user', 'clock', '24 小時制', '來源：ledger ' + first)
            path = Path(saved['paths'][0])
            before = path.read_bytes()
            for bad in (second + 'f', second[:-1]):
                with self.assertRaisesRegex(ValueError, 'does not resolve'):
                    runtime.remember(self.root, 'user', 'clock', '24 小時制', '來源：ledger ' + bad)
                with self.assertRaises(ValueError):
                    ledger.source({'project': self.root}, bad)
            self.assertEqual(path.read_bytes(), before)

    def test_new_note_and_apply_cannot_publish_an_unresolvable_source(self):
        """#36: first-time remember and apply verify newly cited references too."""
        good, = self.refs('日期用 ISO')
        bad = good[:-1] + ('0' if good[-1] != '0' else '1')
        with runtime.opened(self.config):
            with self.assertRaisesRegex(ValueError, 'does not resolve'):
                runtime.remember(self.root, 'qa', 'new-invalid-ref', 'ISO', '來源：ledger ' + bad)
            self.assertFalse(any('ISO' in text for text in runtime.notes(self.root).values()))
            saved = runtime.remember(self.root, 'qa', 'apply-invalid-ref', 'ISO', '來源：ledger ' + good)
            name = Path(saved['paths'][0]).relative_to(self.root).as_posix()
            before = (self.root / name).read_bytes()
            content = runtime.notes(self.root)[name].replace(good, bad)
            with self.assertRaisesRegex(ValueError, 'does not resolve'):
                runtime.apply(self.root, {'expected_revision': runtime.revision(self.root),
                                          'updates': {name: content}, 'retire': []})
            self.assertEqual((self.root / name).read_bytes(), before)
            # Unchanged references in an edited note are not re-checked, valid ones still pass.
            edited = runtime.notes(self.root)[name].replace('\n\nWhy: ', '\n\n已確認。\n\nWhy: ', 1)
            self.assertTrue(runtime.apply(self.root, {'expected_revision': runtime.revision(self.root),
                                                      'updates': {name: edited}, 'retire': []})['changed'])

    def test_literal_examples_are_not_checked_as_active_sources(self):
        """#42: literal examples can coexist with a verifiable citation in the same note."""
        good, = self.refs('日期用 ISO')
        fake = 'local:900:ab12cd34ef56'
        with runtime.opened(self.config):
            saved = runtime.remember(self.root, 'qa', 'example-format', 'ISO', f'來源：ledger `{good}`')
            name = Path(saved['paths'][0]).relative_to(self.root).as_posix()
            original = runtime.notes(self.root)[name]
            examples = (f'\n```text\n{fake}\n```\n\n~~~~\n{fake}\n~~~~\n\n'
                        f'    {fake}\n\n<!-- {fake} -->\n')
            plan = {'expected_revision': runtime.revision(self.root), 'updates': {name: original + examples}, 'retire': []}
            self.assertTrue(runtime.apply(self.root, plan)['changed'])
            self.assertEqual(runtime.notes(self.root)[name], original + examples)
            self.assertIn(good, runtime.notes(self.root)[name])
            plan['expected_revision'] = runtime.revision(self.root)
            plan['updates'][name] += f'\n來源：ledger `{fake}`\n'
            with self.assertRaisesRegex(ValueError, 'does not resolve'):
                runtime.apply(self.root, plan)
            self.assertEqual(runtime.notes(self.root)[name], original + examples)

    def test_rebuilding_a_legacy_source_even_when_the_same_ref_is_in_a_fence(self):
        good, = self.refs('日期用 ISO')
        with runtime.opened(self.config):
            saved = runtime.remember(self.root, 'qa', 'legacy-fence', 'ISO', '來源：ledger seq 1')
            name = Path(saved['paths'][0]).relative_to(self.root).as_posix()
            note = runtime.notes(self.root)[name] + f'\n```text\n{good}\n```\n'
            runtime.apply(self.root, {'expected_revision': runtime.revision(self.root),
                                      'updates': {name: note}, 'retire': []})
            migrated = runtime.remember(self.root, 'qa', 'legacy-fence', 'ISO', f'來源：ledger {good}')
            self.assertTrue(migrated['changed'])
            self.assertIn(f'Why: 來源：ledger {good}\n', runtime.notes(self.root)[name])


class IntegrityTests(Base):
    ledger_cfg = {'enabled': True, 'dream_every': 1000}

    def cli(self, script, *args):
        return subprocess.run([sys.executable, str(ROOT / 'tools' / script), '--config', str(self.config), *args],
                              capture_output=True, text=True, encoding='utf-8', timeout=60)

    def test_structured_credential_fields_are_redacted_before_the_ledger(self):
        """#38: a credential under a sensitive key is masked whatever the value looks like."""
        value = 'QA_ONLY_value_7'
        with runtime.opened(self.config):
            ledger.append(self.root, 'tool', {'input': {'command': 'login password=' + value},
                                              'result': {'password': value, 'nested': {'api_key': value},
                                                         'tokens': [value], 'input_tokens': 12, 'note': 'ok'}})
            record = ledger.replay(self.root)[-1]
        text = json.dumps(record, ensure_ascii=False)
        self.assertNotIn(value, text)
        self.assertEqual(record['data']['result']['password'], '[REDACTED]')
        self.assertEqual(record['data']['result']['nested']['api_key'], '[REDACTED]')
        self.assertEqual(record['data']['result']['input_tokens'], 12)
        self.assertEqual(record['data']['result']['note'], 'ok')
        self.assertNotIn(value, (ledger.raw_root(self.root) / 'ledger.jsonl').read_text(encoding='utf-8'))

    def test_structured_redaction_handles_scalar_types_and_preserves_usage(self):
        """#43: mask credential values, including numeric array leaves, without losing metrics."""
        values = {'password': 987612, 'token': 981234, 'tokens': [123, {'code': False}],
                  'clientSecret': True, 'X-API-Key': 'QA-only', 'service_token': 17,
                  'accessTokens': [7], '私鑰': 5, 'token_count': '1240', 'tokenizer': 'sample_bpe',
                  'max_output_tokens': '40', 'credentials_required': True, 'note': 'keep-me',
                  'oauthTokens': ['qa-only'], 'headers.authorization': 'qa-only', 'accessTokenValue': 123,
                  'usage.input_tokens': 82, 'tokenCount': 16}
        expected = {'password': '[REDACTED]', 'token': '[REDACTED]',
                    'tokens': ['[REDACTED]', {'code': '[REDACTED]'}],
                    'clientSecret': '[REDACTED]', 'X-API-Key': '[REDACTED]', 'service_token': '[REDACTED]',
                    'accessTokens': ['[REDACTED]'], '私鑰': '[REDACTED]', 'token_count': '1240',
                    'tokenizer': 'sample_bpe', 'max_output_tokens': '40', 'credentials_required': True,
                    'note': 'keep-me', 'oauthTokens': ['[REDACTED]'], 'headers.authorization': '[REDACTED]',
                    'accessTokenValue': '[REDACTED]', 'usage.input_tokens': 82, 'tokenCount': 16}
        with runtime.opened(self.config):
            ledger.append(self.root, 'tool', {'result': values})
            self.assertEqual(ledger.replay(self.root)[-1]['data']['result'], expected)

    def test_corrupt_document_mirror_fails_verify_and_source(self):
        """#39: a mirrored blob whose bytes changed is reported, not served as the exact text."""
        document = self.project / 'terms.txt'
        document.write_bytes(b'Clause 7: refunds within 14 days.\n')
        with runtime.opened(self.config):
            mirrored = ledger.mirror(self.root, document)
            ref = ledger.reference('local', ledger.replay(self.root)[-1])
            self.assertTrue(ledger.source({'project': self.root}, ref)['blob'])
        self.assertEqual(self.cli('memory_ledger.py', 'verify').returncode, 0)
        blob = Path(mirrored['blob'])
        data = bytearray(blob.read_bytes())
        data[-1] ^= 1
        blob.write_bytes(bytes(data))
        verify = self.cli('memory_ledger.py', 'verify')
        self.assertEqual(verify.returncode, 1, verify.stdout)
        self.assertIn('corrupt', verify.stderr)
        found = self.cli('memory_ledger.py', 'source', '--ref', ref)
        self.assertEqual(found.returncode, 1, found.stdout)
        blob.unlink()
        self.assertIn('missing', self.cli('memory_ledger.py', 'verify').stderr)

    def test_truncated_consolidated_ledger_fails_verify_and_prompt(self):
        """#40: verify and prompt exit non-zero when the ledger lost records below the cursor."""
        with runtime.opened(self.config):
            for text in ('一', '二', '三'):
                ledger.append(self.root, 'prompt', {'text': text})
            owner = ledger.claim_job(self.root, 'dream', 3)
            self.assertTrue(ledger.release_job(self.root, owner, 3))
        self.assertEqual(self.cli('memory_ledger.py', 'verify').returncode, 0)
        self.assertEqual(self.cli('memory_dream.py', 'prompt').returncode, 0)
        path = ledger.raw_root(self.root) / 'ledger.jsonl'
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b''.join(lines[:-1]))
        verify = self.cli('memory_ledger.py', 'verify')
        self.assertEqual(verify.returncode, 1, verify.stdout)
        self.assertIn('truncated', verify.stderr)
        prompt = self.cli('memory_dream.py', 'prompt')
        self.assertNotEqual(prompt.returncode, 0)
        self.assertEqual(prompt.stdout, '')
        self.assertEqual(self.cli('memory_ledger.py', 'replay').returncode, 1)

    def test_codex_notify_skips_a_redelivered_turn_but_keeps_new_turns(self):
        """#37: the same turn-id delivered again adds nothing; an interrupted delivery is completed."""
        import codex_notify
        payload = {'type': 'agent-turn-complete', 'thread-id': 'matrix-codex', 'turn-id': 'matrix-turn-1',
                   'cwd': str(self.project), 'input-messages': ['提示'], 'last-assistant-message': '回覆'}
        codex_notify.handle(payload)
        codex_notify.handle(dict(payload))
        codex_notify.handle(dict(payload, **{'turn-id': 'matrix-turn-2'}))
        with runtime.opened(self.config):
            events = ledger.replay(self.root, 'matrix-codex')
        self.assertEqual([(r['kind'], r['data']['turn']) for r in events],
                         [('prompt', 'matrix-turn-1'), ('assistant', 'matrix-turn-1'),
                          ('prompt', 'matrix-turn-2'), ('assistant', 'matrix-turn-2')])
        # Interrupted between prompt and reply: a retry records only the missing reply.
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': '提示', 'host': 'codex', 'turn': 'matrix-turn-3',
                                                'part': 0}, 'matrix-codex')
        codex_notify.handle(dict(payload, **{'turn-id': 'matrix-turn-3'}))
        with runtime.opened(self.config):
            third = [r['kind'] for r in ledger.replay(self.root, 'matrix-codex') if r['data']['turn'] == 'matrix-turn-3']
        self.assertEqual(third, ['prompt', 'assistant'])

    def test_redelivered_codex_turn_retries_a_failed_worker_without_appending(self):
        """#41: a notification can retry work even when every turn record is already present."""
        import codex_notify
        cfg = runtime.read_json(self.config)
        cfg['ledger'].update(dream_every=2, dream_command=[sys.executable, '-c', 'raise SystemExit(27)'])
        self.config.write_text(json.dumps(cfg), encoding='utf-8')
        payload = {'type': 'agent-turn-complete', 'thread-id': 'qa-session', 'turn-id': 'qa-turn',
                   'cwd': str(self.project), 'input-messages': ['QA prompt'], 'last-assistant-message': 'QA result'}
        self.assertEqual(codex_notify.handle(payload), 2)
        self.assertIsNone(codex_notify.handle(payload))  # The first worker already owns the slot.
        with runtime.opened(self.config):
            owner = ledger.pending(self.root)['running']['owner']
        opts = ledger.settings(self.config)
        self.assertEqual(ledger.dream_run(self.config, self.project, 2, opts, owner), 27)
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root)['last_error']['code'], 27)
            original = ledger.replay(self.root)
        self.assertEqual(codex_notify.handle(payload), 2)
        self.assertEqual(len(self.spawned_workers), 2)
        with runtime.opened(self.config):
            owner = ledger.pending(self.root)['running']['owner']
            self.assertEqual(ledger.replay(self.root), original)
        self.assertEqual(ledger.dream_run(self.config, self.project, 2,
                                          dict(opts, dream_command=[sys.executable, '-c', 'pass']), owner), 0)
        self.assertIsNone(codex_notify.handle(payload))
        self.assertEqual(len(self.spawned_workers), 2)
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root)['cursor'], 2)


class ExportTests(Base):
    """#27: export writes UTF-8 itself, so an ASCII console/pipe cannot corrupt Chinese text."""

    def test_export_keeps_chinese_with_an_ascii_console(self):
        import os
        import subprocess
        self.remember('report.order', '回報先列結論，再說明原因')
        target = self.project / 'exports' / '記憶匯出.md'
        env = dict(os.environ, PYTHONIOENCODING='ascii', PYTHONUTF8='0')
        result = subprocess.run([sys.executable, str(ROOT / 'tools/memory_runtime.py'), '--config', str(self.config),
                                 'export', '--output', str(target)], capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = target.read_bytes().decode('utf-8')
        self.assertIn('# 記憶匯出（Jason-memory）', text)
        self.assertIn('回報先列結論，再說明原因', text)
        self.assertNotIn('??', text)
        again = subprocess.run([sys.executable, str(ROOT / 'tools/memory_runtime.py'), '--config', str(self.config),
                                'export', '--output', str(target)], capture_output=True, env=env)
        self.assertEqual(again.returncode, 1)  # Never overwrites an earlier export.

    def test_export_cannot_write_into_the_store_via_dotdot(self):
        """#29: '..' and aliases are normalised before the memory-store guard."""
        import os
        import subprocess
        (self.project / 'outside').mkdir()
        before = runtime.revision(self.root)
        sneaky = str(self.project / 'outside' / '..' / '.jason-memory' / 'export.md')
        result = subprocess.run([sys.executable, str(ROOT / 'tools/memory_runtime.py'), '--config', str(self.config),
                                 'export', '--output', sneaky], capture_output=True, text=True, encoding='utf-8',
                                env=dict(os.environ, PYTHONUTF8='1'))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('outside the memory store', result.stderr)
        self.assertFalse((self.root / 'export.md').exists())
        self.assertEqual(runtime.revision(self.root), before)

@unittest.skipUnless(sys.platform == 'win32', 'Windows console behaviour')
class WindowsNoConsoleWindowTests(unittest.TestCase):
    """#28: background processes must not pop up empty terminal windows."""

    def test_worker_and_children_use_a_hidden_console(self):
        from unittest.mock import patch
        import subprocess
        import memory_dream as dream
        with patch.object(subprocess, 'Popen') as popen:
            ledger._spawn(['x'])
        flags = popen.call_args.kwargs['creationflags']
        self.assertTrue(flags & 0x08000000)          # CREATE_NO_WINDOW
        self.assertFalse(flags & 0x00000008)         # not DETACHED_PROCESS
        with patch.object(subprocess, 'Popen') as agent:
            agent.return_value.returncode = 0
            agent.return_value.stdout.read.return_value = b'JASON_DREAM_STATUS: ok'
            self.assertEqual(dream._run_agent(['agent'], '', {}, Path.cwd()), 0)
        self.assertTrue(agent.call_args.kwargs['creationflags'] & 0x08000000)

if __name__ == '__main__':
    unittest.main()
