"""Reject same-version release assets built from different source inputs."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_runtime import VERSION, REVISION
from scripts.build_release import source_identity, source_files
from scripts.verify_release_set import verify


class ReleaseIdentityTests(unittest.TestCase):
    def test_source_identity_is_stable_and_excludes_private_state(self):
        identity = source_identity()
        self.assertEqual(identity, source_identity())
        self.assertEqual(identity['version'], '1.0')
        self.assertEqual(len(identity['sourceTreeSha256']), 64)
        for path in source_files():
            self.assertFalse(any(part in ('.runtime', '.verification', 'release-artifacts') for part in path.relative_to(ROOT).parts))

    def assets(self, root, *, mismatch=False):
        files = [('app_runtime.py', b'fixture source')]
        entries = [(name, hashlib.sha256(data).hexdigest()) for name, data in files]
        fingerprint = hashlib.sha256(json.dumps(entries, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        info = {'version': VERSION, 'revision': REVISION, 'sourceTreeSha256': fingerprint, 'sourceFileCount': 1}
        with zipfile.ZipFile(root / f'SAR-v{VERSION}-source.zip', 'w') as archive:
            for name, data in files:
                archive.writestr(f'SAR-v{VERSION}-source/' + name, data)
        with zipfile.ZipFile(root / f'SAR-v{VERSION}-windows-x64.zip', 'w') as archive:
            archive.writestr('SAR/_internal/build-info.json', json.dumps(info))
        if mismatch:
            info['sourceTreeSha256'] = '0' * 64
        data = json.dumps(info).encode()
        with tarfile.open(root / f'SAR-v{VERSION}-linux-x86_64.tar.gz', 'w:gz') as archive:
            member = tarfile.TarInfo('SAR/_internal/build-info.json')
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))

    def test_matching_assets_produce_one_manifest_and_checksums(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            root = Path(folder)
            self.assets(root)
            result = verify(root)
            self.assertEqual(len(result['artifacts']), 3)
            self.assertEqual(len((root / 'SHA256SUMS.txt').read_text().splitlines()), 3)
            self.assertEqual(json.loads((root / 'RELEASE_MANIFEST.json').read_text()), result)

    def test_same_version_with_different_source_fails_before_publishing_manifest(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests') as folder:
            root = Path(folder)
            self.assets(root, mismatch=True)
            with self.assertRaisesRegex(ValueError, 'does not match'):
                verify(root)
            self.assertFalse((root / 'RELEASE_MANIFEST.json').exists())


if __name__ == '__main__':
    unittest.main()
