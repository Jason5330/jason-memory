"""Regression tests for #87-#94 and the #85/#86 follow-ups. Each test states the invariant, not only the
reported repro: memory never blocks the user's work, every scope flag means the same thing, ordinary text
is not a citation, a derived index can always be rebuilt, the agent learns what the user is told."""
import ast
import contextlib
import gc
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
import memory_runtime as runtime  # noqa: E402
import memory_ledger as ledger  # noqa: E402
import memory_dream as dream  # noqa: E402
import claude_memory_hook as hook  # noqa: E402
import install_hooks  # noqa: E402
from worker_identity import as_worker  # noqa: E402


class Base(unittest.TestCase):
    ledger_cfg = {'enabled': True, 'foreground_writes': False, 'dream_every': 20}

    def setUp(self):
        self.spawned, original = [], ledger._spawn
        ledger._spawn = self.spawned.append
        self.addCleanup(setattr, ledger, '_spawn', original)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve()
        self.config = self.project / '.jason-memory.json'
        self.write_config(self.ledger_cfg)
        with runtime.opened(self.config) as (_, _, roots):
            self.root = roots['project']
        self.env = dict(os.environ, PYTHONUTF8='1')
        for key in ('JASON_DREAM', 'JASON_DREAM_OWNER'):
            self.env.pop(key, None)

    def write_config(self, ledger_cfg):
        self.config.write_text(json.dumps({'version': 1, 'mode': 'project', 'memory_root': '.jason-memory',
                                           'ledger': ledger_cfg}), encoding='utf-8')

    def cli(self, script, *args, env=None):
        return subprocess.run([sys.executable, str(ROOT / 'tools' / script), '--config', str(self.config), *args],
                              capture_output=True, text=True, encoding='utf-8', env=env or self.env, timeout=120)

    def remember(self, key, value, kind='feedback', why='使用者要求'):
        with as_worker(self.config, env={}), runtime.opened(self.config):
            return runtime.remember(self.root, 'user', key, value, why, kind=kind)

    def hook_run(self, event, config=None):
        event = dict({'session_id': 'qa', 'cwd': str(self.project)}, **event)
        result = subprocess.run([sys.executable, str(ROOT / 'tools/claude_memory_hook.py'), '--config',
                                 str(config or self.config), '--jason-hook'], input=json.dumps(event),
                                capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=120)
        return result, (json.loads(result.stdout) if result.stdout.strip() else {})

    def snapshot(self):
        return hook.handle({'hook_event_name': 'SessionStart', 'session_id': 'qa', 'cwd': str(self.project)},
                           self.config)['hookSpecificOutput']['additionalContext']


class SettingsDisclosureFollowUp(Base):
    def test_every_behaviour_changing_setting_is_disclosed(self):
        """#85 follow-up: any non-default dream_every, and foreground writes with the ledger on."""
        self.assertEqual(ledger.settings_notes(ledger.settings(self.config)), [])
        for cfg, expected in (({'dream_every': 100}, 'ledger.dream_every=100'),
                              ({'dream_every': 5}, 'ledger.dream_every=5'),
                              ({'foreground_writes': True}, 'ledger.foreground_writes=true'),
                              ({'dream_timeout': 60}, 'ledger.dream_timeout=60')):
            with self.subTest(cfg=cfg):
                self.write_config(dict(self.ledger_cfg, **cfg))
                notes = ledger.settings_notes(ledger.settings(self.config))
                self.assertTrue(any(expected in n for n in notes), notes)
                self.assertIn(expected, self.snapshot())
        self.write_config({'enabled': False})  # A store without the ledger is a supported setup.
        self.assertEqual(ledger.settings_notes(ledger.settings(self.config)), [])


