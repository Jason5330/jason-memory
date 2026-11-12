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
        self.assertEqual(ledger.dream_run(self.config, self.project, 3, failing), 3)
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root)['cursor'], 0)
            self.assertEqual(ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append), 3)
        self.assertEqual(ledger.dream_run(self.config, self.project, 3, opts), 0)
        with runtime.opened(self.config):
            self.assertEqual(ledger.pending(self.root), dict(ledger.pending(self.root), cursor=3, pending=0))
            # No new records: nothing runs again.
            self.assertIsNone(ledger.maybe_dream(self.root, self.config, self.project, opts, spawned.append))
        self.assertEqual(len(spawned), 2)

    def test_disabled_ledger_writes_nothing(self):
        self.config.write_text('{"version":1,"mode":"project","memory_root":".jason-memory"}', encoding='utf-8')
        self.event('UserPromptSubmit', prompt='hi')
        self.assertFalse((self.root / '.raw/ledger.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
