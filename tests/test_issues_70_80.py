"""Regression tests for issues #70-#80: self-healing stores, diagnosable background work, snapshot
disclosure, duplicate facts, prose-form credentials and links inside the downloads."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import memory_runtime as runtime  # noqa: E402
import memory_ledger as ledger  # noqa: E402
import memory_dream as dream  # noqa: E402
import memory_facts  # noqa: E402
import claude_memory_hook as hook  # noqa: E402


def note(slug, predicate='color', value='blue', kind='user'):
    facts = [{'subject': 'user', 'predicate': predicate, 'value': value}]
    return ('---\nname: ' + slug + '\ndescription: QA ' + slug + '\ntype: ' + kind + '\n'
            'created: 2026-11-20\nupdated: 2026-11-20\n---\n\nQA value ' + value + '\n\n'
            'Why: QA\n\nHow to apply: QA\n\n```jason-facts\n' + json.dumps(facts) + '\n```\n')


class Base(unittest.TestCase):
    ledger_cfg = None

    def setUp(self):
        self.spawned, original = [], ledger._spawn
        ledger._spawn = self.spawned.append
        self.addCleanup(setattr, ledger, '_spawn', original)
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

    def snapshot(self):
        return hook.handle({'hook_event_name': 'SessionStart', 'session_id': 'qa', 'cwd': str(self.project)},
                           self.config)['hookSpecificOutput']['additionalContext']

    def remember(self, key, value, kind='feedback'):
        with runtime.opened(self.config):
            return runtime.remember(self.root, 'user', key, value, '使用者要求', kind=kind)

    def apply(self, updates):
        with runtime.opened(self.config):
            return runtime.apply(self.root, {'expected_revision': runtime.revision(self.root),
                                             'updates': updates, 'retire': []})


class StoreRepairTests(Base):
    def test_missing_index_is_rebuilt_from_notes_not_left_blank(self):
        """#78"""
        self.remember('qa.constraint', 'QA_PROJECT_CONSTRAINT_78', kind='project')
        self.remember('qa.link', 'https://example.com/QA_REFERENCE_78', kind='reference')
        (self.root / 'MEMORY.md').unlink()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), runtime.opened(self.config):
            index = runtime.read_index(self.root)
            self.assertEqual(index, runtime.index_for(runtime.notes(self.root)))
        self.assertIn('rebuilt it from 2 note(s)', stderr.getvalue())
        snap = self.snapshot()
        for name in runtime.notes(self.root):
            self.assertIn(name, snap)
        self.assertNotIn('CONFLICT:', snap)

    def test_structure_problems_are_not_reported_as_value_conflicts(self):
        """#78: a broken index is a store check, not a memory conflict."""
        self.remember('qa.pref', 'QA_PREF')
        (self.root / 'MEMORY.md').write_text('# Index\n- [gone](missing.md)\n', encoding='utf-8')
        snap = self.snapshot()
        self.assertIn('STORE CHECK', snap)
        self.assertNotIn('CONFLICT:', snap)

    def test_oversized_note_is_quarantined_and_the_store_keeps_working(self):
        """#77"""
        self.remember('qa.good', 'QA_GOOD_77')
        big = self.root / 'feedback/preference-qa-oversized.md'
        payload = ('---\nname: preference-qa-oversized\ndescription: "big"\ntype: feedback\ncreated: 2026-11-20\n'
                   'updated: 2026-11-20\n---\n\n' + 'x' * (4 * 1024 * 1024 + 500) + '\n').encode('utf-8')
        big.write_bytes(payload)
        with contextlib.redirect_stderr(io.StringIO()), runtime.opened(self.config) as (_, _, roots):
            context = runtime.context(roots)['project']
            self.assertIn('QA_GOOD_77', json.dumps(context, ensure_ascii=False))
        self.assertFalse(big.exists())
        kept = list((self.root / 'archive/quarantine').rglob('preference-qa-oversized.md'))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), payload)
        self.assertIn('QA_GOOD_77', self.snapshot())
        self.remember('qa.after', 'QA_AFTER_77')  # Writes work again.

    def test_broken_journal_names_the_file_and_doctor_reports_it(self):
        """#79"""
        self.remember('qa.ok', 'QA_OK_79')
        journal = self.root / '.transaction.json'
        for content in ('{"id": "zzz"}', '{"id": ', ''):
            with self.subTest(journal=content):
                journal.write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError) as caught:
                    with runtime.opened(self.config):
                        pass
                self.assertIn(str(journal), str(caught.exception))
                self.assertIn('move it out of the memory folder', str(caught.exception))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = runtime.storelib.doctor.main(['doctor', str(self.root)])
                self.assertEqual(code, 1)
                self.assertIn('unfinished save journal', output.getvalue())
        journal.unlink()
        self.remember('qa.again', 'QA_AGAIN_79')