class DoctorDriftTests(Base):
    def test_doctor_reports_an_index_row_that_no_longer_matches_its_note(self):
        """#86 follow-up: doctor, not only the runtime audit, sees a note edited outside the runtime."""
        saved = self.remember('qa.drift', 'QA drift original')
        path = Path(saved['paths'][0])
        path.write_text(path.read_text(encoding='utf-8').replace('"QA drift original"', '"QA drift edited"'),
                        encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = runtime.storelib.doctor.main(['doctor', str(self.root)])
        self.assertEqual(code, 1, output.getvalue())
        self.assertIn('index out of sync with notes', output.getvalue())
        self.assertIn(path.relative_to(self.root).as_posix(), output.getvalue())
        # The audit names it once, not twice.
        with runtime.opened(self.config) as (_, _, roots):
            structure = runtime.context(roots)['project']['audit']['structural_errors']
        self.assertEqual(structure.count('out of sync with notes'), 1)

    def test_hand_written_index_is_not_held_to_runtime_rows(self):
        store = self.project / 'hand'
        (store / 'feedback').mkdir(parents=True)
        (store / 'feedback/a.md').write_text('---\nname: a\ndescription: one thing\ntype: feedback\ncreated: '
                                             '2026-01-01\nupdated: 2026-01-01\n---\n\nRule.\n\nWhy: x\n\n'
                                             'How to apply: y\n', encoding='utf-8')
        (store / 'MEMORY.md').write_text('# Memory\n\n- [A](feedback/a.md) — a different summary\n', encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = runtime.storelib.doctor.main(['doctor', str(store)])
        self.assertEqual(code, 0, output.getvalue())


class HookNeverBlocksTests(Base):
    """#87 #93: memory is an add-on; no failure may block the user's prompt or stop the session."""

    def damaged(self, label):
        index = self.root / 'MEMORY.md'
        if label == 'invalid utf-8 index':
            index.write_bytes(index.read_bytes() + b'\xff\xfe')
        elif label == 'missing config':
            self.config.unlink()
        elif label == 'unreadable journal':
            (self.root / '.transaction.json').write_text('{"id": ', encoding='utf-8')

    def test_no_failure_blocks_the_prompt_and_the_agent_is_told(self):
        for label in ('invalid utf-8 index', 'missing config', 'unreadable journal'):
            with self.subTest(label=label):
                backup = (self.root / 'MEMORY.md').read_bytes()
                config = self.config.read_bytes()
                self.damaged(label)
                try:
                    for name in ('UserPromptSubmit', 'SessionStart', 'Stop', 'PostToolUse'):
                        result, output = self.hook_run({'hook_event_name': name, 'prompt': 'QA',
                                                        'tool_name': 'Bash', 'tool_response': {}})
                        self.assertEqual(result.returncode, 0, result.stderr)
                        if name in ('UserPromptSubmit', 'SessionStart'):
                            self.assertIn('Jason-memory hook failed', output.get('systemMessage', ''))
                            context = output['hookSpecificOutput']['additionalContext']
                            self.assertIn('MEMORY UNAVAILABLE', context)
                            self.assertIn('claude_memory_hook.py', context)  # The command, for the fix.
                finally:
                    self.config.write_bytes(config)
                    (self.root / 'MEMORY.md').write_bytes(backup)
                    if (self.root / '.transaction.json').exists():
                        (self.root / '.transaction.json').unlink()

    def test_the_write_guard_still_refuses_memory_paths_when_it_cannot_run(self):
        self.config.unlink()
        for path, refused in ((str(self.root / 'feedback/x.md'), True), (str(self.project / 'src/app.py'), False)):
            with self.subTest(path=path):
                result, output = self.hook_run({'hook_event_name': 'PreToolUse', 'tool_name': 'Write',
                                                'tool_input': {'file_path': path}})
                self.assertEqual(result.returncode, 0, result.stderr)
                decision = output.get('hookSpecificOutput', {}).get('permissionDecision')
                self.assertEqual(decision == 'deny', refused, output)

    def test_hook_config_from_another_project_does_not_block(self):
        other = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, other, True)
        (other / '.jason-memory.json').write_text(self.config.read_text(encoding='utf-8'), encoding='utf-8')
        result, output = self.hook_run({'hook_event_name': 'UserPromptSubmit', 'prompt': 'QA', 'cwd': str(other)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('disagrees', output['systemMessage'])

    def test_installed_command_follows_the_project_folder(self):
        """#87: no absolute project path in the hook command, so moving the folder keeps it working."""
        command = install_hooks.hook_command(ROOT, ROOT / '.jason-memory.json')
        self.assertIn('"$CLAUDE_PROJECT_DIR/tools/claude_memory_hook.py"', command)
        self.assertIn('--config "$CLAUDE_PROJECT_DIR/.jason-memory.json"', command)
        self.assertNotIn(ROOT.as_posix(), command)
        outside = install_hooks.hook_command(self.project, self.config)  # Framework outside the project.
        self.assertIn((ROOT / 'tools/claude_memory_hook.py').as_posix(), outside)
        bash = shutil.which('bash')
        if os.name == 'nt' or bash is None:
            return  # The real Claude Code run on Windows is part of the release QA.
        moved = Path(tempfile.mkdtemp()).resolve() / 'moved project'
        self.addCleanup(shutil.rmtree, moved.parent, True)
        shutil.copytree(ROOT / 'tools', moved / 'tools', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'global', moved / 'global', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'templates', moved / 'templates')
        (moved / '.jason-memory.json').write_text(self.config.read_text(encoding='utf-8'), encoding='utf-8')
        event = json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 'qa', 'cwd': str(moved),
                            'prompt': 'QA moved'})
        result = subprocess.run([bash, '-c', install_hooks.hook_command(moved, moved / '.jason-memory.json')],
                                input=event, capture_output=True, text=True, encoding='utf-8',
                                env=dict(self.env, CLAUDE_PROJECT_DIR=str(moved)), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('QA moved', (moved / '.jason-memory/.raw/ledger.jsonl').read_text(encoding='utf-8'))


class ScopeFlagTests(Base):
    def test_index_and_export_honour_scope_like_read_and_verify(self):
        """#88"""
        for command in (('index',), ('export', '--output', str(self.project / 'out.md')),
                        ('read', '--note', 'x.md'), ('verify', '--note', 'x.md')):
            with self.subTest(command=command[0]):
                result = self.cli('memory_runtime.py', '--scope', 'global', *command)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('Unavailable scope', result.stderr)
        self.assertFalse((self.project / 'out.md').exists())
        for args in ((), ('--scope', 'project')):
            result = self.cli('memory_runtime.py', *args, 'index')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(json.loads(result.stdout)['stores']), ['project'])
        result = self.cli('memory_runtime.py', '--scope', 'project', 'export', '--output', str(self.project / 'p.md'))
        self.assertEqual(json.loads(result.stdout)['scopes'], ['project'])


class OrdinaryTextIsNotACitationTests(Base):
    def test_hash_shaped_content_is_saved_verbatim(self):
        """#89: git SHA prefixes and URL paths are content; real citations are still verified."""
        ref = ledger.reference('local', ledger.append(self.root, 'prompt', {'text': 'QA 89'}))
        for i, value in enumerate(('修正位於 abc1234567890123456789012345678:42:abcdef1234 的解析邏輯。',
                                   '參考 https://example.com/deadbeef12:34:abcdef1234 這個頁面。',
                                   'deadbeef:12:abcdef12 這段雜湊要保留。',
                                   'Fixed in abc1234:42:def5678 and 0123456789abcdef0:1:abcdef123456.')):
            with self.subTest(value=value):
                saved = self.remember('qa.hash' + str(i), value)
                self.assertIn(value, Path(saved['paths'][0]).read_text(encoding='utf-8'))
        for i, why in enumerate(('來源：ledger local:900:ab12cd34ef56', '來源：deadbeef:12:abcdef12',
                                 'Source: ' + ref[:-1] + 'z', 'see local:9:abc')):
            with self.subTest(why=why):
                with self.assertRaisesRegex(ValueError, 'does not resolve: .*Nothing was saved'):
                    self.remember('qa.badref' + str(i), 'QA bad ' + str(i), why=why)
        # The exact shape the ledger writes is still a citation anywhere, and must resolve.
        fake = '0123456789abcdef:1:abcdef123456'
        with self.assertRaisesRegex(ValueError, 'does not resolve'):
            self.remember('qa.exact', 'QA see ' + fake)
        self.remember('qa.good', 'QA good', why='來源：ledger ' + ref)


class PackagedTestImportsTests(unittest.TestCase):
    def test_every_local_module_a_packaged_test_imports_is_packaged(self):
        """#90: the ZIP's own tests must import only files that are in the ZIP."""
        builder = ROOT / 'tools/build_download.py'
        if not builder.exists():
            self.skipTest('builder not packaged')
        import build_download
        packaged = set(build_download.PACKAGE_FILES)
        local = {p.stem for p in (ROOT / 'tests').glob('*.py')}
        for name in sorted(n for n in packaged if n.startswith('tests/') and n.endswith('.py')):
            tree = ast.parse((ROOT / name).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                           else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                for module in modules:
                    top = module.split('.')[0]
                    if top in local:
                        self.assertIn('tests/' + top + '.py', packaged, name + ' imports ' + top)


class AlwaysApplyDeliveryTests(Base):
    def test_more_rules_are_inlined_compactly_and_the_rest_is_disclosed(self):
        """#91: compact bodies, no duplicate index rows for inlined notes, per-note cap, explicit disclosure."""
        for i in range(2):
            self.remember('qa.who' + str(i), 'QA_WHO_' + str(i) + ' 資深工程師', 'user')
        for i in range(30):
            self.remember('qa.rule' + str(i), 'QA_RULE_' + str(i) + ' ' + '寫檔一律指定 encoding 並回報錯誤 ' * 3)
        for i in range(30):
            self.remember('qa.fact' + str(i), 'QA_FACT_' + str(i) + ' ' + '輸出放在 output 目錄 ' * 3,
                          'project' if i % 2 else 'reference')
        snap = self.snapshot()
        self.assertLessEqual(len(snap), hook.LIMIT)
        inlined = re.findall(r'^NOTE .+ \((user|feedback); --scope \w+ --note [\w/.-]+\)$', snap, re.M)
        self.assertGreaterEqual(len(inlined), 10, snap[-2000:])
        self.assertEqual(inlined[:2], ['user', 'user'])
        self.assertNotIn('jason-facts', snap)
        self.assertNotIn('How to apply: 本專案；QA_RULE_0', snap)
        partial = re.search(r'PARTIAL SNAPSHOT: (\d+) always-apply note bodies omitted', snap)
        self.assertEqual(int(partial.group(1)), 32 - len(inlined))
        self.assertIn('NOT delivered in full', snap)
        # An inlined note is not listed again; an omitted one is listed before ordinary rows.
        rows = [line for line in snap.splitlines() if line.startswith('- [')]
        for line in rows:
            self.assertNotRegex(line, r'QA_WHO_')
        self.assertIn('](feedback/', rows[0] if rows else '')
        self.assertIn('QA_RULE_', rows[0] if rows else '')

    def test_a_long_rule_is_shortened_not_dropped(self):
        self.remember('qa.long', 'QA_LONG_START ' + '很長的規則內容 ' * 400)
        snap = self.snapshot()
        self.assertIn('QA_LONG_START', snap)
        self.assertIn('[shortened; full note: read --scope project --note', snap)
        self.assertIn('1 shortened', snap)


class LedgerFailureReachesTheAgentTests(Base):
    def test_broken_ledger_warning_is_in_the_agent_context(self):
        """#92"""
        ledger.append(self.root, 'prompt', {'text': 'one'})
        ledger.append(self.root, 'prompt', {'text': 'two'})
        path = ledger.raw_root(self.root) / ledger.LEDGER
        lines = path.read_text(encoding='utf-8').splitlines()
        record = json.loads(lines[0])
        record['hash'] = '0' * 64
        lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        for name in ('UserPromptSubmit', 'SessionStart'):
            result, output = self.hook_run({'hook_event_name': name, 'prompt': 'after break'})
            self.assertEqual(result.returncode, 0, result.stderr)
            if name == 'UserPromptSubmit':
                self.assertIn('原始帳本無法寫入', output['systemMessage'])
                context = output['hookSpecificOutput']['additionalContext']
                self.assertIn('LEDGER WARNING', context)
                self.assertIn('NOT recorded', context)

    def test_missing_receipt_is_no_evidence_not_a_disk_error(self):
        with runtime.opened(self.config):
            with self.assertRaisesRegex(ValueError, 'Missing, stale or invalid memory evidence'):
                runtime.check_receipt(self.root, 'deadbeef' * 4, 0)


class OversizedIndexTests(Base):
    def test_oversized_index_is_set_aside_and_rebuilt(self):
        """#93: every entry point keeps working; the old index is kept byte for byte."""
        saved = self.remember('qa.kept', 'QA_KEPT_93')
        huge = ('# 記憶索引\n\n' + ('- [x](y.md) — ' + 'A' * 200 + '\n') * 6000).encode('utf-8')
        output = io.StringIO()
        (self.root / 'MEMORY.md').write_bytes(huge)
        with contextlib.redirect_stdout(output):
            self.assertEqual(runtime.storelib.doctor.main(['doctor', str(self.root)]), 1)
        self.assertIn('archive/quarantine/', output.getvalue())
        result, out = self.hook_run({'hook_event_name': 'UserPromptSubmit', 'prompt': 'QA'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('QA_KEPT_93', json.dumps(out, ensure_ascii=False))
        kept = list((self.root / 'archive/quarantine').rglob('MEMORY.md'))
        self.assertEqual([p.read_bytes() for p in kept], [huge])
        for args in (('index',), ('index', '--full'), ('verify', '--note',
                                                       Path(saved['paths'][0]).relative_to(self.root).as_posix())):
            result = self.cli('memory_runtime.py', *args)
            self.assertEqual(result.returncode, 0, result.stderr)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(runtime.storelib.doctor.main(['doctor', str(self.root)]), 0, output.getvalue())

    def test_index_rows_have_bounded_length(self):
        value = 'QA_LONG_VALUE ' + '規則內容' * 200
        saved = self.remember('qa.longvalue', value)
        row = next(line for line in (self.root / 'MEMORY.md').read_text(encoding='utf-8').splitlines()
                   if 'QA_LONG_VALUE' in line)
        self.assertLess(len(row), runtime.DESCRIPTION_CAP + 120)
        self.assertIn(value, Path(saved['paths'][0]).read_text(encoding='utf-8'))


class AgentPipesTests(unittest.TestCase):
    def run_agent(self, script, timeout):
        folder = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, folder, True)
        agent = folder / 'agent.py'
        agent.write_text(script, encoding='utf-8')
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            try:
                code = dream._run_agent([sys.executable, str(agent)], 'prompt ' * 1000, dict(os.environ),
                                        folder, timeout)
            except subprocess.TimeoutExpired:
                code = 'timeout'
            gc.collect()
        return code, [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]

    def test_no_pipe_handle_leaks_on_any_path(self):
        """#94"""
        for label, script, timeout, expected in (
                ('ok', 'import sys\nsys.stdin.read()\nprint("JASON_DREAM_STATUS: ok")\n', 60, 0),
                ('early exit', 'import sys\nsys.exit(3)\n', 60, 3),
                ('stuck', 'import time\ntime.sleep(60)\n', 2, 'timeout')):
            with self.subTest(label=label):
                code, leaked = self.run_agent(script, timeout)
                self.assertEqual(code, expected)
                self.assertEqual(leaked, [])


if __name__ == '__main__':
    unittest.main()
