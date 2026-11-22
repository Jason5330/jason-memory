import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import memory_dream as dream
import memory_ledger as ledger
import memory_runtime as runtime


class ChunkIndexTests(unittest.TestCase):
    def check_next_prompt(self, staged):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp).resolve()

            def config(name):
                path = project / (name + '.json')
                path.write_text(json.dumps({'version': 1, 'mode': 'global', 'home': name,
                                            'ledger': {'foreground_writes': True}}), encoding='utf-8')
                return path

            source = config('live')
            target = config('staging') if staged else source
            with runtime.opened(source, project) as (_, _, roots):
                for text in ('Use the first header', 'Correct the previous header'):
                    ledger.append(roots['project'], 'prompt', {'text': text})
            indexes = []

            def worker(command, prompt):
                index = prompt.split('## 現有索引\n\n', 1)[1].split('\n\n## 原始紀錄', 1)[0]
                indexes.append(index)
                if len(indexes) == 1:
                    with runtime.opened(target, project) as (_, _, roots):
                        for scope, root in roots.items():
                            runtime.remember(root, 'report', 'header', 'QA-header-' + scope,
                                             'User requested it', scope=scope, kind='feedback')
                return 0

            with patch.object(dream, 'CHUNK_CAP', 1):
                code = dream.consolidate(source, project, 1, 2, ledger.settings(source), worker, target)
            self.assertEqual(code, 0)
            self.assertEqual(len(indexes), 2)
            for scope in ('global', 'project'):
                self.assertNotIn('QA-header-' + scope, indexes[0])
                self.assertIn('QA-header-' + scope, indexes[1])

    def test_next_chunk_sees_notes_written_in_previous_chunk(self):
        self.check_next_prompt(staged=False)

    def test_next_chunk_sees_rebuild_staging_index(self):
        self.check_next_prompt(staged=True)


if __name__ == '__main__':
    unittest.main()
