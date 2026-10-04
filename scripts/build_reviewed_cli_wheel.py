"""Build only the reviewed wheel; never publish, sign, or run a model."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

VERSION = '0.5.291'
SHA256 = 'd77d1673b6c13524c4b97aaf4d98f71390943b3def9579022ec9b6a325e1caff'
SIZE = 913779
BACKEND_VERSION = '1.32.4'
FILENAME = f'dradar-{VERSION}-py3-none-any.whl'


def verify_wheel(source, wheel):
    body = wheel.read_bytes()
    if wheel.name != FILENAME or len(body) != SIZE or hashlib.sha256(body).hexdigest() != SHA256:
        raise ValueError('Reviewed wheel name, size or SHA256 mismatch')
    source_files = {p.relative_to(source / 'src').as_posix(): p for p in
                    (source / 'src/dradar').rglob('*') if p.is_file()
                    and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    with zipfile.ZipFile(wheel) as archive:
        package_names = {n for n in archive.namelist() if n.startswith('dradar/') and not n.endswith('/')}
        if package_names != set(source_files):
            raise ValueError('Reviewed wheel/source package file set differs')
        for name, path in source_files.items():
            if archive.read(name) != path.read_bytes():
                raise ValueError('Reviewed wheel/source byte mismatch: ' + name)
    return {'wheel': FILENAME, 'sha256': SHA256, 'bytes': SIZE,
            'package_files_matched': len(source_files), 'model_calls': 0,
            'publication_calls': 0, 'signing_calls': 0}


def build(source, output):
    version = tomllib.loads((source / 'pyproject.toml').read_text())['project']['version']
    if version != VERSION or (source / 'src/dradar/__init__.py').read_text() != f'__version__ = "{VERSION}"\n':
        raise ValueError('Source version is not the reviewed stable candidate')
    if importlib.metadata.version('hatchling') != BACKEND_VERSION:
        raise ValueError('Use the pinned reviewed hatchling backend')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Build output must be empty; preserve existing artifacts')
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'hatchling', 'build', '-t', 'wheel', '-d', str(output)],
                   cwd=source, check=True)
    result = verify_wheel(source, output / FILENAME)
    (output / 'BUILD_RECEIPT.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path('.'))
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source_root.resolve(), args.output_dir.resolve())))


if __name__ == '__main__':
    main()
