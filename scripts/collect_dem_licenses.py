"""Collect license texts from the engine's exact conda/pip inputs."""
from importlib import metadata
import json
from pathlib import Path
import shutil
import sys


def license_file(path):
    return (path.is_file() and '__pycache__' not in path.parts
            and path.suffix.lower() not in ('.py', '.pyc', '.pyo', '.pyd', '.so', '.dll', '.exe'))


def collect(target):
    target.mkdir(parents=True,exist_ok=True)
    inventory=[]
    for manifest in sorted((Path(sys.prefix)/'conda-meta').glob('*.json')):
        record=json.loads(manifest.read_text(encoding='utf-8'))
        # A project-local pip wheel may replace a conda Python package for the
        # build. Its actual metadata and license are collected below instead.
        try:
            if metadata.version(record['name']) != record['version']:
                continue
        except metadata.PackageNotFoundError:
            pass
        info={key:record.get(key) for key in ('name','version','build','license')}
        inventory.append(info)
        folder=target/record['name'];folder.mkdir(exist_ok=True)
        (folder/'PACKAGE.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
        source=Path(record.get('link',{}).get('source',''))/'info'/'licenses'
        if source.is_dir():
            shutil.copytree(source,folder/'texts',dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__','*.py','*.pyc','*.pyo','*.pyd','*.so','*.dll','*.exe'))
        for name in record.get('files',[]):
            file=Path(sys.prefix)/name
            if license_file(file) and ('/licenses/' in '/'+name.lower() or file.name.lower().startswith(('license','copying','copyright'))):
                destination=folder/'installed'/name;destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(file,destination)
    for name in ('sardem','GDAL','numpy','requests','certifi','charset-normalizer','idna','urllib3',
                 'pyinstaller','pyinstaller-hooks-contrib'):
        distribution=metadata.distribution(name);folder=target/name;folder.mkdir(exist_ok=True)
        (folder/'PACKAGE.txt').write_text(f'{name} {distribution.version}\n',encoding='utf-8')
        for index,file in enumerate(distribution.files or []):
            if Path(file).name.lower().startswith(('license','copying','notice')) or '/licenses/' in file.as_posix():
                path=Path(distribution.locate_file(file))
                if license_file(path):shutil.copyfile(path,folder/f'{index}-{path.name}')
    (target/'engine-packages.json').write_text(json.dumps(inventory,indent=2),encoding='utf-8')


if __name__=='__main__':collect(Path(sys.argv[1]).resolve())
