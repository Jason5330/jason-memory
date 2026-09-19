import json
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import memory_runtime as runtime
import memory_facts
import claude_memory_hook as hook
import hook_settings


def note(slug, value='doctor', subject='user:anna', extra=None, body=None):
    facts = [{'subject': subject, 'predicate': 'occupation', 'value': value}]
    if extra:
        facts.append(extra)
    return ('---\nname: ' + slug + '\ndescription: Current occupation\ntype: user\n'
            'created: 2026-09-18\nupdated: 2026-09-18\n---\n\n' + (body or 'Current occupation: ' + value) +
            '\n\n```jason-facts\n' + json.dumps(facts, ensure_ascii=False) + '\n```\n')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve()
        self.config = self.project / '.jason-memory.json'
        self.config.write_text('{"version":1,"mode":"project","memory_root":".jason-memory"}', encoding='utf-8')
        with runtime.opened(self.config) as (_, _, roots):
            self.root = roots['project']

    def apply(self, updates, retire=None, revision=None):
        with runtime.opened(self.config):
            result = runtime.apply(self.root, {'expected_revision': revision or runtime.revision(self.root),
                                             'updates': updates, 'retire': retire or []})
        if list((self.root / '.hook-state').glob('*.json')):
            self.event('PostToolUse', tool_name='Bash', tool_response={'stdout': json.dumps(result)})
        return result

    def event(self, name, **kwargs):
        return hook.handle({'hook_event_name': name, 'session_id': 'test', 'cwd': str(self.project), **kwargs}, self.config)

    def test_remember_generates_valid_note_and_current_receipt_in_one_call(self):
        self.event('SessionStart')
        with runtime.opened(self.config):
            saved = runtime.remember(self.root, 'user', 'response.order', '先結論，再細節', '使用者明確要求')
        self.event('PostToolUse', tool_name='Bash', tool_response={'stdout': json.dumps(saved)})
        self.assertTrue(saved['changed'])
        self.assertIn('jason-facts', Path(saved['paths'][0]).read_text(encoding='utf-8'))
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice(Path(saved['paths'][0]))), {})

    def test_remember_duplicate_verifies_without_rewriting_or_archiving(self):
        with runtime.opened(self.config):
            first = runtime.remember(self.root, 'user', 'response.order', '結論優先', '使用者要求')
            path = Path(first['paths'][0]);mtime = path.stat().st_mtime_ns
            second = runtime.remember(self.root, 'USER', 'Response.Order', '結論優先', '再次確認')
        self.assertFalse(second['changed'])
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertNotEqual(first['receipt'], second['receipt'])
        self.assertFalse((self.root / 'archive').exists())

    def test_remember_changed_fact_rejects_without_damaging_mixed_notes(self):
        self.apply({'job.md': note('job', extra={'subject': 'user:anna', 'predicate': 'name', 'value': 'ANNA'})})
        before = runtime.revision(self.root)
        with runtime.opened(self.config), self.assertRaisesRegex(ValueError, 'batch-correct'):
            runtime.remember(self.root, 'user:anna', 'occupation', 'teacher', 'Correction')
        self.assertEqual(runtime.revision(self.root), before)

    def test_remember_duplicate_checks_every_related_note(self):
        self.apply({'one.md': note('one'), 'two.md': note('two')})
        with runtime.opened(self.config):
            saved = runtime.remember(self.root, 'user:anna', 'occupation', '醫生', 'Already known')
        self.assertEqual(len(saved['verified_paths']), 2)
        self.assertFalse(saved['changed'])

    def test_remember_rejects_empty_or_multiline_input(self):
        for value in ('', 'one\ntwo', '```jason-facts'):
            with self.subTest(value=value), runtime.opened(self.config), self.assertRaises(ValueError):
                runtime.remember(self.root, 'user', 'response.order', value, 'User request')
        self.assertEqual(runtime.notes(self.root), {})

    def test_batch_does_not_rewrite_unrelated_notes(self):
        self.apply({'one.md': note('one'), 'two.md': note('two', subject='user:bob')})
        timestamp = (self.root / 'two.md').stat().st_mtime_ns
        self.apply({'one.md': note('one', 'teacher')})
        self.assertEqual((self.root / 'two.md').stat().st_mtime_ns, timestamp)

    def test_apply_validates_once_but_recovery_still_validates(self):
        with patch.object(runtime, 'validate', wraps=runtime.validate) as validate:
            self.apply({'one.md': note('one')})
            self.assertEqual(validate.call_count, 1)

    def test_ordinary_post_tools_do_not_scan_store_but_stop_catches_damage(self):
        self.event('SessionStart')
        with patch.object(runtime, 'context', side_effect=AssertionError('unnecessary scan')):
            self.assertEqual(self.event('PostToolUse', tool_name='Read', tool_response={'content': 'report'}), {})
            self.assertEqual(self.event('PostToolUse', tool_name='Bash', tool_response={'stdout': 'hello'}), {})
        (self.root / 'broken.md').write_text('broken note', encoding='utf-8')
        self.assertEqual(self.event('Stop', last_assistant_message='完成')['decision'], 'block')

    def test_configured_framework_reads_allowed_parallel_stores_still_denied(self):
        framework = self.project / '.jason-memory-global/framework'
        (framework / 'tools').mkdir(parents=True)
        with patch.object(runtime, '__file__', str(framework / 'tools/memory_runtime.py')):
            for relative in ('SKILL.md', 'docs/memory-runtime.md', 'tools/memory_runtime.py'):
                path = framework / relative;path.parent.mkdir(exist_ok=True);path.write_text('test', encoding='utf-8')
                self.assertEqual(self.event('PreToolUse', tool_name='Read', tool_input={'file_path': str(path)}), {})
            wrong = self.project / '.jason-memory-global/memory/other/MEMORY.md'
            result = self.event('PreToolUse', tool_name='Read', tool_input={'file_path': str(wrong)})
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_cli_stdin_batch_and_utf8_on_ascii_console(self):
        plan = {'expected_revision': runtime.revision(self.root), 'updates': {'job.md': note('job', body='醫生')}, 'retire': []}
        import os
        result = subprocess.run([sys.executable, str(ROOT / 'tools/memory_runtime.py'), '--config', str(self.config),
                                 'apply', '--plan', '-'], input=json.dumps(plan).encode('utf-8'), capture_output=True,
                                env=dict(os.environ, PYTHONIOENCODING='ascii'))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout.decode('utf-8'))
        self.assertTrue(output['changed'])
        self.assertNotIn(b'\\u2014', result.stdout)

    def test_hook_matchers_avoid_unnecessary_shell_pre_and_read_post(self):
        settings = json.loads(hook_settings.merged(b'', 'python claude_memory_hook.py --jason-hook'))
        self.assertNotIn('Bash', settings['hooks']['PreToolUse'][0]['matcher'])
        self.assertNotIn('Read', settings['hooks']['PostToolUse'][0]['matcher'])

    def test_verified_notice_does_not_require_specific_success_vocabulary(self):
        self.event('SessionStart')
        self.apply({'job.md': note('job')})
        reply = '已將「先結論再細節」設為全域偏好。\n驗證結果：儲存成功。\n筆記：[格式](' + str(self.root / 'job.md') + ')'
        self.assertEqual(self.event('Stop', last_assistant_message=reply), {})

    def test_fast_paths_preserve_index_reading_limit(self):
        original = runtime.revision(self.root)
        index = self.root / 'MEMORY.md'
        index.write_bytes(index.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(runtime.revision(self.root), original)
        (self.root / 'MEMORY.md').write_text('x' * 101, encoding='utf-8')
        with patch.object(runtime.storelib.doctor, 'INDEX_READ_CAP', 100):
            for action in (lambda: runtime.revision(self.root),
                           lambda: runtime.apply(self.root, {'expected_revision': 'stale', 'updates': {}, 'retire': []}),
                           lambda: runtime.remember(self.root, 'user', 'order', 'conclusion-first', 'user request')):
                with self.assertRaisesRegex(ValueError, 'Index exceeds'):
                    action()

    def test_cross_note_partial_correction_rejected_batch_succeeds(self):
        extra = {'subject': 'user:anna', 'predicate': 'preferred-name', 'value': 'ANNA'}
        self.apply({'job.md': note('job', 'teacher'), 'name.md': note('name', 'teacher', extra=extra)})
        previous = runtime.revision(self.root)
        with self.assertRaisesRegex(ValueError, 'Fact conflict'):
            self.apply({'job.md': note('job')})
        self.assertEqual(runtime.revision(self.root), previous)
        result = self.apply({'job.md': note('job'), 'name.md': note('name', extra=extra)})
        self.assertTrue(result['notice_required'])
        self.assertIn('preferred-name', (self.root / 'name.md').read_text(encoding='utf-8'))
        self.assertEqual(len(list((self.root / 'archive').rglob('job.md'))), 1)
        self.assertFalse(memory_facts.audit(runtime.notes(self.root))['conflicts'])

    def test_stale_writer_rejected(self):
        revision = runtime.revision(self.root)
        self.apply({'job.md': note('job')})
        with self.assertRaisesRegex(ValueError, 'Revision conflict'):
            self.apply({'other.md': note('other', 'teacher')}, revision=revision)

    def test_two_processes_same_revision_one_wins_retry_preserves_both(self):
        version = runtime.revision(self.root)
        processes = []
        for slug in ('alice', 'bob'):
            plan = self.project / (slug + '.json')
            plan.write_text(json.dumps({'expected_revision': version,
                            'updates': {slug + '.md': note(slug, subject='user:' + slug)}, 'retire': []}), encoding='utf-8')
            processes.append(subprocess.Popen([sys.executable, str(ROOT / 'tools/memory_runtime.py'),
                             '--config', str(self.config), 'apply', '--plan', str(plan)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for process in processes:
            process.communicate(timeout=30)
        self.assertEqual(sorted(p.returncode for p in processes), [0, 1])
        data = runtime.notes(self.root)
        missing = 'bob' if 'alice.md' in data else 'alice'
        self.apply({missing + '.md': note(missing, subject='user:' + missing)})
        self.assertEqual(set(runtime.notes(self.root)), {'alice.md', 'bob.md'})

    def test_redirected_memory_root_rejected(self):
        other = self.project / 'outside'
        other.mkdir()
        alias = self.project / 'redirect'
        try:
            alias.symlink_to(other, target_is_directory=True)
        except OSError:
            self.skipTest('Host does not permit directory symlinks')
        self.config.write_text('{"version":1,"mode":"project","memory_root":"redirect"}')
        with self.assertRaisesRegex(ValueError, 'Redirected'):
            runtime.configuration(self.config)

    def test_unrelated_identified_fact_cannot_disappear_in_correction(self):
        self.apply({'name.md': note('name', extra={'subject': 'user:anna', 'predicate': 'preferred-name', 'value': 'ANNA'})})
        with self.assertRaisesRegex(ValueError, 'drops unrelated'):
            self.apply({'name.md': note('name', 'teacher')})

    def test_missing_fact_block_rejected_for_new_preference(self):
        with self.assertRaisesRegex(ValueError, 'require a jason-facts'):
            self.apply({'name.md': note('name').split('```')[0]})

    def test_manual_index_corruption_reported(self):
        self.apply({'job.md': note('job')})
        (self.root / 'MEMORY.md').write_text('# Index\n- [broken](missing.md)\n', encoding='utf-8')
        result = self.event('SessionStart')['hookSpecificOutput']['additionalContext']
        self.assertIn('CONFLICT', result)
        self.assertIn('missing.md', result)

    def test_index_derived_from_current_description(self):
        self.apply({'user/job.md': note('job').replace('Current occupation\n', '醫生\n')})
        index = (self.root / 'MEMORY.md').read_text(encoding='utf-8')
        self.assertIn('user/job.md', index)
        self.assertIn('醫生', index)

    def test_legacy_uppercase_extension_remains_readable(self):
        self.apply({'job.MD': note('job')})
        self.assertIn('job.MD', runtime.notes(self.root))

    def test_noop_and_retirement(self):
        self.apply({'job.md': note('job')})
        self.assertFalse(self.apply({'job.md': note('job')})['changed'])
        result = self.apply({}, ['job.md'])
        self.assertTrue(result['notice_required'])
        self.assertFalse((self.root / 'job.md').exists())
        self.assertIn('archive/MEMORY.md', (self.root / 'MEMORY.md').read_text(encoding='utf-8'))

    def test_replay_after_each_atomic_write_boundary(self):
        self.apply({'a.md': note('a', 'teacher'), 'b.md': note('b', 'teacher')})
        original = runtime.storelib.atomic_write
        for boundary in range(1, 8):
            desired = 'doctor' if boundary % 2 else 'teacher'
            counter = [0]
            def interrupted(path, content):
                original(path, content)
                counter[0] += 1
                if counter[0] == boundary:
                    raise OSError('simulated interruption')
            with patch.object(runtime.storelib, 'atomic_write', interrupted):
                try:
                    self.apply({'a.md': note('a', desired), 'b.md': note('b', desired)})
                except OSError:
                    pass
            with runtime.opened(self.config):
                data = runtime.notes(self.root)
                self.assertIn(desired, data['a.md'])
                self.assertIn(desired, data['b.md'])
                self.assertFalse(memory_facts.audit(data)['conflicts'])
                self.assertFalse((self.root / '.transaction.json').exists())

    def test_path_escape_and_invalid_config(self):
        for name in ('../escape.md', 'archive/x.md', 'MEMORY.md', 'x\\bad.md', '.secret/x.md'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.apply({name: note('escape')})
        self.config.write_text('{"version":1,"mode":"project","memory_root":"../other"}')
        with self.assertRaises(ValueError):
            runtime.configuration(self.config)

    def test_legacy_anna_and_negation(self):
        data = {'name.md': 'ANNA 的職業是醫生，不是老師也不是可靠性工程師',
                'job.md': 'ANNA 是可靠性工程師'}
        report = memory_facts.audit(data)
        self.assertEqual(len(report['conflicts']), 1)
        self.assertEqual(set(report['conflicts'][0]['values']), {'doctor', 'reliability engineer'})
        self.assertEqual(memory_facts.extract('ANNA 不是老師')[0], [])
        self.assertEqual(memory_facts.extract('> ANNA 是老師')[0], [])

    def test_distinct_users_and_aliases(self):
        self.assertFalse(memory_facts.audit({'a': note('a'), 'b': note('b', 'teacher', 'user:bob')})['conflicts'])
        self.assertFalse(memory_facts.audit({'a': note('a'), 'b': note('b', '醫生')})['conflicts'])
        self.assertTrue(memory_facts.audit({'a': note('a', body='ANNA 是老師')})['conflicts'])

    def test_unknown_legacy_not_certified_clean(self):
        report = memory_facts.audit({'x.md': 'Her favorite color is amber.'})
        self.assertEqual(report['semantic_review_needed'], ['x.md'])

    def test_formatting_fact_values_preserve_case_and_whitespace(self):
        def text(value):
            return '```jason-facts\n' + json.dumps([{'subject': 'user:self', 'predicate': 'reply.prefix', 'value': value}]) + '\n```'
        for value in ('hello', 'HELLO '):
            self.assertTrue(memory_facts.audit({'a': text('HELLO'), 'b': text(value)})['conflicts'])

    def test_new_session_refresh_compact_and_no_chatter(self):
        self.apply({'job.md': note('job', 'teacher')})
        first = self.event('SessionStart')
        self.assertIn('teacher', first['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.event('UserPromptSubmit', prompt='HI'), {})
        self.apply({'job.md': note('job', 'doctor')})
        refresh = self.event('UserPromptSubmit', prompt='HI')
        self.assertIn('doctor', refresh['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('teacher', refresh['hookSpecificOutput']['additionalContext'])
        self.assertIn('doctor', self.event('SessionStart', source='compact')['hookSpecificOutput']['additionalContext'])

    def test_wrong_path_and_direct_write_denied_normal_files_allowed(self):
        for tool, path in [('Read', self.project / '.ai-memory/MEMORY.md'), ('Write', self.root / 'job.md')]:
            response = self.event('PreToolUse', tool_name=tool, tool_input={'file_path': str(path)})
            self.assertEqual(response['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': str(self.project / 'report.md')}), {})
        self.assertEqual(self.event('PreToolUse', tool_name='Read', tool_input={'file_path': str(self.root / 'MEMORY.md')}), {})

    def test_missing_notice_blocked_once_and_links_required(self):
        self.event('SessionStart')
        self.apply({'job.md': note('job')})
        self.assertEqual(self.event('Stop', last_assistant_message='完成')['decision'], 'block')
        self.assertFalse(self.event('Stop', last_assistant_message='完成', stop_hook_active=True)['continue'])
        self.assertEqual(self.event('Stop', last_assistant_message='已將此要求記憶在 [job.md](' + str(self.root / 'job.md') + ')。'), {})

    def test_no_change_no_notification(self):
        self.event('SessionStart')
        self.assertEqual(self.event('Stop', last_assistant_message='4'), {})

    def test_notice_must_link_changed_file_and_retired_archive(self):
        self.event('SessionStart')
        self.apply({'job.md': note('job')})
        wrong = '已記住 job.md。[其他](' + str(self.root / 'MEMORY.md') + ')'
        self.assertEqual(self.event('Stop', last_assistant_message=wrong)['decision'], 'block')
        self.event('UserPromptSubmit')
        result = self.apply({}, ['job.md'])
        good = '已封存此記憶：[歷史](' + result['paths'][0] + ')'
        self.assertEqual(self.event('Stop', last_assistant_message=good), {})

    def test_overflow_explicit(self):
        self.apply({'job.md': note('job', body='x' * 12000)})
        result = self.event('SessionStart')['hookSpecificOutput']['additionalContext']
        self.assertLessEqual(len(result), hook.LIMIT)
        self.assertIn('PARTIAL SNAPSHOT', result)

    def test_merge_hooks_preserves_others_and_uninstalls_only_owned(self):
        original = {'permissions': {'deny': ['Bash(rm:*)']}, 'hooks': {'Stop': [
            {'hooks': [{'type': 'command', 'command': 'echo custom'}]}]}}
        encoded = json.dumps(original).encode()
        command = 'python claude_memory_hook.py --jason-hook'
        once = hook_settings.merged(encoded, command)
        self.assertEqual(once, hook_settings.merged(once, command))
        self.assertEqual(json.loads(hook_settings.merged(once)), original)

    def test_global_sharing_scope_isolation_and_local_precedence(self):
        config = self.project / 'global.json'
        config.write_text('{"version":1,"mode":"global","home":"shared"}')
        second = self.project / 'second'
        second.mkdir()
        with runtime.opened(config, second) as (_, _, roots):
            runtime.apply(roots['global'], {'expected_revision': runtime.revision(roots['global']),
                          'updates': {'job.md': note('job')}, 'retire': []}, 'global')
            global_root, project_root = roots['global'], roots['project']
        with runtime.opened(config, self.project) as (_, _, roots):
            self.assertEqual(roots['global'], global_root)
            self.assertNotEqual(roots['project'], project_root)
            self.assertIn('job.md', runtime.notes(roots['global']))
        self.assertEqual(hook.handle({'hook_event_name': 'SessionStart', 'cwd': str(self.project)}, config), {})

    def test_global_session_keeps_root_after_working_directory_change(self):
        self.config.unlink()  # A global-only workspace, not a local project override.
        config = self.project / 'global.json'
        config.write_text('{"version":1,"mode":"global","home":"shared"}')
        event = {'hook_event_name': 'SessionStart', 'session_id': 'pinned-root', 'cwd': str(self.project)}
        first = hook.handle(event, config)
        child = self.project / 'child'
        child.mkdir()
        event.update(hook_event_name='UserPromptSubmit', cwd=str(child))
        self.assertEqual(hook.handle(event, config), {})
        self.assertIn(str(self.project), first['hookSpecificOutput']['additionalContext'])

    def notice(self, path=None):
        return '已將此要求記憶在 [筆記](' + str(path or self.root / 'job.md') + ')，健檢通過。'

    def test_false_success_without_any_write_is_blocked(self):
        self.event('SessionStart')
        reply = self.notice(self.root / 'feedback/report-structure-first.md')
        result = self.event('Stop', last_assistant_message=reply)
        self.assertEqual(result['decision'], 'block')
        self.assertIn('Unsupported memory-success', result['reason'])
        self.assertFalse((self.root / 'feedback/report-structure-first.md').exists())

    def test_false_success_without_startup_baseline_is_blocked(self):
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice())['decision'], 'block')
        self.apply({'job.md': note('job')})
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice(), stop_hook_active=True), {})

    def test_plain_chinese_simplified_english_claims_are_checked(self):
        phrases = ['了解，這個偏好已記錄。', '已記住。', '已經保存這個需求。', '此需求已紀錄。', '記住了。', '偏好已寫入。',
                   '已將這項要求記憶在記憶系統。', '偏好已保存到記憶系統。',
                   '这个偏好已经记录。', 'Memory saved.', 'I have saved your preference.', 'Your preference has been recorded.']
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.event('UserPromptSubmit')
                self.assertEqual(self.event('Stop', last_assistant_message=phrase)['decision'], 'block')

    def test_failed_future_quoted_and_unrelated_statements_do_not_trigger(self):
        phrases = ['尚未保存記憶。', '無法保存這個要求。', 'I have not saved your preference.',
                   '我會在保存成功後通報。', '範例：「已記住」。', '> 已記住。',
                   '```text\n已保存記憶。\n```', '已更新 Excel 表格。', '已修補記憶工具。',
                   '已更新 README。\n這份文件說明記憶功能。', '42']
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.event('UserPromptSubmit')
                self.assertEqual(self.event('Stop', last_assistant_message=phrase), {})

    def test_persistent_false_success_ends_with_explicit_failure_warning(self):
        self.event('SessionStart')
        self.event('Stop', last_assistant_message=self.notice())
        result = self.event('Stop', last_assistant_message=self.notice(), stop_hook_active=True)
        self.assertFalse(result['continue'])
        self.assertIn('未通過驗證', result['systemMessage'])
        self.assertEqual(self.event('Stop', last_assistant_message='更正：尚未保存記憶。', stop_hook_active=True), {})

    def test_existing_note_needs_current_turn_verification(self):
        self.apply({'job.md': note('job')})
        self.event('SessionStart')
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice())['decision'], 'block')
        before = runtime.revision(self.root)
        with runtime.opened(self.config):
            result = runtime.verify(self.root, ['job.md'])
        self.event('PostToolUse', tool_name='Bash', tool_response={'stdout': json.dumps(result)})
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice(), stop_hook_active=True), {})
        self.assertEqual(runtime.revision(self.root), before)
        self.assertFalse(result['changed'])

    def test_noop_apply_provides_evidence_without_rewriting_note(self):
        self.apply({'job.md': note('job')})
        self.event('SessionStart')
        timestamp = (self.root / 'job.md').stat().st_mtime_ns
        result = self.apply({'job.md': note('job')})
        self.assertFalse(result['changed'])
        self.assertEqual((self.root / 'job.md').stat().st_mtime_ns, timestamp)
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice()), {})

    def test_receipt_in_assistant_prose_is_not_tool_evidence(self):
        self.event('SessionStart')
        with runtime.opened(self.config):
            result = runtime.apply(self.root, {'expected_revision': runtime.revision(self.root),
                                   'updates': {'job.md': note('job')}, 'retire': []})
        reply = self.notice() + '\n' + json.dumps(result)
        self.assertEqual(self.event('Stop', last_assistant_message=reply)['decision'], 'block')

    def test_stale_receipt_from_previous_turn_is_rejected(self):
        result = self.apply({'job.md': note('job')})
        self.event('SessionStart')
        self.event('PostToolUse', tool_name='Bash', tool_response={'stdout': json.dumps(result)})
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice())['decision'], 'block')

    def test_failed_tool_output_does_not_supply_evidence(self):
        self.event('SessionStart')
        with runtime.opened(self.config):
            result = runtime.apply(self.root, {'expected_revision': runtime.revision(self.root),
                                   'updates': {'job.md': note('job')}, 'retire': []})
        self.event('PostToolUse', tool_name='Bash', tool_response={'stdout': json.dumps(result), 'exitCode': 1})
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice())['decision'], 'block')

    def test_failed_write_cannot_claim_success_on_a_clean_empty_index(self):
        self.event('SessionStart')
        with self.assertRaises(ValueError):
            self.apply({'job.md': 'invalid note'})
        self.assertFalse(runtime.context({'project': self.root})['project']['audit']['structural_errors'])
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice())['decision'], 'block')

    def test_receipt_invalidated_if_note_is_changed_or_deleted(self):
        for delete in (False, True):
            with self.subTest(delete=delete):
                self.event('UserPromptSubmit')
                self.apply({'job.md': note('job')})
                if delete:
                    (self.root / 'job.md').unlink()
                else:
                    (self.root / 'job.md').write_text(note('job', 'teacher'), encoding='utf-8')
                self.assertEqual(self.event('Stop', last_assistant_message=self.notice())['decision'], 'block')

    def test_receipt_for_other_note_does_not_validate_claim(self):
        self.apply({'job.md': note('job')})
        self.event('SessionStart')
        self.apply({'other.md': note('other', subject='user:bob')})
        reply = self.notice() + '\n已保存另一份記憶 [other](' + str(self.root / 'other.md') + ')'
        self.assertEqual(self.event('Stop', last_assistant_message=reply)['decision'], 'block')

    def test_receipt_for_different_root_cannot_be_reused(self):
        self.event('SessionStart')
        self.event('PostToolUse', tool_name='Bash', tool_response={'receipt': 'a' * 32, 'receipt_root': str(self.project)})
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice())['decision'], 'block')

    def test_index_only_receipt_does_not_prove_preference_saved(self):
        self.event('SessionStart')
        self.apply({})
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice(self.root / 'MEMORY.md'))['decision'], 'block')

    def test_genuine_index_repair_notice_is_allowed(self):
        self.event('SessionStart')
        self.apply({})
        reply = '已更新記憶索引 [索引](' + str(self.root / 'MEMORY.md') + ')。'
        self.assertEqual(self.event('Stop', last_assistant_message=reply), {})

    def test_multiple_saves_in_one_turn_can_share_one_notice(self):
        self.event('SessionStart')
        self.apply({'job.md': note('job')})
        self.apply({'other.md': note('other', subject='user:bob')})
        reply = self.notice() + '\n' + self.notice(self.root / 'other.md')
        self.assertEqual(self.event('Stop', last_assistant_message=reply), {})

    def test_corrupt_index_invalidates_receipt(self):
        self.event('SessionStart')
        self.apply({'job.md': note('job')})
        (self.root / 'MEMORY.md').write_text('# Empty index\n', encoding='utf-8')
        self.assertEqual(self.event('Stop', last_assistant_message=self.notice())['decision'], 'block')

    def test_global_false_success_and_verified_save(self):
        self.config.unlink()
        config = self.project / 'global.json'
        config.write_text('{"version":1,"mode":"global","home":"shared"}')
        base = {'session_id': 'global-proof', 'cwd': str(self.project)}
        hook.handle({**base, 'hook_event_name': 'SessionStart'}, config)
        response = hook.handle({**base, 'hook_event_name': 'Stop', 'last_assistant_message': '已記住。'}, config)
        self.assertEqual(response['decision'], 'block')
        with runtime.opened(config, self.project) as (_, _, roots):
            root = roots['global']
            saved = runtime.apply(root, {'expected_revision': runtime.revision(root), 'updates': {'job.md': note('job')}, 'retire': []}, 'global')
        hook.handle({**base, 'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'tool_response': {'stdout': json.dumps(saved)}}, config)
        self.assertEqual(hook.handle({**base, 'hook_event_name': 'Stop', 'last_assistant_message': self.notice(root / 'job.md')}, config), {})


if __name__ == '__main__':
    unittest.main()
