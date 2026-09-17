import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('build_download', ROOT / 'tools/build_download.py')
download = importlib.util.module_from_spec(spec)
spec.loader.exec_module(download)


class DownloadTest(unittest.TestCase):
    def test_clean_package_works_without_git_and_excludes_private_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / 'source'
            private = uuid.uuid4().hex
            for name in download.PACKAGE_FILES:
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
            for name in ('.git/config', '.jason-memory/private.md',
                         '.jason-memory/MEMORY.md', 'private.xlsx'):
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(private, encoding='utf-8')
            # The builder must not need any command-line tool, including Git.
            with patch('subprocess.Popen', side_effect=AssertionError('No external commands')):
                archive = download.build(folder / 'ready.zip', source)
            with ZipFile(archive) as zipfile:
                names = zipfile.namelist()
                self.assertNotIn('jason-memory/.git/config', names)
                self.assertNotIn('jason-memory/private.xlsx', names)
                self.assertEqual([n for n in names if '/.jason-memory/' in n],
                                 ['jason-memory/.jason-memory/MEMORY.md'])
                self.assertTrue(all(private.encode() not in zipfile.read(n) for n in names))
                zipfile.extractall(folder / 'unpacked')
            project = folder / 'unpacked/jason-memory'
            # The downloaded Claude entrance must resolve to the packaged blank index.
            entrance = project / 'CLAUDE.md'
            imports = [line[1:] for line in entrance.read_text(encoding='utf-8').splitlines()
                       if line.startswith('@')]
            self.assertEqual(imports, ['.jason-memory/MEMORY.md'])
            self.assertEqual((entrance.parent / imports[0]).read_bytes(),
                             (project / 'templates/MEMORY.md').read_bytes())
            for tool, argument in [('jason_check.py', '.jason-memory/MEMORY.md'),
                                   ('jason_doctor.py', '.jason-memory')]:
                result = subprocess.run([sys.executable, '-X', 'utf8', 'tools/' + tool, argument],
                                        cwd=project, capture_output=True, text=True,
                                        encoding='utf-8', timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((project / '.git').exists())
            self.assertEqual((source / '.jason-memory/MEMORY.md').read_text(), private)

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'existing.zip'
            path.write_bytes(b'keep-existing-data')
            with self.assertRaises(FileExistsError):
                download.build(path)
            self.assertEqual(path.read_bytes(), b'keep-existing-data')


if __name__ == '__main__':
    unittest.main()
