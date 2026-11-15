"""One project install configures both Claude Code (hooks) and Codex (notify) without clobbering."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import install_hooks  # noqa: E402


class InstallBothHostsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.project = base / 'project'
        self.project.mkdir()
        self.profile = base / 'profile'
        (self.profile / '.codex').mkdir(parents=True)
        self.toml = self.profile / '.codex/config.toml'
        self.set_config({'enabled': True, 'foreground_writes': False})

    def set_config(self, ledger):
        cfg = {'version': 1, 'mode': 'project', 'memory_root': '.jason-memory'}
        if ledger is not None:
            cfg['ledger'] = ledger
        (self.project / '.jason-memory.json').write_text(json.dumps(cfg), encoding='utf-8')

    def install(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = install_hooks.main(['--project', str(self.project), '--user-home', str(self.profile), *extra])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_one_install_configures_claude_hooks_and_codex_notify(self):
        self.toml.write_bytes(b'model = "x"\n[profiles.a]\nk = 1\n')
        result = self.install()
        self.assertEqual(result['codex_notify'], 'installed')
        settings = (self.project / '.claude/settings.json').read_text(encoding='utf-8')
        self.assertIn('claude_memory_hook.py', settings)
        data = self.toml.read_text(encoding='utf-8')
        self.assertTrue(data.startswith('notify = '))  # Top-level key, before any [table].
        self.assertIn('codex_notify.py', data)
        self.assertIn('model = "x"\n[profiles.a]\nk = 1\n', data)
        self.assertEqual(self.install()['codex_notify'], 'unchanged')
        self.assertEqual(data.count('codex_notify.py'), 1)

    def test_existing_foreign_notify_is_never_replaced(self):
        original = b'notify = ["my-notifier"]\n'
        self.toml.write_bytes(original)
        self.assertIn('skipped', self.install()['codex_notify'])
        self.assertEqual(self.toml.read_bytes(), original)

    def test_uninstall_removes_only_our_notify_and_hooks(self):
        self.toml.write_bytes(b'\xef\xbb\xbfmodel = "x"\r\n')
        self.install()
        self.install('--uninstall')
        self.assertEqual(self.toml.read_bytes(), b'\xef\xbb\xbfmodel = "x"\r\n')
        settings = self.project / '.claude/settings.json'
        self.assertNotIn('claude_memory_hook.py', settings.read_text(encoding='utf-8') if settings.exists() else '')

    def test_foreground_mode_needs_no_notify_and_missing_codex_is_skipped(self):
        self.set_config(None)
        self.assertIn('not needed', self.install()['codex_notify'])
        self.assertFalse(self.toml.exists())
        self.set_config({'enabled': True, 'foreground_writes': False})
        shutil.rmtree(self.profile / '.codex')
        self.assertIn('Codex configuration folder not found', self.install()['codex_notify'])
        self.assertFalse((self.profile / '.codex').exists())

    def test_dry_run_changes_nothing(self):
        self.toml.write_bytes(b'model = "x"\n')
        result = self.install('--dry-run')
        self.assertEqual(result['codex_notify'], 'installed')
        self.assertEqual(self.toml.read_bytes(), b'model = "x"\n')
        self.assertFalse((self.project / '.claude/settings.json').exists())

    def make_dispatcher(self, folder):
        """A stand-in for another Jason-memory installation's tools folder."""
        tools = self.project.parent / folder / 'tools'
        tools.mkdir(parents=True)
        (tools / 'codex_notify.py').write_text('', encoding='utf-8')
        (tools / 'memory_ledger.py').write_text('', encoding='utf-8')
        return tools / 'codex_notify.py'

    def test_project_b_shares_a_and_uninstalling_b_keeps_a(self):
        """#19: ownership is the exact script path, not the file name."""
        a = self.make_dispatcher('A')
        original = ('notify = ' + json.dumps(['python', a.as_posix()]) + '\nmodel = "x"\n').encode()
        self.toml.write_bytes(original)
        self.assertTrue(self.install()['codex_notify'].startswith('shared'))
        self.assertEqual(self.toml.read_bytes(), original)
        self.assertTrue(self.install('--uninstall')['codex_notify'].startswith('kept'))
        self.assertEqual(self.toml.read_bytes(), original)

    def test_unrelated_script_with_the_same_name_is_foreign(self):
        tool = self.project.parent / 'my-unrelated-tool'
        tool.mkdir()
        (tool / 'codex_notify.py').write_text('', encoding='utf-8')
        original = ('notify = ' + json.dumps([(tool / 'codex_notify.py').as_posix()]) + '\n').encode()
        self.toml.write_bytes(original)
        result = self.install()
        self.assertTrue(result['codex_notify'].startswith('skipped'))
        self.assertIn('NOT connected', result['warnings'][0])
        self.assertEqual(self.toml.read_bytes(), original)

    def test_stale_jason_line_is_replaced(self):
        gone = (self.project.parent / 'deleted-copy/tools/codex_notify.py').as_posix()
        self.toml.write_bytes(('notify = ' + json.dumps(['python', gone]) + '\n').encode())
        self.assertEqual(self.install()['codex_notify'], 'installed')
        data = self.toml.read_text(encoding='utf-8')
        self.assertNotIn('deleted-copy', data)
        self.assertIn((ROOT / 'tools/codex_notify.py').as_posix(), data)


if __name__ == '__main__':
    unittest.main()