class SnapshotTests(Base):
    def test_truncated_index_is_disclosed_with_counts(self):
        """#72"""
        for i in range(30):
            self.remember('qa.rule' + str(i), 'QA rule ' + str(i) + ' ' + 'long description ' * 8,
                          kind='reference' if i % 2 else 'project')
        with runtime.opened(self.config):
            total = sum(1 for line in runtime.read_index(self.root).splitlines() if line.startswith('- ['))
        snap = self.snapshot()
        found = re.search(r'INDEX TRUNCATED: (\d+) of (\d+) project index rows not shown', snap)
        self.assertIsNotNone(found, snap[-1500:])
        shown = sum(1 for line in snap.splitlines() if line.startswith('- ['))
        self.assertEqual((int(found.group(2)), shown + int(found.group(1))), (total, total))


class DuplicateFactTests(Base):
    def test_correction_names_every_note_holding_the_fact(self):
        """#76"""
        self.apply({'user/dup-one.md': note('dup-one'), 'user/dup-two.md': note('dup-two')})
        with runtime.opened(self.config) as (_, _, roots):
            audit = runtime.context(roots)['project']['audit']
        self.assertEqual(audit['duplicates'][0]['paths'], ['user/dup-one.md', 'user/dup-two.md'])
        self.assertIn('DUPLICATE FACTS', self.snapshot())
        with runtime.opened(self.config):
            with self.assertRaises(ValueError) as caught:
                runtime.remember(self.root, 'user', 'color', 'green', '使用者更正')
        self.assertIn('user/dup-one.md, user/dup-two.md', str(caught.exception))
        # Following the message - both notes in one plan - succeeds.
        self.apply({'user/dup-one.md': note('dup-one', value='green'), 'user/dup-two.md': note('dup-two', value='green')})


