"""Destructive cleanup tests run ONLY in temporary isolated profiles/packages."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import purge_memory as purge
import build_download
import build_global_download


class PurgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='jason-purge-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.profile = self.base / '同事 profile'
        self.project = self.base / 'project with spaces'
        self.profile.mkdir()
        with ZipFile(build_download.build(self.base / 'project.zip')) as archive:
            archive.extractall(self.project.parent / 'unpack')
        (self.project.parent / 'unpack/jason-memory').rename(self.project)
        self.home = self.profile / '.jason-memory-global'

    def args(self, **kwargs):
        data = dict(project=str(self.project), user_home=str(self.profile), home=None,
                    codex_home=None, claude_home=None, dry_run=False)
        data.update(kwargs)
        return argparse.Namespace(**data)

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def install(self, custom=None):
        if custom:
            self.home = custom
        self.put(self.profile / '.codex/AGENTS.md', b'\xef\xbb\xbf# Personal\r\nKeep rules\r\n')
        self.put(self.profile / '.claude/CLAUDE.md', b'# Other instructions\n')
        self.put(self.profile / '.claude/settings.json', json.dumps({
            'permissions': {'allow': ['Read']}, 'model': 'personal-model',
            'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo keep'}]}]}
        }).encode())
        self.run_cli(ROOT / 'global/install.py', ['--home', str(self.home)])
        self.put(self.home / 'memory/shared/user.md', b'private preferences')
        self.put(self.home / 'memory/projects/project-hash/.receipts/private.json', b'private receipt')

    def run_cli(self, script, extra=(), code=0):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(script),
                                 '--user-home', str(self.profile), *extra],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def snapshot(self):
        return {str(p.relative_to(self.base)): p.read_bytes()
                for p in self.base.rglob('*') if p.is_file()}

    def test_full_project_and_global_keep_user_files_settings_and_other_projects(self):
        self.install()
        self.put(self.project / 'report.xlsx', b'precious work')
        self.put(self.project / '.git/config', b'user repository metadata')
        self.put(self.project / '.jason-memory/feedback/private.md', b'private local note')
        self.put(self.project / '.jason-memory/.hook-state/state.json', b'private state')
        other = self.base / 'other-project/.jason-memory/MEMORY.md'
        self.put(other, b'keep other project memory')
        result = self.run_cli(self.project / 'tools/purge_memory.py')
        self.assertTrue(json.loads(result.stdout)['complete'])
        self.assertFalse(self.home.exists())
        self.assertFalse((self.project / '.jason-memory').exists())
        for name in ('AGENTS.md', 'CLAUDE.md', 'SKILL.md', 'tools/memory_runtime.py', '.jason-memory.json'):
            self.assertFalse((self.project / name).exists(), name)
        self.assertEqual((self.project / 'report.xlsx').read_bytes(), b'precious work')
        self.assertEqual((self.project / '.git/config').read_bytes(), b'user repository metadata')
        self.assertEqual(other.read_bytes(), b'keep other project memory')
        self.assertEqual((self.profile / '.codex/AGENTS.md').read_bytes(), b'\xef\xbb\xbf# Personal\r\nKeep rules\r\n')
        self.assertEqual((self.profile / '.claude/CLAUDE.md').read_bytes(), b'# Other instructions\n')
        settings = json.loads((self.profile / '.claude/settings.json').read_bytes())
        self.assertEqual(settings['hooks']['Stop'][0]['hooks'][0]['command'], 'echo keep')
        self.assertEqual(settings['permissions'], {'allow': ['Read']})
        self.assertEqual(settings['model'], 'personal-model')
        self.assertFalse(list(self.profile.rglob('*.bak')))
        # It can be rerun safely after removing itself from normal AI entry points.
        self.run_cli(self.project / 'tools/purge_memory.py')

    def test_dry_run_does_not_modify_any_file(self):
        self.install()
        before = self.snapshot()
        result = self.run_cli(self.project / 'tools/purge_memory.py', ['--dry-run'])
        self.assertTrue(json.loads(result.stdout)['delete'])
        self.assertEqual(before, self.snapshot())

    def test_custom_global_home_detected_from_entrance(self):
        self.install(self.base / '自訂 shared')
        self.run_cli(self.project / 'tools/purge_memory.py')
        self.assertFalse(self.home.exists())

    def test_global_custom_home_preserves_unrelated_files(self):
        self.install(self.base / 'existing folder')
        self.put(self.home / 'family.xlsx', b'unrelated work')
        self.put(self.home / 'framework/custom-app.txt', b'another application')
        self.run_cli(self.project / 'tools/purge_memory.py')
        self.assertEqual((self.home / 'family.xlsx').read_bytes(), b'unrelated work')
        self.assertEqual((self.home / 'framework/custom-app.txt').read_bytes(), b'another application')
        self.assertFalse((self.home / 'memory').exists())
        self.assertFalse((self.home / 'framework/tools/memory_runtime.py').exists())

    def test_global_owned_file_list_matches_installer(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('purge_test_installer', ROOT / 'global/install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        self.assertEqual(set(purge.GLOBAL_FILES), set(installer.FILES.values()) | {'memory-config.json'})

    def test_global_package_alone_purges_all_shared_project_notes(self):
        self.install()
        output = build_global_download.build(self.base / 'global.zip')
        with ZipFile(output) as archive:
            archive.extractall(self.base / 'global-unpack')
        package = self.base / 'global-unpack/jason-memory-global'
        self.run_cli(package / 'tools/purge_memory.py')
        self.assertFalse(self.home.exists())
        self.assertFalse((package / 'global/install.py').exists())
        self.assertTrue((self.project / '.jason-memory/MEMORY.md').exists())

    def test_custom_project_memory_path(self):
        (self.project / '.jason-memory').rename(self.project / 'my-memory')
        self.put(self.project / '.jason-memory.json', b'{"version":1,"mode":"project","memory_root":"my-memory"}')
        self.run_cli(self.project / 'tools/purge_memory.py')
        self.assertFalse((self.project / 'my-memory').exists())

    def test_modified_rules_are_reported_and_preserved(self):
        target = self.project / 'CLAUDE.md'
        original = target.read_bytes() + b'\n# User-added work rules\n'
        target.write_bytes(original)
        result = self.run_cli(self.project / 'tools/purge_memory.py', code=2)
        self.assertFalse(json.loads(result.stdout)['complete'])
        self.assertEqual(target.read_bytes(), original)
        self.assertIn('modified framework file', result.stdout)

    def test_project_mixed_hooks_and_backup_keep_unrelated_settings(self):
        import hook_settings
        target = self.project / '.claude/settings.json'
        base = b'{"model":"keep-model","hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo keep"}]}]}}'
        mixed = hook_settings.merged(base, 'python claude_memory_hook.py --jason-hook')
        target.write_bytes(mixed)
        backup = target.with_name('settings.json.' + 'a' * 32 + '.bak')
        backup.write_bytes(mixed)
        self.run_cli(self.project / 'tools/purge_memory.py')
        for path in (target, backup):
            self.assertEqual(json.loads(path.read_bytes()), json.loads(base))

    def test_missing_manifest_reports_incomplete_without_deleting_source(self):
        (self.project / 'uninstall-manifest.json').unlink()
        result = self.run_cli(self.project / 'tools/purge_memory.py', code=2)
        self.assertFalse(json.loads(result.stdout)['complete'])
        self.assertTrue((self.project / 'AGENTS.md').is_file())
        self.assertFalse((self.project / '.jason-memory').exists())

    def test_damaged_settings_or_block_prevents_any_deletion(self):
        for kind in ('settings', 'block'):
            with self.subTest(kind=kind):
                target = self.profile / '.claude' / ('settings.json' if kind == 'settings' else 'CLAUDE.md')
                self.put(target, b'{broken' if kind == 'settings' else purge.START)
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    purge.make_plan(self.args())
                self.assertEqual(before, self.snapshot())
                target.unlink()

    def test_traversal_or_protected_memory_root_rejected(self):
        for root in ('../outside', '.', '.claude', '.git', 'C:/Users', 'nested/../../outside'):
            with self.subTest(root=root):
                self.put(self.project / '.jason-memory.json', json.dumps({
                    'version': 1, 'mode': 'project', 'memory_root': root}).encode())
                with self.assertRaises(ValueError):
                    purge.make_plan(self.args())
                self.assertTrue((self.project / '.jason-memory/MEMORY.md').exists())

    def test_manifest_traversal_rejected_before_any_mutation(self):
        self.put(self.project / 'uninstall-manifest.json', json.dumps({
            'format': 'jason-memory-package-v1', 'files': {'../outside': 'x'}}).encode())
        before = self.snapshot()
        with self.assertRaises(ValueError):
            purge.make_plan(self.args())
        self.assertEqual(before, self.snapshot())

    def test_unidentified_global_folder_is_never_deleted(self):
        self.put(self.home / 'family.txt', b'private unrelated data')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'ownership'):
            purge.make_plan(self.args())
        self.assertEqual(before, self.snapshot())

    def test_changed_file_between_plan_and_execute_prevents_deletion(self):
        _, _, plan = purge.make_plan(self.args())
        (self.project / 'README.md').write_bytes(b'concurrent user edit')
        with self.assertRaisesRegex(ValueError, 'changed during preflight'):
            plan.execute()
        self.assertTrue((self.project / '.jason-memory/MEMORY.md').exists())

    def test_environment_config_paths_and_override_rule(self):
        codex, claude = self.base / 'custom codex', self.base / 'custom claude'
        block = purge.START + b'\n' + purge.RECALL + b'\ntext\n' + purge.END + b'\n'
        self.put(codex / 'AGENTS.override.md', block + b'user override\r\n')
        self.put(claude / 'CLAUDE.md', block + b'claude user rules\n')
        with patch.dict(os.environ, CODEX_HOME=str(codex), CLAUDE_CONFIG_DIR=str(claude)):
            _, _, plan = purge.make_plan(self.args(user_home=None, home=str(self.base / 'missing-home')))
        plan.execute()
        self.assertEqual((codex / 'AGENTS.override.md').read_bytes(), b'user override\r\n')
        self.assertEqual((claude / 'CLAUDE.md').read_bytes(), b'claude user rules\n')

    def test_junction_or_symlink_in_memory_is_rejected(self):
        outside = self.base / 'outside'
        outside.mkdir()
        self.put(outside / 'secret.txt', b'keep')
        link = self.project / '.jason-memory/redirected'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                                    capture_output=True)
            if result.returncode:
                self.skipTest('Junction creation unavailable')
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaisesRegex(ValueError, 'reparse|symlink'):
                purge.make_plan(self.args())
            self.assertEqual((outside / 'secret.txt').read_bytes(), b'keep')
            self.assertTrue((self.project / '.jason-memory/MEMORY.md').exists())
        finally:
            if os.name == 'nt':
                link.rmdir()  # Remove only this verified junction, not its target.
            else:
                link.unlink()

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher')
    def test_actual_bat_launcher_dry_run_and_purge(self):
        for args in (['--dry-run'], []):
            result = subprocess.run(['cmd', '/d', '/c', 'call', str(self.project / 'UNINSTALL.bat'),
                                     '--user-home', str(self.profile), *args],
                                    capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((self.project / '.jason-memory').exists(), bool(args))

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher')
    def test_double_click_defaults_with_isolated_windows_profile(self):
        env = dict(os.environ, USERPROFILE=str(self.profile),
                   CODEX_HOME=str(self.profile / '.codex'),
                   CLAUDE_CONFIG_DIR=str(self.profile / '.claude'))
        result = subprocess.run(['cmd', '/d', '/c', 'call', str(self.project / 'UNINSTALL.bat')],
                                input='\n', env=env, capture_output=True, text=True,
                                encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.project / '.jason-memory').exists())


if __name__ == '__main__':
    unittest.main()
