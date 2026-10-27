"""Crop directory, batch metadata and persistent user summaries."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from download_hub import DownloadHub
from earthdata_auth import EarthdataAuth
from nisar_summary import HEADER


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.root = Path(self.temp.name)
        self.output = self.root / '测试 目录'
        self.auth = EarthdataAuth()
        self.hub = DownloadHub(self.auth, self.root / 'state')
        self.hub.configure({'output': str(self.output), 'concurrent': 2})

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def payload(self, suffix='ONE', direction='ASCENDING'):
        name = 'NISAR_L1_' + suffix + '_RSLC'
        return {'scenes': [{'id': name, 'dataset': 'NISAR', 'product': 'RSLC',
                            'url': 'https://datapool.asf.alaska.edu/TEST/' + name + '.h5',
                            'direction': direction, 'startTime': '2026-10-20T01:02:03Z',
                            'stopTime': '2026-10-20T01:02:30Z', 'path': 23, 'frame': 100}],
                'corners': [[86.79, 43.12], [86.81, 43.12], [86.81, 43.10], [86.79, 43.10]],
                'frequencies': ['A'], 'polarizations': ['HH'], 'includeDEM': False}

    def test_crop_subdirectory_and_summary_have_real_batch_metadata(self):
        first = self.payload()
        first['scenes'] += self.payload('TWO', 'DESCENDING')['scenes']
        result = self.hub.add_subset(first)
        self.assertTrue(all(Path(task['target']).parent == self.output / 'rslc' for task in result['tasks']))
        text = (self.output / 'summary.txt').read_text(encoding='utf-8')
        for value in (HEADER, '升轨 1 景', '降轨 1 景', '86.79 至 86.81', '43.10 至 43.12',
                      'Track：23', 'Frame：100', '2026-10-20T01:02:03Z', '频率：A；极化：HH', '待下载'):
            self.assertIn(value, text)
        self.assertIn('已完成且文件存在：0 景', text)
        self.assertNotIn('https://', text)

    def test_completed_history_survives_removal_restart_and_later_batches(self):
        ident = self.hub.add_subset(self.payload())['ids'][0]
        task = next(iter(self.hub.subsets.tasks.values()))
        file = Path(task['output'])
        file.parent.mkdir()
        file.write_bytes(b'completed fixture')
        task['status'] = 'complete'
        self.hub.subsets.save()
        self.hub.action('remove', [ident])
        self.hub.close()
        self.hub = DownloadHub(self.auth, self.root / 'state')
        self.hub.add_subset(self.payload('TWO', 'DESCENDING'))
        text = (self.output / 'summary.txt').read_text(encoding='utf-8')
        self.assertIn('NISAR_L1_ONE_RSLC', text)
        self.assertIn('NISAR_L1_TWO_RSLC', text)
        self.assertIn('已完成且文件存在：1 景', text)

    def test_unknown_or_unwritable_summary_warns_without_overwriting_or_losing_tasks(self):
        self.output.mkdir()
        summary = self.output / 'summary.txt'
        summary.write_text('my own notes', encoding='utf-8')
        result = self.hub.add_subset(self.payload())
        self.assertIn('未覆盖', result['warning'])
        self.assertEqual(summary.read_text(encoding='utf-8'), 'my own notes')
        self.assertEqual(result['added'], 1)
        summary.unlink()
        with patch('nisar_summary.tempfile.NamedTemporaryFile', side_effect=PermissionError('fixture')):
            self.hub.subsets.save()
        self.assertIn('下载摘要未更新', self.hub.snapshot()['warning'])
        self.hub.subsets.save()
        self.assertTrue(summary.read_text(encoding='utf-8').startswith(HEADER))

    def test_old_task_location_and_download_signature_are_preserved(self):
        self.hub.add_subset(self.payload())
        jobs = self.hub.subsets
        task = next(iter(jobs.tasks.values()))
        task['output'] = str(self.output / Path(task['output']).name)
        task.pop('summary_root')
        jobs.summary_records.clear()
        jobs.save()
        self.hub.close()
        self.hub = DownloadHub(self.auth, self.root / 'state')
        result = self.hub.add_subset(self.payload())
        self.assertEqual(result['added'], 0)
        self.assertEqual(Path(result['tasks'][0]['target']).parent, self.output)


if __name__ == '__main__':
    unittest.main()