class BackgroundWorkTests(Base):
    ledger_cfg = {'enabled': True, 'foreground_writes': True, 'dream_every': 1}

    def append(self, text='QA'):
        with runtime.opened(self.config):
            ledger.append(self.root, 'prompt', {'text': text})
            return len(ledger.records(self.root))

    def state(self):
        with runtime.opened(self.config):
            return ledger.dream_state(self.root)

    def test_reservation_without_pid_expires_after_the_start_grace(self):
        """#80"""
        head = self.append()
        with runtime.opened(self.config):
            self.assertIsNotNone(ledger.claim_job(self.root, 'dream', head))  # Starter dies before spawning.
            opts = ledger.settings(self.config)
            self.assertIsNone(ledger.maybe_dream(self.root, self.config, self.project, opts))
            state = ledger.dream_state(self.root)
            state['running']['started'] -= ledger.STARTING_GRACE + 1
            ledger._save_state(self.root, state)
            self.assertEqual(ledger.maybe_dream(self.root, self.config, self.project, opts), head)

    def test_worker_records_its_own_pid(self):
        """#80: the slot is tied to a live process as soon as the worker starts."""
        head = self.append()
        seen = []

        def command_sees_pid(*args, **kwargs):
            with runtime.opened(self.config):
                seen.append(ledger.dream_state(self.root)['running']['pid'])
            return 0
        with runtime.opened(self.config):
            owner = ledger.claim_job(self.root, 'dream', head)
        opts = dict(ledger.settings(self.config), dream_command=['qa'])
        with mock.patch.object(ledger.subprocess, 'call', command_sees_pid):
            self.assertEqual(ledger.dream_run(self.config, self.project, head, opts, owner), 0)
        self.assertEqual(seen, [os.getpid()])

    def test_failure_keeps_the_worker_output(self):
        """#73"""
        head = self.append()
        opts = dict(ledger.settings(self.config), dream_command=[
            sys.executable, '-c', 'import sys; print("QA_DIAG_73 agent said why"); sys.exit(3)'])
        self.assertEqual(ledger.dream_run(self.config, self.project, head, opts), 3)
        error = self.state()['last_error']
        self.assertEqual(error['code'], 3)
        self.assertIn('QA_DIAG_73 agent said why', error['error'])
        self.assertIn('QA_DIAG_73', (self.root / '.raw/dream.log').read_text(encoding='utf-8'))

    def test_worker_output_in_last_error_is_redacted(self):
        head = self.append()
        opts = dict(ledger.settings(self.config), dream_command=[
            sys.executable, '-c', 'import sys; print("password=QA_SECRET_73x9"); sys.exit(4)'])
        ledger.dream_run(self.config, self.project, head, opts)
        self.assertNotIn('QA_SECRET_73x9', self.state()['last_error']['error'])

    def test_agent_timeout_is_named_and_not_retried_on_every_event(self):
        """#73"""
        cfg = json.loads(self.config.read_text(encoding='utf-8'))
        cfg['ledger'].update(dream_timeout=1, dream_agent=[sys.executable, '-c', 'import time; time.sleep(30)'])
        self.config.write_text(json.dumps(cfg), encoding='utf-8')
        head = self.append()
        opts = ledger.settings(self.config)
        started = time.time()
        self.assertEqual(ledger.dream_run(self.config, self.project, head, opts), 124)
        self.assertLess(time.time() - started, 25)
        error = self.state()['last_error']
        self.assertEqual(error['code'], 124)
        self.assertIn('timed out after 1 s', error['error'])
        head = self.append('more')
        with runtime.opened(self.config):
            self.assertIsNone(ledger.maybe_dream(self.root, self.config, self.project, opts))  # Cooling down.
            state = ledger.dream_state(self.root)
            state['last_error']['at'] -= ledger.TIMEOUT_COOLDOWN + 1
            ledger._save_state(self.root, state)
            self.assertEqual(ledger.maybe_dream(self.root, self.config, self.project, opts), head)

    def test_timeout_fires_even_when_the_agent_never_reads_its_prompt(self):
        """#73: subprocess.run writes stdin synchronously on Windows; a large prompt to an agent that
        stopped reading (and a grandchild holding the pipe) must not outlive the timeout."""
        agent = [sys.executable, '-c', 'import subprocess, sys, time; '
                 'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(40)"]); time.sleep(40)']
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            dream._run_agent(agent, 'x' * 300000, dict(os.environ), self.project, timeout=2)
        self.assertLess(time.time() - started, 15)

    def test_invalid_timeout_setting_is_rejected(self):
        cfg = json.loads(self.config.read_text(encoding='utf-8'))
        for bad in (0, -5, '60', True):
            with self.subTest(timeout=bad):
                cfg['ledger']['dream_timeout'] = bad
                self.config.write_text(json.dumps(cfg), encoding='utf-8')
                with self.assertRaises(ValueError):
                    ledger.settings(self.config)


