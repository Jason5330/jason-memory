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
            return runtime.apply(self.root, {'expected_revision': revision or runtime.revision(self.root),
                                            'updates': updates, 'retire': retire or []})

    def event(self, name, **kwargs):
        return hook.handle({'hook_event_name': name, 'session_id': 'test', 'cwd': str(self.project), **kwargs}, self.config)

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
        self.assertEqual(self.event('Stop', last_assistant_message='完成', stop_hook_active=True), {})
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


if __name__ == '__main__':
    unittest.main()
