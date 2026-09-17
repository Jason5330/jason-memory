import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'global/global_store.py'


def note(slug, fact='全局回報先寫結論'):
    return ('---\nname: ' + slug + '\ndescription: 回報順序\ntype: feedback\n'
            'created: 2026-09-17\nupdated: 2026-09-17\n---\n\n' + fact +
            '\n\nWhy: 使用者明確要求。\n\nHow to apply: 按指定範圍使用。\n')


class GlobalStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'shared home'
        self.a = self.root / 'project A'
        self.b = self.root / 'project B'
        self.a.mkdir()
        self.b.mkdir()

    def run_cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), '--home', str(self.home), *map(str,args)],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def save(self, slug, scope='global', project=None, content=None, sha=None):
        source = self.root / (slug + '-input.md')
        source.write_text(content or note(slug), encoding='utf-8')
        args = ['save', '--scope', scope, '--project', project or self.a,
                '--slug', slug, '--input', source, '--summary', '回報偏好']
        if sha:
            args += ['--expected-sha', sha]
        return self.run_cli(*args)

    def test_global_recall_and_project_isolation(self):
        first = self.run_cli('context','--project',self.a)
        self.save('reply-style')
        self.save('customer-a', 'project')
        other = self.run_cli('context','--project',self.b)
        self.assertEqual(first['global_store'],other['global_store'])
        self.assertNotEqual(first['project_store'],other['project_store'])
        self.assertIn('reply-style.md',other['global_index'])
        self.assertNotIn('customer-a.md',other['project_index'])
        read = self.run_cli('read','--scope','global','--project',self.b,'--slug','reply-style')
        self.assertIn('先寫結論',read['content'])
        self.assertFalse((self.a / '.git').exists())
        self.assertFalse((self.a / '.jason-memory').exists())

    def test_stale_update_does_not_overwrite_newer_fact(self):
        first = self.save('reply-style')
        new = self.save('reply-style',content=note('reply-style','改成先列風險'),sha=first['sha256'])
        source = self.root / 'stale.md'
        source.write_text(note('reply-style','過時內容'),encoding='utf-8')
        self.run_cli('save','--scope','global','--project',self.a,'--slug','reply-style',
                     '--input',source,'--summary','舊資訊','--expected-sha',first['sha256'],ok=False)
        read = self.run_cli('read','--scope','global','--project',self.b,'--slug','reply-style')
        self.assertEqual(read['sha256'],new['sha256'])
        self.assertIn('先列風險',read['content'])

    def test_identical_save_is_noop(self):
        first = self.save('reply-style')
        self.assertTrue(first['changed'])
        self.assertFalse(self.save('reply-style')['changed'])
        context = self.run_cli('context','--project',self.a)
        self.assertEqual(context['global_index'].count('](reply-style.md)'),1)

    def test_duplicate_after_another_fact_is_still_noop(self):
        self.save('first')
        self.save('second')
        self.assertFalse(self.save('first')['changed'])

    def test_retire_removes_active_rule_but_preserves_archive(self):
        saved = self.save('reply-style')
        result = self.run_cli('retire','--scope','global','--project',self.a,'--slug','reply-style',
                              '--expected-sha',saved['sha256'])
        context = self.run_cli('context','--project',self.b)
        self.assertNotIn('](reply-style.md)',context['global_index'])
        self.assertFalse(Path(saved['path']).exists())
        self.assertEqual(Path(result['archive_path']).read_text(encoding='utf-8'),note('reply-style'))
        self.assertIn('archive/MEMORY.md',context['global_index'])
        manifest = Path(result['archive_path']).parent / 'MEMORY.md'
        self.assertIn(Path(result['archive_path']).name,manifest.read_text(encoding='utf-8'))

    def test_json_escaping_does_not_leave_unrecoverable_journal(self):
        self.save('escaped',content=note('escaped','\\' * 3200000))
        context = self.run_cli('context','--project',self.a)
        self.assertIn('escaped.md',context['global_index'])

    def test_global_scope_rejects_project_type(self):
        source = self.root / 'project.md'
        source.write_text(note('only-project').replace('type: feedback','type: project'),encoding='utf-8')
        self.run_cli('save','--scope','global','--project',self.a,'--slug','only-project',
                     '--input',source,'--summary','project',ok=False)

    def test_reject_invalid_schema_path_and_missing_expected_revision(self):
        saved = self.save('reply-style')
        source = self.root / 'bad.md'
        source.write_text('not a valid note',encoding='utf-8')
        self.run_cli('save','--scope','global','--project',self.a,'--slug','bad',
                     '--input',source,'--summary','bad',ok=False)
        self.run_cli('read','--scope','global','--project',self.a,'--slug','../outside',ok=False)
        source.write_text(note('reply-style','different'),encoding='utf-8')
        self.run_cli('save','--scope','global','--project',self.a,'--slug','reply-style',
                     '--input',source,'--summary','different',ok=False)
        read = self.run_cli('read','--scope','global','--project',self.a,'--slug','reply-style')
        self.assertEqual(read['sha256'],saved['sha256'])

    def test_two_processes_preserve_both_index_entries(self):
        processes = []
        for slug in ('agent-a','agent-b','agent-c','agent-d'):
            source = self.root / (slug + '.md')
            source.write_text(note(slug),encoding='utf-8')
            processes.append(subprocess.Popen([sys.executable,str(SCRIPT),'--home',str(self.home),
                'save','--scope','global','--project',str(self.a),'--slug',slug,'--input',str(source),
                '--summary',slug],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8'))
        results = [(process, *process.communicate(timeout=30)) for process in processes]
        for process,out,err in results:
            self.assertEqual(process.returncode,0,out+err)
        index = self.run_cli('context','--project',self.a)['global_index']
        for slug in ('agent-a','agent-b','agent-c','agent-d'):
            self.assertEqual(index.count('](' + slug + '.md)'),1)

    def test_context_recovers_interrupted_note_index_commit(self):
        ctx = self.run_cli('context','--project',self.a)
        store = Path(ctx['global_store'])
        content = note('recovered')
        index = ctx['global_index'] + '\n- [Recovered](recovered.md) — 回復寫入\n'
        (store / '.pending.json').write_text(json.dumps({'slug':'recovered','content':content,'index':index}),encoding='utf-8')
        (store / 'recovered.md').write_text(content,encoding='utf-8')
        context = self.run_cli('context','--project',self.a)
        self.assertIn('recovered.md',context['global_index'])
        self.assertFalse((store / '.pending.json').exists())

    def test_retire_recovers_at_each_write_boundary(self):
        spec = importlib.util.spec_from_file_location('global_store_fault_test',SCRIPT)
        writer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(writer)
        for boundary in ('journal','archive','manifest','index'):
            with self.subTest(boundary=boundary):
                self.home = self.root / ('interrupted-' + boundary)
                saved = self.save('reply-style')
                store = Path(saved['path']).parent.resolve()
                targets = {'journal':store / '.pending.json',
                           'archive':store / 'archive' / ('reply-style-' + saved['sha256'] + '.md'),
                           'manifest':store / 'archive/MEMORY.md', 'index':store / 'MEMORY.md'}
                original_write = writer.atomic_write
                def interrupted(path, text):
                    original_write(path,text)
                    if path == targets[boundary]:
                        raise RuntimeError('simulated process interruption')
                with patch.object(writer,'atomic_write',side_effect=interrupted):
                    with self.assertRaises(RuntimeError):
                        writer.retire_note(store,'reply-style',saved['sha256'])
                ctx = self.run_cli('context','--project',self.a)
                self.assertIn('archive/MEMORY.md',ctx['global_index'])
                self.assertNotIn('](reply-style.md)',ctx['global_index'])
                self.assertFalse((store / 'reply-style.md').exists())
                self.assertFalse((store / '.pending.json').exists())
                self.assertEqual(targets['archive'].read_text(encoding='utf-8'),note('reply-style'))
                self.save('reply-style')

    def test_symlink_note_escape_is_rejected(self):
        ctx = self.run_cli('context','--project',self.a)
        outside = self.root / 'outside.md'
        outside.write_text(note('outside'),encoding='utf-8')
        link = Path(ctx['global_store']) / 'outside.md'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('Host does not permit test symlinks')
        result = self.run_cli('read','--scope','global','--project',self.a,'--slug','outside',ok=False)
        self.assertIn('Redirected memory path',result.stderr)
        self.assertEqual(outside.read_text(encoding='utf-8'),note('outside'))


if __name__ == '__main__':
    unittest.main()
