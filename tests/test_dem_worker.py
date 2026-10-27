"""Keep GDAL and h5py DLLs in separate processes, as in the application."""
from pathlib import Path
import os
import subprocess
import sys
import unittest

class DemWorkerSubprocessTests(unittest.TestCase):
    def test_native_dem_cases(self):
        script = Path(__file__).parent / 'native_checks' / 'dem_worker_cases.py'
        result = subprocess.run([sys.executable, '-B', str(script)], capture_output=True,
                                encoding='utf-8', errors='replace', timeout=120,
                                env={**os.environ, 'PYTHONUTF8': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == '__main__':
    unittest.main()