class AgentDiscoveryTests(unittest.TestCase):
    """#71"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()

    def isolated(self):
        return contextlib.ExitStack()

    def locate_with(self, *paths):
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'')
        with mock.patch.object(dream.shutil, 'which', return_value=None), \
                mock.patch.object(dream.Path, 'home', return_value=self.home), \
                mock.patch.dict(os.environ, {'APPDATA': str(self.home / 'appdata')}):
            return dream.agent_command({'dream_agent': 'auto'})

    def test_vscode_extension_claude_is_used_when_not_on_path(self):
        exe = '.exe' if os.name == 'nt' else ''
        old = self.home / '.vscode/extensions/anthropic.claude-code-1.0.0-win32-x64/resources/native-binary' / ('claude' + exe)
        new = self.home / '.vscode/extensions/anthropic.claude-code-2.0.0-win32-x64/resources/native-binary' / ('claude' + exe)
        old.parent.mkdir(parents=True)
        old.write_bytes(b'')
        os.utime(old, (1, 1))
        os.utime(old.parents[2], (1, 1))
        command = self.locate_with(new)
        self.assertEqual(Path(command[0]), new)
        self.assertEqual(command[1], '-p')

    def test_nothing_found_explains_that_nothing_is_lost(self):
        self.assertIsNone(self.locate_with())
        self.assertIn('nothing is lost', dream.NO_AGENT)
        self.assertIn('ledger.dream_agent', dream.NO_AGENT)

    def test_global_install_warns_when_no_agent_cli_exists(self):
        profile = self.home / 'profile'
        (profile / '.codex').mkdir(parents=True)
        env = dict(os.environ, PATH=str(Path(sys.executable).parent), HOME=str(self.home / 'nohome'),
                   USERPROFILE=str(self.home / 'nohome'), APPDATA=str(self.home / 'appdata'), PYTHONUTF8='1')
        result = subprocess.run([sys.executable, str(ROOT / 'global/install.py'), '--user-home', str(profile),
                                 '--home', str(self.home / 'mem'), '--dry-run'], capture_output=True, text=True,
                                encoding='utf-8', env=env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('no agent CLI found' in w for w in json.loads(result.stdout)['warnings']), result.stdout)


class ProseCredentialTests(unittest.TestCase):
    """#74: `password is 0427` is a credential; `password is required` is not."""
    CATCH = [('password is 0427', '0427'), ('the password is hunter2', 'hunter2'), ('password was 0427', '0427'),
             ('密碼是 1234', '1234'), ('token is 0427', '0427'), ('api key is 0427', '0427'),
             ('my password is 0427.', '0427'), ('Password is now QAxx7781', 'QAxx7781'),
             ('the token was set to qa1234abcd', 'qa1234abcd'), ('密碼為 5566', '5566'), ('密碼改成 qa778899', 'qa778899'),
             ('access token is ya29qa123', 'ya29qa123'), ('password is "qa 99 secret"', 'qa 99 secret'),
             ('令牌是 abc123', 'abc123'), ('PASSWORD IS 0427', '0427'), ('password is 0427 and token is 9911', '9911')]
    KEEP = ['password is required', 'token is expired', '密碼是必填', 'password is 8 characters',
            'password is 12 chars long', 'max token is 4096', 'the token is valid for 30 days',
            'password is at least 12', '密碼是 8 碼', 'token is 4096 tokens', 'password is wrong',
            'the token is stored in 1Password', '密碼是使用者自己保管', 'api key is set in OPENAI_API_KEY',
            'maximum token is 8000', 'token limit is 8000', 'password length is 12', 'token count is 3',
            'session token is null', 'version is 2', '密碼長度是 12 位', 'token 上限是 4096',
            'password is changed every 90 days', '密碼是 12 位以上', 'access token is ${GITHUB_TOKEN}']

    def test_prose_credentials_are_masked_and_refused(self):
        for text, secret in self.CATCH:
            with self.subTest(text=text):
                self.assertTrue(memory_facts.note_has_credential(text))
                self.assertNotIn(secret, memory_facts.redact(text))

    def test_prose_about_credentials_is_kept(self):
        for text in self.KEEP:
            with self.subTest(text=text):
                self.assertFalse(memory_facts.note_has_credential(text))
                self.assertEqual(memory_facts.redact(text), text)


class PackagedLinkTests(unittest.TestCase):
    """#70: a relative link inside a download must point to a file inside that download."""

    def check(self, builder):
        spec = importlib.util.spec_from_file_location('qa_builder_' + builder.stem, builder)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            with ZipFile(module.build(Path(tmp) / 'p.zip')) as archive:
                names = archive.namelist()
                prefix = names[0].split('/')[0] + '/'
                files = {n[len(prefix):] for n in names}
                broken = []
                for name in names:
                    if not name.endswith('.md'):
                        continue
                    text = archive.read(name).decode('utf-8-sig')
                    text = re.sub(r'```.*?```', '', text, flags=re.S)
                    text = re.sub(r'`[^`\n]*`', '', text)
                    for target in re.findall(r'\]\(([^\s)]+)\)', text):
                        path = target.split('#', 1)[0]
                        if not path or '://' in path or not path.endswith(('.md', '.json', '.py', '.cmd', '.bat')):
                            continue
                        parts = []
                        for part in (PurePosixPath(name[len(prefix):]).parent / path).parts:
                            if part == '..':
                                parts.pop()
                            elif part != '.':
                                parts.append(part)
                        if '/'.join(parts) not in files:
                            broken.append((name, target))
        self.assertEqual(broken, [])

    def test_project_zip_links_resolve(self):
        self.check(ROOT / 'tools/build_download.py')

    def test_global_zip_links_resolve(self):
        self.check(ROOT / 'tools/build_global_download.py')


if __name__ == '__main__':
    unittest.main()
