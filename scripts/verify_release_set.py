"""Verify that source and both portable programs were built from the same inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_runtime import VERSION, REVISION


def sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def verify(directory):
    directory = Path(directory)
    source = directory / f'SAR-v{VERSION}-source.zip'
    prefix = f'SAR-v{VERSION}-source/'
    with zipfile.ZipFile(source) as archive:
        names = [name for name in archive.namelist() if not name.endswith('/')]
        if len(names) != len(set(names)) or any(not name.startswith(prefix) or '..' in Path(name).parts for name in names):
            raise ValueError('Invalid source archive entries')
        entries = sorted((name.removeprefix(prefix), hashlib.sha256(archive.read(name)).hexdigest()) for name in names)
    fingerprint = hashlib.sha256(json.dumps(entries, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    expected = {'version': VERSION, 'revision': REVISION, 'sourceTreeSha256': fingerprint, 'sourceFileCount': len(entries)}
    artifacts = [source]
    for suffix in ('windows-x64.zip', 'linux-x86_64.tar.gz'):
        path = directory / f'SAR-v{VERSION}-{suffix}'
        member = 'SAR/_internal/build-info.json'
        if suffix.endswith('.zip'):
            with zipfile.ZipFile(path) as archive:
                actual = json.loads(archive.read(member))
        else:
            with tarfile.open(path) as archive:
                with archive.extractfile(member) as stream:
                    actual = json.load(stream)
        if actual != expected:
            raise ValueError(f'{path.name} does not match the source archive')
        artifacts.append(path)
    report = {**expected, 'artifacts': [{'name': path.name, 'bytes': path.stat().st_size, 'sha256': sha256(path)} for path in artifacts]}
    (directory / 'RELEASE_MANIFEST.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    (directory / 'SHA256SUMS.txt').write_text(''.join(item['sha256'] + '  ' + item['name'] + '\n' for item in report['artifacts']), encoding='ascii')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    print(json.dumps(verify(parser.parse_args().directory), indent=2))
