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

from worker_identity import as_worker

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
            # A background write needs the owner token of a reserved job for that store (#81).
            project = args[list(args).index('--project') + 1]
            with as_worker(self.config, project, env):
                return subprocess.run([sys.executable, str(script), *map(str, args)], input=stdin,
                                      capture_output=True, text=True, encoding='utf-8', env=env, timeout=120)
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
                    'full stop': '來源：ledger {}。', 'parenthesis': '（來源：ledger {}）', 'no ledger word': '來源：{}',
                    # Markdown and quoting around the reference (#55): valid must pass, invalid must fail.
                    'bold': '來源：ledger **{}**', 'italic': 'Source: ledger *{}*', 'underscore': 'Source: ledger _{}_',
                    'link label': 'Source: [ledger {}](https://example.invalid/evidence)',
                    'angle brackets': 'Source: ledger <{}>', 'double quotes': '來源：ledger "{}"',
                    'single quotes': "來源：ledger '{}'", 'corner brackets': '來源：ledger 「{}」',
                    'lenticular brackets': '來源：ledger 【{}】', 'bold code': '來源：ledger **`{}`**',
                    'no ledger word bold': '來源：**{}**',
                    'trailing colon': '來源：ledger {}: 使用者要求', 'fullwidth colon': '來源：ledger {}：使用者要求',
                    'table cell': '| 來源 | ledger {} |', 'exclamation': 'Source: ledger {}!'}
        shared = self.home / 'memory/shared'
        # Positive controls: a valid reference is accepted in every context.
        for label, template in contexts.items():
            with self.subTest(control=label):
                result = self.remember(a, 'ok-' + str(abs(hash(label))), template.format(good))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        number = 0
        for variant, ref in bad.items():
            for label, template in contexts.items():
                if label.startswith('no ledger word') and variant == 'store non-hex':
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
            # Quoted values containing spaces must be masked whole (#54).
            'quoted spaces assign': ('password="{}" QA_AFTER_QUOTE_77', 'qaprefixalpha qaprivatetailone'),
            'quoted spaces flag': ('mysql --password "{}" -u qa', 'qaprefixbeta qaprivatetailtwo'),
            'single quoted colon': ("password: '{}'", 'qaprefixgamma qaprivatetailthree'),
            'json quoted spaces': ('{{"password": "{}", "user": "QA_USER_88"}}', 'qaprefixdelta qaprivatetailfour'),
            'env quoted spaces': ('API_KEY="{}" make deploy', 'qaprefixepsilon qaprivatetailfive'),
            'spaced equals': ('client_secret = "{}"', 'qaprefixzeta qaprivatetailsix'),
            'flag equals quoted': ('--token="{}"', 'qaprefixeta qaprivatetailseven'),
            'auth header quoted': ('Authorization: "Bearer {}"', 'qaprefixtheta qaprivatetaileight'),
            'escaped quote inside': ('password="{}" QA_AFTER_ESCAPE_91', 'qaprefixiota \\"qaprivatemiddle\\" qaprivatetailnine'),
            'powershell env': ('$env:API_KEY = "{}"', 'qaprefixkappa qaprivatetailten'),
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
                    for part in [secret] + [p for p in secret.split() if len(p) >= 8]:
                        self.assertNotIn(part, text)  # Every word of a multi-word secret, not just the start.
        replay = views['replay']
        self.assertIn('QA_REQ_42', replay)
        self.assertIn('QA_KEEP_MARKER_8842', replay)
        self.assertIn('QA_AFTER_QUOTE_77', replay)
        self.assertIn('QA_AFTER_ESCAPE_91', replay)
        self.assertIn('QA_USER_88', replay)
        self.assertIn('make deploy', replay)
        self.assertIn('data_2024.csv', replay)
        self.assertNotIn(letters + 't', replay)
        self.assertIn('"input_tokens": 12', replay)

    # --- invariant 2b: ordinary values next to credential-like names are never masked or refused ----
    ORDINARY = ['PWD=/home/qa', 'pwd: C:\\Users\\qa', 'session_id: 42', 'sid=7', 'max_token: 4096',
                'max_tokens=4096', 'token_limit: 8000', 'retry_count=3', 'token: null', 'password: required',
                'password_min_length: 8', 'api_key: ${OPENAI_API_KEY}', 'token: 放在環境變數 OPENAI_API_KEY',
                'password_expires_days=90', '金鑰：存放於環境變數 QA_KEY', 'token: "环境变量 QA_TOKEN"',
                # Chinese prose after a colon describes the credential (found in self-QA of #66).
                '密碼：請向管理員索取', '密碼：不要寫進筆記', '憑證：已過期', '密碼：同上', '密碼：無',
                '密碼：每 90 天更換一次', 'API 金鑰：由使用者在執行時輸入', '令牌：從 vault 取得', '密碼：（略）',
                '密钥：保存在环境变量 QA_KEY 中', '密碼存在 1Password', '密碼政策：至少 12 碼',
                # Prose about a credential is kept; only a digit-bearing token after `is`/`是` is a value (#74).
                'password is required', 'token is expired', '密碼是必填', 'password is 8 characters',
                'max token is 4096', '密碼是 8 碼', 'the token is stored in 1Password', 'token limit is 8000']
    CREDENTIALS = [('password="0427"', '0427'), ('password=581', '581'), ('mysql --password 7391 -u qa', '7391'),
                   ('token: qaopaquelettersonly', 'qaopaquelettersonly'),
                   ('api_key=sk-qa0123456789abcdefghij', 'qa0123456789abcdefghij'),
                   ("client_secret: 'qa spaced secret'", 'spaced secret'),
                   ('password=測試甲', '測試甲'), ('密碼：測試乙', '測試乙'),
                   ('--password "échantillon exemple"', 'échantillon exemple'),
                   ('密碼：(qa123abc)', 'qa123abc'), ('密碼：用qa77secret', 'qa77secret'),
                   ('密碼=請勿外流丁', '請勿外流丁'), ('密碼：「測試丙」', '測試丙'),
                   ('my password is 4417', '4417'), ('密碼是 6630', '6630'), ('the token was qa5521tok', 'qa5521tok'),
                   ('api key is 7788', '7788')]

    def test_ordinary_values_survive_and_credentials_are_caught_in_ledger_and_notes(self):
        c = self.project('ordinary')
        ref = self.append(c, 'tool', {'tool': 'Bash', 'input': {'command': 'env'},
                                      'result': {'stdout': '\n'.join(self.ORDINARY + [l for l, _ in self.CREDENTIALS])}})
        raw = self.run_py(self.ledger, '--config', self.config, '--project', c, 'source', '--ref', ref)
        stdout = json.loads(raw.stdout)['record']['data']['result']['stdout']
        for line in self.ORDINARY:
            with self.subTest(view='ledger', ordinary=line):
                self.assertIn(line, stdout.splitlines())
        for line, secret in self.CREDENTIALS:
            with self.subTest(view='ledger', credential=line):
                self.assertNotIn(secret, stdout)
        # Found in a real Claude Code run: the same lines inside ONE shell string with literal `\n`
        # separators (prompt text and tool input), not real newlines.
        literal = "printf '" + '\\n'.join(self.ORDINARY + [l for l, _ in self.CREDENTIALS]) + "\\n'"
        prompt_ref = self.append(c, 'prompt', {'text': 'run exactly: ' + literal})
        text = json.loads(self.run_py(self.ledger, '--config', self.config, '--project', c, 'source', '--ref',
                                      prompt_ref).stdout)['record']['data']['text']
        for line in self.ORDINARY:
            with self.subTest(view='literal \\n', ordinary=line):
                self.assertIn(line, text)
        for line, secret in self.CREDENTIALS:
            with self.subTest(view='literal \\n', credential=line):
                self.assertNotIn(secret, text)
        for number, line in enumerate(self.ORDINARY):
            with self.subTest(view='note', ordinary=line):
                result = self.remember(c, 'ordinary-' + str(number), 'QA', scope='project', value=line)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for number, (line, _) in enumerate(self.CREDENTIALS):
            with self.subTest(view='note', credential=line):
                before = tree_bytes(self.store(c))
                result = self.remember(c, 'credential-' + str(number), 'QA', scope='project', value=line)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(tree_bytes(self.store(c)), before)

        page = next(self.store(c).glob('feedback/*.md'))
        before = tree_bytes(self.store(c))
        edited = page.read_text(encoding='utf-8') + '\n密碼：合成測試值\n'
        result = self.apply(c, 'project', {page.relative_to(self.store(c)).as_posix(): edited})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('credential-like value', result.stderr)
        self.assertEqual(tree_bytes(self.store(c)), before)

    # --- invariant 3: nothing is accepted or reported as fine on a ledger that fails verify ----
    def test_corrupt_ledger_is_never_extended_or_reported_as_valid(self):
        def rehashed(lines):
            records, previous, output = [json.loads(line) for line in lines], '0' * 64, []
            records[1]['data']['text'] = 'QA-REWRITTEN-2'
            for record in records:
                record['prev'] = previous
                body = {k: v for k, v in record.items() if k != 'hash'}
                record['hash'] = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
                previous = record['hash']
                output.append((json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'))
            return output

        def corruptions(lines):
            yield 'edited data', [lines[0].replace(b'QA-ORIGINAL-1', b'QA-MODIFIED-1')] + lines[1:]
            yield 'deleted middle', lines[:1] + lines[2:]
            yield 'swapped', [lines[1], lines[0]] + lines[2:]
            yield 'torn last line', lines[:-1] + [lines[-1][:len(lines[-1]) // 2]]
            yield 'garbage line', lines + [b'not json\n']
            yield 'non-object line', lines + [b'[1, 2]\n']
            # Every remaining line is valid, but the consolidated checkpoint says 3 records existed (#53).
            yield 'truncated below cursor', lines[:2]
            # A consistent re-hashed rewrite: the chain verifies, only the checkpoint can tell (#40, #53).
            yield 'rehashed rewrite below cursor', rehashed(lines)
        template = None
        seed = self.seed_lines()
        checkpoint = json.dumps({'cursor': 3, 'cursor_hash': json.loads(seed[2])['hash']})
        for label, broken in corruptions(seed):
            folder = self.project('broken-' + label.replace(' ', '-'))
            ledger_file = self.store(folder) / '.raw/ledger.jsonl'
            if template is None:
                template = self.append(folder, 'prompt', {'text': 'warm-up'})
            ledger_file.parent.mkdir(parents=True, exist_ok=True)
            ledger_file.write_bytes(b''.join(broken))
            if label in ('truncated below cursor', 'rehashed rewrite below cursor'):
                (ledger_file.parent / 'dream.json').write_text(checkpoint, encoding='utf-8')
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


    # --- invariant 5: the built-in Codex worker may write where the notes live, and only there ---
    def test_builtin_codex_worker_saves_through_its_sandbox_and_new_session_recalls(self):
        """#69: the worker runs with cwd = project under `workspace-write`; the global memory home is
        outside the project. A fake `codex` enforces the same writable roots with an audit hook."""
        bin_dir, log = self.base / 'fake-codex-bin', self.base / 'fake-codex-argv.jsonl'
        bin_dir.mkdir(exist_ok=True)
        guard = bin_dir / 'sandbox_guard.py'
        guard.write_text(r'''
import os, runpy, sys
roots = [os.path.normcase(os.path.realpath(p)) for p in os.environ['QA_WRITABLE'].split(os.pathsep)]
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
def inside(path):
    path = os.path.normcase(os.path.realpath(os.fspath(path)))
    return any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) for r in roots)
def hook(event, args):
    targets = ()
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)) and (args[2] or 0) & WRITE:
        targets = (args[0],)
    elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.truncate', 'os.symlink', 'os.link'):
        targets = (args[0],)
    elif event == 'os.rename':
        targets = args[:2]
    for target in targets:
        if not isinstance(target, int) and not inside(target):
            raise PermissionError('read-only sandbox: ' + os.fspath(target))
sys.addaudithook(hook)
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
runpy.run_path(script, run_name='__main__')
''', encoding='utf-8')
        fake = bin_dir / 'fake_codex.py'
        fake.write_text(r'''
import json, os, re, shlex, subprocess, sys
args = sys.argv[1:]
with open(os.environ['QA_CODEX_LOG'], 'a', encoding='utf-8') as f:
    f.write(json.dumps({'argv': args, 'cwd': os.getcwd()}) + '\n')
assert args[:3] == ['exec', '--sandbox', 'workspace-write'] and args[-1] == '-', args
writable = [os.getcwd()] + [args[i + 1] for i, a in enumerate(args) if a == '--add-dir']
text = sys.stdin.buffer.read().decode('utf-8')
prefix = shlex.split(next(l for l in text.splitlines() if l.startswith('指令前綴（Bash）：')).split('：', 1)[1])
ref = re.search(r'\[ref (\S+) session', text).group(1)
value = re.search(r'(QA_CODEX_PREF_\w+)', text).group(1)
env = dict(os.environ, QA_WRITABLE=os.pathsep.join(writable), PYTHONDONTWRITEBYTECODE='1')
done = subprocess.run([prefix[0], os.environ['QA_GUARD'], *prefix[1:], 'remember', '--scope', 'project',
                       '--type', 'feedback', '--subject', 'user', '--key', 'report-format', '--value', value,
                       '--why', '使用者要求；來源：ledger ' + ref], env=env, capture_output=True, text=True,
                      encoding='utf-8')
if done.returncode:
    print('環境唯讀，未執行保存：' + done.stderr.strip()[-300:])
else:
    print('JASON_DREAM_STATUS: ok')
''', encoding='utf-8')
        if os.name == 'nt':
            (bin_dir / 'codex.cmd').write_text('@"' + sys.executable + '" "' + str(fake) + '" %*\r\n', encoding='utf-8')
        else:
            launcher = bin_dir / 'codex'
            launcher.write_text('#!/bin/sh\nexec "' + sys.executable + '" "' + str(fake) + '" "$@"\n', encoding='utf-8')
            launcher.chmod(0o755)
        cfg = json.loads(self.config.read_text(encoding='utf-8'))
        original = dict(cfg['ledger'])
        cfg['ledger']['dream_agent'] = 'codex'
        self.config.write_text(json.dumps(cfg), encoding='utf-8')
        self.addCleanup(lambda: self.config.write_text(json.dumps(dict(cfg, ledger=original)), encoding='utf-8'))
        env = {'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', ''), 'QA_CODEX_LOG': str(log),
               'QA_GUARD': str(guard)}
        shared = self.home / 'memory/shared'

        def recall(project, value):
            context = self.run_py(self.runtime, '--config', self.config, '--project', project, 'index')
            self.assertEqual(context.returncode, 0, context.stderr)
            return value in context.stdout

        with self.subTest(entry='dream-run (background trigger)'):
            p, other = self.project('codex-worker'), self.project('codex-other')
            self.append(p, 'prompt', {'text': '請記住：這個專案的報告一律用 QA_CODEX_PREF_RUN 格式'})
            head = self.append(p, 'assistant', {'text': '好的'}).split(':')[1]
            before = tree_bytes(shared)
            result = self.run_py(self.ledger, '--config', self.config, '--project', p, 'dream-run', '--upto', head,
                                 extra_env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads(self.run_py(self.ledger, '--config', self.config, '--project', p, 'pending').stdout)
            self.assertEqual((state['cursor'], state['pending'], state['last_error']), (int(head), 0, None))
            call = json.loads(log.read_text(encoding='utf-8').splitlines()[-1])
            opened = [call['argv'][i + 1] for i, a in enumerate(call['argv']) if a == '--add-dir']
            self.assertEqual([os.path.normcase(os.path.realpath(d)) for d in opened],
                             [os.path.normcase(os.path.realpath(self.home))])
            self.assertTrue(recall(p, 'QA_CODEX_PREF_RUN'), 'a new session must recall the project preference')
            self.assertFalse(recall(other, 'QA_CODEX_PREF_RUN'), 'project scope must not leak to other projects')
            self.assertEqual(tree_bytes(shared), before)
        with self.subTest(entry='rebuild (staged home)'):
            p = self.project('codex-rebuild')
            self.append(p, 'prompt', {'text': '請記住：這個專案的報告一律用 QA_CODEX_PREF_REBUILD 格式'})
            result = self.run_py(self.dream, '--config', self.config, '--project', p, 'rebuild', '--yes', extra_env=env)
            report = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(report['rebuilt'])
            self.assertTrue(recall(p, 'QA_CODEX_PREF_REBUILD'))


    # --- invariant 6: a damaged store heals itself or names the file to fix, on every entry ------
    def test_damaged_store_heals_or_names_the_file_on_every_entry(self):
        """#77 #78 #79: never 'all memory gone' silently, never a dead end without a path."""
        def seeded(label):
            folder = self.project('damage-' + label)
            ref = self.append(folder, 'prompt', {'text': 'QA: project rule and link'})
            for key, kind in (('damage-proj', 'project'), ('damage-ref', 'reference')):
                result = self.run_py(self.runtime, '--config', self.config, '--project', folder, 'remember',
                                     '--scope', 'project', '--type', kind, '--subject', 'qa', '--key', key,
                                     '--value', 'VALUE_' + key + '_' + label, '--why', '來源：ledger ' + ref, dream=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            return folder, ref

        def entries(folder, ref, label):
            yield 'index', self.run_py(self.runtime, '--config', self.config, '--project', folder, 'index')
            yield 'context', self.run_py(self.runtime, '--config', self.config, '--project', folder, 'context')
            yield 'remember', self.run_py(self.runtime, '--config', self.config, '--project', folder, 'remember',
                                          '--scope', 'project', '--subject', 'qa', '--key', 'after-' + label,
                                          '--value', 'VALUE_after_' + label, '--why', '來源：ledger ' + ref, dream=True)
            event = {'hook_event_name': 'SessionStart', 'session_id': 'qa', 'cwd': str(folder), 'source': 'startup'}
            yield 'claude hook', self.run_py(self.hook, '--config', self.config, '--jason-hook',
                                             stdin=json.dumps(event))

        for label in ('missing-index', 'oversized-note'):
            folder, ref = seeded(label)
            store = self.store(folder)
            if label == 'missing-index':
                (store / 'MEMORY.md').unlink()
            else:
                (store / 'feedback').mkdir(exist_ok=True)
                (store / 'feedback/preference-qa-huge.md').write_bytes(
                    b'---\nname: preference-qa-huge\n---\n\n' + b'x' * (4 * 1024 * 1024 + 10))
            for entry, result in entries(folder, ref, label):
                with self.subTest(damage=label, entry=entry):
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    if entry in ('index', 'claude hook'):
                        names = [p.relative_to(store).as_posix() for p in store.rglob('*.md')
                                 if 'archive' not in p.parts and p.name != 'MEMORY.md']
                        for name in names:
                            self.assertIn(name, result.stdout)
                    if entry == 'claude hook':
                        self.assertNotIn('CONFLICT:', result.stdout)
            if label == 'oversized-note':
                kept = list((store / 'archive/quarantine').rglob('preference-qa-huge.md'))
                self.assertEqual(len(kept), 1)
                self.assertEqual(kept[0].stat().st_size, len(b'---\nname: preference-qa-huge\n---\n\n') + 4 * 1024 * 1024 + 10)

        for label, where in (('project-journal', 'store'), ('global-journal', 'home')):
            folder, ref = seeded(label)
            journal = (self.store(folder) / '.transaction.json' if where == 'store'
                       else self.home / '.rebuild-transaction.json')
            journal.write_text('{"id": ', encoding='utf-8')
            try:
                for entry, result in entries(folder, ref, label):
                    with self.subTest(damage=label, entry=entry):
                        shown = result.stdout + result.stderr
                        if entry == 'claude hook':
                            # Reported with the file to fix, without blocking the user's prompt (#87).
                            self.assertEqual(result.returncode, 0, shown)
                            self.assertIn('systemMessage', result.stdout)
                            self.assertIn('MEMORY UNAVAILABLE', result.stdout)
                        else:
                            self.assertNotEqual(result.returncode, 0, shown)
                        self.assertIn(journal.name, shown)
                        self.assertIn('move it out of the memory folder', shown)
            finally:
                journal.unlink()
            result = self.run_py(self.runtime, '--config', self.config, '--project', folder, 'index')
            self.assertEqual(result.returncode, 0, result.stderr)


    # --- invariant 7: every hint the snapshot gives works as written, on a store past every budget ----
    def test_snapshot_hints_work_on_a_large_store_and_worker_identity_is_bound(self):
        """#82 #83 #81 on the packaged ZIP: run the hinted command verbatim; a stray JASON_DREAM unlocks nothing."""
        import re
        import shlex
        folder = self.project('large-store')
        ref = self.append(folder, 'prompt', {'text': 'QA large store'})
        for i in range(45):
            kind = ('project', 'reference', 'feedback')[i % 3]
            result = self.run_py(self.runtime, '--config', self.config, '--project', folder, 'remember', '--scope',
                                 'project', '--type', kind, '--subject', 'qa', '--key', 'large-' + str(i),
                                 '--value', 'QA large rule ' + str(i) + ' ' + '很長的摘要內容 ' * 12,
                                 '--why', '來源：ledger ' + ref, dream=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_py(self.runtime, '--config', self.config, '--project', folder, 'remember', '--scope',
                             'project', '--type', 'user', '--subject', 'qa', '--key', 'identity',
                             '--value', 'QA_LARGE_USER_IDENTITY', '--why', '來源：ledger ' + ref, dream=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [l for l in (self.store(folder) / 'MEMORY.md').read_text(encoding='utf-8').splitlines()
                if l.startswith('- [')]
        event = {'hook_event_name': 'SessionStart', 'session_id': 'qa', 'cwd': str(folder), 'source': 'startup'}
        snap = json.loads(self.run_py(self.hook, '--config', self.config, '--jason-hook',
                                      stdin=json.dumps(event)).stdout)['hookSpecificOutput']['additionalContext']
        with self.subTest(check='hint returns the full project index'):
            self.assertIn('INDEX TRUNCATED', snap)
            prefix = shlex.split(re.search(r'^Command prefix \(Bash\): (.+)$', snap, re.M).group(1))
            hint = re.search(r'for the full list run: Command prefix \+ (index[^(]*?) \(', snap).group(1).split()
            full = subprocess.run(prefix + hint, capture_output=True, text=True, encoding='utf-8', env=self.env)
            self.assertEqual(full.returncode, 0, full.stderr)
            store = json.loads(full.stdout)['stores']['project']
            self.assertFalse(store['truncated'])
            self.assertEqual(sum(1 for l in store['index'].splitlines() if l.startswith('- [')), len(rows))
        with self.subTest(check='user identity is inlined first'):
            self.assertIn('QA_LARGE_USER_IDENTITY', snap)
        with self.subTest(check='bare JASON_DREAM=1 cannot write'):
            stray = self.run_py(self.runtime, '--config', self.config, '--project', folder, 'remember', '--scope',
                                'project', '--subject', 'qa', '--key', 'stray', '--value', 'QA_STRAY',
                                '--why', '來源：ledger ' + ref, extra_env={'JASON_DREAM': '1'})
            self.assertEqual(stray.returncode, 1, stray.stdout)
            self.assertIn('background memory mode', stray.stderr)
        with self.subTest(check='worker hook still blocks direct edits'):
            target = self.store(folder) / 'feedback/qa-direct.md'
            pre = {'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'session_id': 'w', 'cwd': str(folder),
                   'tool_input': {'file_path': str(target)}}
            out = self.run_py(self.hook, '--config', self.config, '--jason-hook', stdin=json.dumps(pre),
                              extra_env={'JASON_DREAM': '1'})
            self.assertEqual(json.loads(out.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        with self.subTest(check='pending discloses consolidation switched off by settings'):
            pending = json.loads(self.run_py(self.ledger, '--config', self.config, '--project', folder, 'pending').stdout)
            self.assertTrue(any('dream_every=100000' in note for note in pending['settings']))


if __name__ == '__main__':
    unittest.main()
