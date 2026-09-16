"""Behavioral regression tests; fixtures never touch a user's memory store."""
import contextlib
import io
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import jason_check as check
from tools import jason_doctor as doctor


def note(slug, body='A durable reference.', kind='reference'):
    return (f'---\nname: {slug}\ndescription: A useful memory\ntype: {kind}\n'
            f'created: 2026-09-01\nupdated: 2026-09-09\n---\n{body}\n')


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='jason-test-')
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith('JASON_')}
        self.env_patch = patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def write(self, filename, text):
        path = self.store / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
        return path

    def diagnose(self, index, notes=None, schema=True):
        self.write('MEMORY.md', index)
        for filename, text in (notes or {}).items():
            self.write(filename, text)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = doctor.main(['doctor', str(self.store)] + ([] if schema else ['--no-schema']))
        return code, output.getvalue()

    def test_empty_and_valid_store(self):
        self.assertEqual(self.diagnose('# Index\n')[0], 0)
        self.assertEqual(self.diagnose('- [A](a.md)', {'a.md': note('a')})[0], 0)

    def test_missing_pointer_all_extension_cases(self):
        for ext in ('md', 'MD', 'Md', 'mD'):
            with self.subTest(ext=ext):
                code, out = self.diagnose(f'- [Missing](missing.{ext})')
                self.assertEqual(code, 1)
                self.assertIn('missing file', out)

    def test_existing_uppercase_extension(self):
        self.assertEqual(self.diagnose('- [A](a.MD)', {'a.MD': note('a')})[0], 0)

    def test_filename_case_follows_filesystem(self):
        self.write('a.md', note('a'))
        exists = (self.store / 'A.md').is_file()
        code, _ = self.diagnose('- [A](A.md)')
        self.assertEqual(code, 0 if exists else 1)

    def test_isolated_cycle_and_chain_are_unreachable(self):
        notes = {'a.md': note('a', '[[b]]'), 'b.md': note('b', '[[a]] [[c]]'), 'c.md': note('c')}
        for schema in (True, False):
            with self.subTest(schema=schema):
                code, out = self.diagnose('# Index', notes, schema=schema)
                self.assertEqual(code, 1)
                self.assertEqual(out.count('ISSUE: orphan note'), 3)

    def test_indexed_cycle_and_transitive_chain_are_reachable(self):
        code, out = self.diagnose('- [A](a.md)', {
            'a.md': note('a', '[[b|B]]'), 'b.md': note('b', '[[a]] [[c#Heading]]'), 'c.md': note('c')})
        self.assertEqual(code, 0)
        self.assertIn('not in MEMORY.md', out)

    def test_inactive_index_examples_do_not_activate_notes(self):
        examples = [
            '<!-- - [A](a.md) -->', '<!--\n- [A](a.md)\n-->',
            '```markdown\n- [A](a.md)\n```', '~~~~md\n- [A](a.md)\n~~~~',
            '- `[A](a.md)`', '    - [A](a.md)', 'Example: [A](a.md)',
            '```md\n- [A](a.md)\n~~~\n- [A](a.md)',
        ]
        for index in examples:
            with self.subTest(index=index):
                code, out = self.diagnose(index, {'a.md': note('a')})
                self.assertEqual(code, 1)
                self.assertIn('orphan note', out)

    def test_active_pointer_after_comments_and_fences(self):
        code, _ = self.diagnose('```html\n<!--\n```\n<!-- hidden -->\n- [A](<a.md#section>)',
                               {'a.md': note('a')})
        self.assertEqual(code, 0)

    def test_incomplete_links_and_images_are_not_index_entries(self):
        for index in ('- [A](a.md', '- ![A](a.md)', r'- \[A](a.md)',
                      '- [A](<a.md)', '- [A](a.md>)', '- [A](a.md.bak)',
                      '- [A](a.md "unclosed)', '- [A](a.md#anchor'):
            with self.subTest(index=index):
                code, out = self.diagnose(index, {'a.md': note('a')})
                self.assertEqual(code, 1)
                self.assertIn('orphan note', out)

    def test_complete_links_accept_anchors_and_titles(self):
        for index in ('- [A](a.md#anchor)', '- [A](<a.md#anchor>)',
                      '- [A](a.md "Title")', "- [A](a.md 'Title')",
                      '- [A](<a.md> "Title")'):
            with self.subTest(index=index):
                self.assertEqual(self.diagnose(index, {'a.md': note('a')})[0], 0)

    def test_literal_comment_marker_does_not_hide_later_pointers(self):
        for example in ('Example `<!--`', '```example <!--\nignored\n```'):
            with self.subTest(example=example):
                code, _ = self.diagnose(example + '\n- [A](a.md)', {'a.md': note('a')})
                self.assertEqual(code, 0)

    def test_inactive_wikilinks_do_not_make_notes_reachable(self):
        for body in ('<!-- [[b]] -->', '```\n[[b]]\n```', '`[[b]]`'):
            with self.subTest(body=body):
                code, out = self.diagnose('- [A](a.md)', {'a.md': note('a', body), 'b.md': note('b')})
                self.assertEqual(code, 1)
                self.assertIn('unreachable from MEMORY.md): b.md', out)

    def test_frontmatter_wikilink_is_not_a_graph_edge(self):
        code, _ = self.diagnose('- [A](a.md)', {
            'a.md': note('a').replace('A useful memory', 'Related [[b]]'), 'b.md': note('b')})
        self.assertEqual(code, 1)

    def test_duplicate_pointer_is_info(self):
        code, out = self.diagnose('- [A](a.md)\n- [Again](./a.md)', {'a.md': note('a')})
        self.assertEqual(code, 0)
        self.assertIn('2 times', out)

    def test_archive_does_not_rescue_live_note(self):
        code, out = self.diagnose('- [Archive](archive/a.md)',
                                  {'archive/a.md': 'Archived', 'a.md': note('a')})
        self.assertEqual(code, 1)
        self.assertIn('orphan note', out)

    def test_paths_and_remote_urls(self):
        for target in ('../outside.md', 'file:///outside.md', 'wrong/a.md'):
            with self.subTest(target=target):
                self.assertEqual(self.diagnose(f'- [A]({target})')[0], 1)
        self.assertEqual(self.diagnose('- [Remote](https://example.com/a.md)')[0], 0)

    def test_symlink_escape_is_not_read(self):
        outside = self.store.parent / (self.store.name + '-outside.md')
        outside.write_text('SENSITIVE SENTINEL', encoding='utf-8')
        self.addCleanup(outside.unlink)
        try:
            (self.store / 'a.md').symlink_to(outside)
        except OSError:
            self.skipTest('Creating symlinks is not permitted on this host')
        code, out = self.diagnose('- [A](a.md)')
        self.assertEqual(code, 1)
        self.assertNotIn('SENSITIVE SENTINEL', out)

    def test_nonempty_labels_accept_inline_and_following_lines(self):
        bodies = [
            '**Why:** Verified.\n**How to apply:** Run `python test.py`.',
            'Why:\nEvidence here.\nHow to apply:\nUse this method.',
            '## Why：\n已驗證。\n## How to apply：\n適用於此情境。',
            '**Why**: Evidence.\n**How to apply**: `python test.py`',
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.diagnose('- [A](a.md)', {'a.md': note('a', body, 'feedback')})[0], 0)

    def test_empty_or_inactive_labels_fail(self):
        bodies = [
            '**Why:**\n**How to apply:**',
            '**Why:**\n**How to apply:** Do it.',
            '**Why:**\n## Unrelated heading\nUnrelated content.\n**How to apply:** Do it.',
            '<!--\nWhy: example\nHow to apply: example\n-->',
            '```\nWhy: example\nHow to apply: example\n```',
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.diagnose('- [A](a.md)', {'a.md': note('a', body, 'feedback')})[0], 1)

    def test_schema_errors_and_no_schema(self):
        for content in (note('a').replace('2026-09-01', '2026-99-99'),
                        note('a').replace('type: reference', 'type: reference\ntype: user'), 'No frontmatter'):
            with self.subTest(content=content):
                self.assertEqual(self.diagnose('- [A](a.md)', {'a.md': content})[0], 1)
                self.assertEqual(self.diagnose('- [A](a.md)', schema=False)[0], 0)

    def test_nested_metadata_and_bom_crlf(self):
        content = note('a').replace('type: reference', 'metadata:\n  type: reference')
        self.assertEqual(self.diagnose('- [A](a.md)', {'a.md': '\ufeff' + content.replace('\n', '\r\n')})[0], 0)

    def test_templates(self):
        self.assertEqual(self.diagnose((ROOT / 'templates/MEMORY.md').read_text(encoding='utf-8'))[0], 0)
        for filename, slug in [('example-feedback.md', 'verify-before-done'), ('example-project.md', 'api-gateway-v2-status')]:
            content = (ROOT / 'templates' / filename).read_text(encoding='utf-8')
            self.write(slug + '.md', content)
        self.assertEqual(self.diagnose('- [F](verify-before-done.md)\n- [P](api-gateway-v2-status.md)')[0], 0)

    def test_published_document_links_resolve(self):
        # Check published prose links, not paths in example memory snippets.
        for name in ('README.md', 'SKILL.md', 'CLAUDE.md', 'AGENTS.md'):
            text = doctor._active_markdown((ROOT / name).read_text(encoding='utf-8'))
            for target in re.findall(r'\]\(([^\s)]+)\)', text):
                path = target.split('#', 1)[0]
                if not path or '://' in path:
                    continue
                with self.subTest(document=name, target=target):
                    self.assertTrue((ROOT / path).is_file(), target)

    def test_installed_rules_resolve_framework_outside_project(self):
        # Framework sources need not sit at the consumer project's root.
        template = (ROOT / 'templates/standing-rules.md').read_text(encoding='utf-8')
        installed = template.replace('<MEMORY_ROOT>', '.jason-memory').replace('<FRAMEWORK_ROOT>', ROOT.as_posix())
        for name in ('AGENTS.md', 'CLAUDE.md'):
            self.write(name, installed)
        self.write('.jason-memory/MEMORY.md', (ROOT / 'templates/MEMORY.md').read_text(encoding='utf-8'))
        for target in re.findall(r'`([^`]+)`', installed):
            if target.endswith(('/SKILL.md', '/tools/jason_check.py', '/tools/jason_doctor.py', '/MEMORY.md')):
                with self.subTest(target=target):
                    self.assertTrue((self.store / target).is_file(), target)
        # Exercise the quoted commands exactly as installed, from a different
        # consumer directory. Framework and memory paths need not share a root.
        commands = re.findall(r'^python "([^"]+)" "([^"]+)"$', installed, re.M)
        self.assertEqual(len(commands), 2)
        for script, argument in commands:
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, script, argument], cwd=self.store,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def measure(self, text):
        path = self.write('MEMORY.md', text)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = check.main(['check', str(path)])
        return code, output.getvalue()

    def test_no_default_size_budget(self):
        for text in ('x\n' * 201, 'x\n' * 1000, 'x' * 25601, '記' * 10000):
            with self.subTest(size=len(text)):
                code, out = self.measure(text)
                self.assertEqual(code, 0)
                self.assertIn('no size budget configured', out)
                self.assertEqual(self.diagnose(text)[0], 0)

    def test_optional_budgets_are_independent(self):
        scenarios = [
            ({'JASON_WARN': '2'}, 'x\n' * 2, 0, 0),
            ({'JASON_WARN': '2'}, 'x\n' * 3, 1, 0),
            ({'JASON_HARD': '2'}, 'x\n' * 3, 2, 1),
            ({'JASON_HARD': '3'}, 'x\n' * 3, 0, 0),
            ({'JASON_WARN_BYTES': '5'}, '記記', 1, 0),
            ({'JASON_HARD_BYTES': '5'}, '記記', 2, 1),
            ({'JASON_HARD_BYTES': '6'}, '記記', 0, 0),
            ({'JASON_WARN': '5', 'JASON_HARD_BYTES': '3'}, 'xxxx', 2, 1),
        ]
        for env, text, expected_check, expected_doctor in scenarios:
            with self.subTest(env=env), patch.dict(os.environ, env):
                self.assertEqual(self.measure(text)[0], expected_check)
                self.assertEqual(self.diagnose(text)[0], expected_doctor)

    def test_invalid_or_disabled_budgets(self):
        for value in ('', 'bad', '0', '-1'):
            env = {key: value for key in ('JASON_WARN', 'JASON_WARN_BYTES', 'JASON_HARD', 'JASON_HARD_BYTES')}
            with self.subTest(value=value), patch.dict(os.environ, env):
                self.assertEqual(self.measure('x\n' * 1000)[0], 0)
                self.assertEqual(self.diagnose('x\n' * 1000)[0], 0)

    def test_streaming_counts_across_chunk_boundaries(self):
        for text, lines in [('', 0), ('a\n', 1), ('a\r\nb', 2),
                            ('x' * 65535 + '\n終', 2), ('x\n' * 65536, 65536)]:
            with self.subTest(size=len(text)):
                code, out = self.measure(text)
                self.assertEqual(code, 0)
                self.assertIn(f'{lines} lines / {len(text.encode("utf-8"))} bytes', out)

    def test_check_error_codes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check.main(['check']), 64)
            self.assertEqual(check.main(['check', 'a', 'b']), 64)
            self.assertEqual(check.main(['check', str(self.store / 'missing')]), 66)

    def test_doctor_parser_safety_limit_is_separate(self):
        code, out = self.diagnose('x' * (doctor.INDEX_READ_CAP + 1))
        self.assertEqual(code, 1)
        self.assertIn('parser safety limit', out)
        self.assertNotIn('clean', out)
        self.assertEqual(self.diagnose('x' * doctor.INDEX_READ_CAP)[0], 0)

    def test_bounded_read_handles_growth_after_stat(self):
        path = self.write('growing.md', 'x' * 9)
        with patch.object(doctor.os.path, 'getsize', return_value=0):
            self.assertIs(doctor._read_bytes(path, cap=8), doctor._TOO_LARGE)

    def test_unreadable_subtree_is_not_reported_clean(self):
        # Permission bits are unreliable on Windows; reproduce os.walk's error
        # callback contract without requiring administrative permissions.
        def unreadable_walk(root, onerror=None):
            yield str(self.store), ['private'], ['MEMORY.md']
            if onerror:
                onerror(PermissionError(13, 'Permission denied', str(self.store / 'private')))

        with patch.object(doctor.os, 'walk', unreadable_walk):
            code, out = self.diagnose('# Index\n')
        self.assertEqual(code, 1)
        self.assertIn('cannot scan directory', out)
        self.assertNotIn('no broken pointers', out)

    def test_invalid_utf8_is_not_reported_clean(self):
        for target in ('MEMORY.md', 'a.md'):
            for schema in (True, False):
                with self.subTest(target=target, schema=schema):
                    self.write('MEMORY.md', '- [A](a.md)\n')
                    self.write('a.md', note('a'))
                    path = self.store / target
                    with path.open('ab') as stream:
                        stream.write(b'\n\xff')
                    output = io.StringIO()
                    args = ['doctor', str(self.store)] + ([] if schema else ['--no-schema'])
                    with contextlib.redirect_stdout(output):
                        code = doctor.main(args)
                    self.assertEqual(code, 1)
                    self.assertIn('UTF-8', output.getvalue())

    def test_cli_entrypoints(self):
        self.write('MEMORY.md', '# Index\n' * 250)
        for script, arg in [('jason_check.py', self.store / 'MEMORY.md'), ('jason_doctor.py', self.store)]:
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(ROOT / 'tools' / script), str(arg)],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
