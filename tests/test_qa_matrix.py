"""Adversarial acceptance matrix on the packaged global ZIP.

Each test states one invariant and probes it across input variants and every entry point, instead
of re-running a single reported repro. Bug classes: cited sources (#33-#36, #42, #52), credential
redaction (#38, #43, #50), ledger integrity (#39, #40, #46, #51) and global rebuild publishing (#49).
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / 'tools/build_global_download.py'


def tree_bytes(folder):
    """Snapshot of every active and archived note plus index under a memory folder."""
    folder = Path(folder)
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob('*'))
            if p.is_file() and '.hook-state' not in p.parts and '.raw' not in p.parts}


@unittest.skipUnless(BUILDER.is_file(), 'source checkout only')
class GlobalQAMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = cls.base = Path(cls.tmp.name).resolve()
        spec = importlib.util.spec_from_file_location('qa_matrix_build', BUILDER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with ZipFile(module.build(base / 'g.zip')) as archive:
            archive.extractall(base / 'unpacked')
        package = base / 'unpacked/jason-memory-global'
        profile = base / 'profile'
        (profile / '.codex').mkdir(parents=True)
        (profile / '.codex/config.toml').write_bytes(b'model = "x"\n')
        cls.home = base / 'qa home'
        cls.env = dict(os.environ, PYTHONUTF8='1')
        cls.env.pop('JASON_DREAM', None)
        subprocess.run([sys.executable, str(package / 'global/install.py'), '--user-home', str(profile),
                        '--home', str(cls.home)], check=True, capture_output=True, env=cls.env, timeout=120)
        tools = cls.home / 'framework/tools'
        cls.runtime, cls.ledger, cls.dream = (tools / n for n in ('memory_runtime.py', 'memory_ledger.py', 'memory_dream.py'))
        cls.hook, cls.notify = tools / 'claude_memory_hook.py', tools / 'codex_notify.py'
        cls.config = cls.home / 'framework/memory-config.json'
        cfg = json.loads(cls.config.read_text(encoding='utf-8'))
        cfg['ledger']['dream_every'] = 100000  # Never start a real background agent from the matrix.
        cls.config.write_text(json.dumps(cfg), encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # --- helpers ---------------------------------------------------------------------------
    def run_py(self, script, *args, stdin=None, dream=False, extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        if dream:
            env['JASON_DREAM'] = '1'
        return subprocess.run([sys.executable, str(script), *map(str, args)], input=stdin, capture_output=True,
                              text=True, encoding='utf-8', env=env, timeout=120)

    def project(self, name):
        folder = self.base / 'projects' / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def store_id(self, project):
        return hashlib.sha256(os.path.normcase(str(Path(project).resolve())).encode('utf-8')).hexdigest()[:16]

    def store(self, project):
        return self.home / 'memory/projects' / hashlib.sha256(
            os.path.normcase(str(Path(project).resolve())).encode('utf-8')).hexdigest()

    def append(self, project, kind, data, session='qa'):
        result = self.run_py(self.ledger, '--config', self.config, '--project', project, 'append',
                             '--kind', kind, '--data', json.dumps(data, ensure_ascii=False), '--session', session)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        return self.store_id(project) + ':' + str(record['seq']) + ':' + record['hash'][:12]

    def remember(self, project, key, why, scope='global', value=None):
        return self.run_py(self.runtime, '--config', self.config, '--project', project, 'remember', '--scope', scope,
                           '--subject', 'qa', '--key', key, '--value', value or ('VALUE_' + key), '--why', why,
                           dream=True)

    def revision(self, project, scope):
        out = self.run_py(self.runtime, '--config', self.config, '--project', project, 'context')
        return json.loads(out.stdout)['stores'][scope]['revision']

    def apply(self, project, scope, updates, retire=()):
        plan = {'expected_revision': self.revision(project, scope), 'updates': updates, 'retire': list(retire)}
        return self.run_py(self.runtime, '--config', self.config, '--project', project, 'apply', '--scope', scope,
                           '--plan', '-', stdin=json.dumps(plan, ensure_ascii=False), dream=True)

    def note_name(self, result, root):
        path = Path(json.loads(result.stdout)['paths'][0])
        return path.relative_to(root).as_posix()

    # --- invariant 1: a published note never cites a source that `source --ref` rejects -------
    def test_every_cited_source_must_resolve_on_every_write_path(self):
        a = self.project('cite')
        good = self.append(a, 'prompt', {'text': '日期一律用 ISO 8601'})
        store, seq, digest = good.split(':')
        bad = {
            'hash 5': store + ':' + seq + ':' + digest[:5],
            'hash 11': store + ':' + seq + ':' + digest[:11],
            'hash 13': good + 'f',
            'hash non-hex': good[:-1] + 'g',
            'hash wrong': good[:-1] + ('0' if digest[-1] != '0' else '1'),
            'hash upper': store + ':' + seq + ':' + digest.upper(),
            'seq missing': store + ':99:' + digest,
            'seq zero': store + ':0:' + digest,
            'store short': store[:15] + ':' + seq + ':' + digest,
            'store long': store + '0:' + seq + ':' + digest,
            'store upper': store.upper() + ':' + seq + ':' + digest,
            'store non-hex': 'zz' * 8 + ':' + seq + ':' + digest,
            'store local': 'local:' + seq + ':' + digest,
        }
        contexts = {'plain': '來源：ledger {}', 'english': 'Source: ledger {}', 'inline code': '來源：ledger `{}`',
                    'full stop': '來源：ledger {}。', 'parenthesis': '（來源：ledger {}）', 'no ledger word': '來源：{}'}
        shared = self.home / 'memory/shared'
        # Positive controls: a valid reference is accepted in every context.
        for label, template in contexts.items():
            with self.subTest(control=label):
                result = self.remember(a, 'ok-' + str(abs(hash(label))), template.format(good))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        number = 0
        for variant, ref in bad.items():
            for label, template in contexts.items():
                if label == 'no ledger word' and variant == 'store non-hex':
                    continue  # Without the word `ledger`, `zzzz…:1:…` is not presented as a ledger source.
                number += 1
                with self.subTest(entry='remember new', variant=variant, context=label):
                    before = tree_bytes(shared)
                    result = self.remember(a, 'bad-' + str(number), template.format(ref))
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertEqual(tree_bytes(shared), before)
        # The other write paths, with the plain context.
        legacy = self.remember(a, 'legacy', '來源：ledger seq 1')
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        cited = self.remember(a, 'cited', '來源：ledger ' + good)
        name = self.note_name(cited, shared)
        local_saved = self.remember(a, 'local-cited', '來源：ledger ' + good, scope='project')
        self.assertEqual(local_saved.returncode, 0, local_saved.stderr)
        project_root = self.store(a)
        local_name = self.note_name(local_saved, project_root)
        for variant, ref in bad.items():
            with self.subTest(entry='same-value upgrade', variant=variant):
                before = tree_bytes(shared)
                result = self.remember(a, 'legacy', '來源：ledger ' + ref)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(tree_bytes(shared), before)
            with self.subTest(entry='apply global', variant=variant):
                before = tree_bytes(shared)
                text = (shared / name).read_text(encoding='utf-8').replace(good, ref)
                result = self.apply(a, 'global', {name: text})
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(tree_bytes(shared), before)
            with self.subTest(entry='apply project', variant=variant):
                before = tree_bytes(project_root)
                text = (project_root / local_name).read_text(encoding='utf-8').replace(good, ref)
                result = self.apply(a, 'project', {local_name: text})
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(tree_bytes(project_root), before)
        # Literal format examples stay allowed in fenced code and comments (#42).
        example = (shared / name).read_text(encoding='utf-8').replace(
            '\n\nWhy: ', '\n\n```text\n來源：ledger ' + bad['hash 5'] + '\n```\n<!-- ledger ' + bad['hash 13'] +
            ' -->\n\nWhy: ', 1)
        result = self.apply(a, 'global', {name: example})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # --- invariant 2: no credential value reaches the ledger, replay, source or dream input -----
    def test_no_credential_value_survives_in_any_raw_view(self):
        b = self.project('redact')
        letters = 'qaopaquefixturelettersonlyvalue'
        basic = base64.b64encode(b'qa-demo:synthetic-unused-password').decode()
        cases = {
            'basic header': ('Authorization: Basic {}', basic),
            'bearer header lower': ('authorization: bearer {}', letters + 'a'),
            'token scheme': ('Authorization: Token {}', letters + 'b'),
            'proxy header': ('Proxy-Authorization: Basic {}', base64.b64encode(b'qa:proxy-pass').decode()),
            'curl -H': ('curl -H "Authorization: Bearer {}" https://example.invalid', letters + 'c'),
            'json header string': ('{{"Authorization": "Bearer {}"}}', letters + 'd'),
            'bare bearer': ('used Bearer {} for the call', letters + 'e'),
            'cookie': ('Cookie: session={}; theme=dark', letters + 'f'),
            'set-cookie': ('Set-Cookie: sid={}; Path=/; HttpOnly', letters + 'g'),
            'url userinfo': ('git clone https://qa-user:{}@example.invalid/repo.git', letters + 'h'),
            'query token': ('GET https://example.invalid/cb?access_token={}&state=1', letters + 'i'),
            'api_key assign': ('api_key={}', letters + 'j'),
            'quoted env': ('API_KEY="{}"', letters + 'k'),
            'password flag': ('mysql --password {} -u qa', letters + 'l'),
            'password colon': ('password: {}', letters + 'm'),
            'export token': ('export GITHUB_TOKEN={}', letters + 'n'),
            'client secret': ('client_secret={}', letters + 'o'),
            'x-api-key header': ('X-Api-Key: {}', letters + 'p'),
            'private key': ('-----BEGIN RSA PRIVATE KEY-----\n{}\n-----END RSA PRIVATE KEY-----', 'MIIEowIBAAKCAQEA' + letters),
        }
        stdout = 'QA_METRIC=17\n' + '\n'.join(t.format(v) for t, v in cases.values())
        # Ordinary output next to a credential must survive (found in a real Claude Code run).
        stdout += "\necho 'Authorization: Bearer " + letters + "t' && echo QA_KEEP_MARKER_8842\ntoken count was 5, file data_2024.csv"
        structured = {'headers': {'Authorization': 'Bearer ' + letters + 'q', 'X-Request-Id': 'QA_REQ_42'},
                      'env': {'AWS_SECRET_ACCESS_KEY': letters + 'r', 'PATH': '/usr/bin'},
                      'password': 12345678, 'usage': {'input_tokens': 12}}
        ref = self.append(b, 'tool', {'tool': 'Bash', 'input': {'command': 'qa'}, 'result': {'stdout': stdout, **structured}})
        prompt_ref = self.append(b, 'prompt', {'text': '我的 token 是 Bearer ' + letters + 's 請幫我呼叫'})
        secrets = [v for _, v in cases.values()] + [letters + c for c in 'qrs'] + ['12345678']
        views = {
            'ledger file': (self.store(b) / '.raw/ledger.jsonl').read_text(encoding='utf-8'),
            'replay': self.run_py(self.ledger, '--config', self.config, '--project', b, 'replay').stdout,
            'source': self.run_py(self.ledger, '--config', self.config, '--project', b, 'source', '--ref', ref).stdout,
            'source prompt': self.run_py(self.ledger, '--config', self.config, '--project', b, 'source', '--ref', prompt_ref).stdout,
            'dream prompt': self.run_py(self.dream, '--config', self.config, '--project', b, 'prompt').stdout,
        }
        for view, text in views.items():
            self.assertIn('QA_METRIC=17', text if view != 'source prompt' else 'QA_METRIC=17', view)
            for label, secret in zip(list(cases) + ['struct header', 'struct env', 'prompt bearer', 'struct number'], secrets):
                with self.subTest(view=view, case=label):
                    self.assertNotIn(secret, text)
        replay = views['replay']
        self.assertIn('QA_REQ_42', replay)
        self.assertIn('QA_KEEP_MARKER_8842', replay)
        self.assertIn('data_2024.csv', replay)
        self.assertNotIn(letters + 't', replay)
        self.assertIn('"input_tokens": 12', replay)

    # --- invariant 3: nothing is accepted or reported as fine on a ledger that fails verify ----
    def test_corrupt_ledger_is_never_extended_or_reported_as_valid(self):
        def corruptions(lines):
            yield 'edited data', [lines[0].replace(b'QA-ORIGINAL-1', b'QA-MODIFIED-1')] + lines[1:]
            yield 'deleted middle', lines[:1] + lines[2:]
            yield 'swapped', [lines[1], lines[0]] + lines[2:]
            yield 'torn last line', lines[:-1] + [lines[-1][:len(lines[-1]) // 2]]
            yield 'garbage line', lines + [b'not json\n']
            yield 'non-object line', lines + [b'[1, 2]\n']
        template = None
        for label, broken in corruptions(self.seed_lines()):
            folder = self.project('broken-' + label.replace(' ', '-'))
            ledger_file = self.store(folder) / '.raw/ledger.jsonl'
            if template is None:
                template = self.append(folder, 'prompt', {'text': 'warm-up'})
            ledger_file.parent.mkdir(parents=True, exist_ok=True)
            ledger_file.write_bytes(b''.join(broken))
            before = ledger_file.read_bytes()
            ref = self.store_id(folder) + ':1:' + '0' * 12
            document = folder / 'doc.txt'
            document.write_text('clause', encoding='utf-8')
            cli = {
                'append': ('append', '--kind', 'prompt', '--data', '{"text": "QA-NEW-AFTER-BROKEN"}'),
                'mirror': ('mirror', '--file', document),
                'verify': ('verify',), 'replay': ('replay',), 'search': ('search', '--text', 'QA'),
                'source': ('source', '--ref', ref),
                'pending': ('pending',),
            }
            for entry, args in cli.items():
                with self.subTest(corruption=label, entry=entry):
                    result = self.run_py(self.ledger, '--config', self.config, '--project', folder, *args)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(ledger_file.read_bytes(), before)
                    blobs = self.store(folder) / '.raw/blobs'
                    self.assertFalse(blobs.exists() and any(blobs.iterdir()), 'orphan blob after refused mirror')
            with self.subTest(corruption=label, entry='rebuild'):
                notes_before = tree_bytes(self.home / 'memory')
                result = self.run_py(self.dream, '--config', self.config, '--project', folder, 'rebuild', '--yes')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(ledger_file.read_bytes(), before)
                self.assertEqual({k: v for k, v in tree_bytes(self.home / 'memory').items() if 'dream.json' not in k},
                                 {k: v for k, v in notes_before.items() if 'dream.json' not in k})
            with self.subTest(corruption=label, entry='dream prompt'):
                result = self.run_py(self.dream, '--config', self.config, '--project', folder, 'prompt')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout.strip(), '')
            with self.subTest(corruption=label, entry='claude hook'):
                event = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'qa', 'cwd': str(folder),
                         'prompt': 'QA-HOOK-AFTER-BROKEN'}
                result = self.run_py(self.hook, '--config', self.config, '--jason-hook',
                                     stdin=json.dumps(event, ensure_ascii=False))
                self.assertEqual(result.returncode, 0, result.stderr)  # Never block the user's prompt.
                self.assertIn('未記錄', json.loads(result.stdout)['systemMessage'])
                self.assertEqual(ledger_file.read_bytes(), before)
            with self.subTest(corruption=label, entry='codex notify'):
                payload = {'type': 'agent-turn-complete', 'thread-id': 'qa', 'turn-id': 't-' + label,
                           'cwd': str(folder), 'input-messages': ['QA'], 'last-assistant-message': 'ok'}
                result = self.run_py(self.notify, json.dumps(payload))
                self.assertEqual(result.returncode, 0)
                self.assertIn('failed', result.stderr)
                self.assertEqual(ledger_file.read_bytes(), before)
        # Control: a complete last record without its newline is still valid and extendable (#46).
        folder = self.project('no-final-newline')
        self.append(folder, 'prompt', {'text': 'one'})
        ledger_file = self.store(folder) / '.raw/ledger.jsonl'
        ledger_file.write_bytes(ledger_file.read_bytes().rstrip(b'\n'))
        self.append(folder, 'prompt', {'text': 'two'})
        self.assertEqual(self.run_py(self.ledger, '--config', self.config, '--project', folder, 'verify').returncode, 0)

    def seed_lines(self):
        folder = self.project('seed')
        for n in (1, 2, 3):
            self.append(folder, 'prompt', {'text': 'QA-ORIGINAL-' + str(n)})
        return (self.store(folder) / '.raw/ledger.jsonl').read_bytes().splitlines(keepends=True)

    # --- invariant 4: a successful rebuild publishes exactly what the staging produced ---------
    def test_rebuild_publishes_staged_shared_changes_and_keeps_untouched_notes(self):
        a, b = self.project('rebuild-a'), self.project('rebuild-b')
        a_ref = self.append(a, 'prompt', {'text': 'QA: shared A rule'})
        b_ref = self.append(b, 'prompt', {'text': 'QA: shared B rule'})
        shared = self.home / 'memory/shared'
        a_name = self.note_name(self.remember(a, 'rebuild-a', '來源：ledger ' + a_ref), shared)
        b_name = self.note_name(self.remember(b, 'rebuild-b', '來源：ledger ' + b_ref), shared)
        new_ref = self.append(a, 'correction', {'text': 'QA: drop shared A rule, add shared C rule'})
        worker = self.base / 'rebuild_worker.py'
        worker.write_text(r'''
import json, os, re, subprocess, sys
from pathlib import Path
text = sys.stdin.buffer.read().decode('utf-8')
prefix = next(line for line in text.splitlines() if line.startswith('指令前綴（Bash）：'))
target = re.search(r"--config (?:'([^']+)'|(\S+))", prefix)
config = next(v for v in target.groups() if v)
runtime = sys.argv[1]
def run(*args, stdin=None):
    return subprocess.run([sys.executable, runtime, '--config', config, '--project', str(Path.cwd()), *args],
                          input=stdin, capture_output=True, text=True, encoding='utf-8', check=True).stdout
mode = os.environ['QA_REBUILD_MODE']
if mode in ('retire', 'retire-add'):
    revision = json.loads(run('context'))['stores']['global']['revision']
    run('apply', '--scope', 'global', '--plan', '-',
        stdin=json.dumps({'expected_revision': revision, 'updates': {}, 'retire': [os.environ['QA_RETIRE']]}))
if mode == 'retire-add':
    run('remember', '--scope', 'global', '--subject', 'qa', '--key', 'rebuild-c', '--value', 'VALUE_rebuild-c',
        '--why', '來源：ledger ' + os.environ['QA_NEW_REF'])
if mode == 'bad-ref':
    run('remember', '--scope', 'global', '--subject', 'qa', '--key', 'rebuild-bad', '--value', 'V',
        '--why', '來源：ledger ' + os.environ['QA_NEW_REF'] + 'f')
print('JASON_DREAM_STATUS: ok')
''', encoding='utf-8')
        cfg = json.loads(self.config.read_text(encoding='utf-8'))
        original = dict(cfg['ledger'])
        cfg['ledger']['dream_agent'] = [sys.executable, str(worker), str(self.runtime)]
        self.config.write_text(json.dumps(cfg), encoding='utf-8')
        self.addCleanup(lambda: self.config.write_text(json.dumps(dict(cfg, ledger=original)), encoding='utf-8'))
        env = {'QA_RETIRE': a_name, 'QA_NEW_REF': new_ref}

        def rebuild(mode):
            result = self.run_py(self.dream, '--config', self.config, '--project', a, 'rebuild', '--yes',
                                 extra_env=dict(env, QA_REBUILD_MODE=mode))
            return result, json.loads(result.stdout.splitlines()[-1])

        with self.subTest(mode='no-op keeps every shared note'):
            before = tree_bytes(shared)
            result, report = rebuild('noop')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(report['archived'], [])
            self.assertEqual({k: v for k, v in tree_bytes(shared).items() if not k.startswith('archive')},
                             {k: v for k, v in before.items() if not k.startswith('archive')})
        with self.subTest(mode='failed staging write changes nothing'):
            before = tree_bytes(shared)
            result, report = rebuild('bad-ref')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(report['rebuilt'])
            self.assertEqual(tree_bytes(shared), before)
        with self.subTest(mode='retire + add'):
            result, report = rebuild('retire-add')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(a_name, report['archived'])
            self.assertFalse((shared / a_name).exists())
            self.assertTrue(any(p.name == Path(a_name).name for p in (shared / 'archive').rglob('*.md')))
            self.assertTrue((shared / b_name).exists())
            for reader in (a, b):
                index = self.run_py(self.runtime, '--config', self.config, '--project', reader, 'index').stdout
                self.assertNotIn('VALUE_rebuild-a', index)
                self.assertIn('VALUE_rebuild-b', index)
                self.assertIn('VALUE_rebuild-c', index)
                self.assertIn('VALUE_rebuild-b', (shared / b_name).read_text(encoding='utf-8'))
            names = [p.name for p in shared.rglob('*.md') if 'archive' not in p.parts]
            self.assertTrue(any('VALUE_rebuild-c' in (shared / n).read_text(encoding='utf-8')
                                for n in [p.relative_to(shared).as_posix() for p in shared.rglob('*.md')
                                          if 'archive' not in p.parts]), names)
            found = self.run_py(self.ledger, '--config', self.config, '--project', b, 'source', '--ref', b_ref)
            self.assertEqual(found.returncode, 0, found.stderr)


if __name__ == '__main__':
    unittest.main()
