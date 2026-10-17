#!/usr/bin/python3
"""Archive Linux application sources, tests, notices and required boot assets."""
import argparse
import gzip
import io
import os
from pathlib import Path
import re
import subprocess
import tarfile


def source_epoch(repo):
    value = os.environ.get('SOURCE_DATE_EPOCH')
    if value is None and (repo / 'SOURCE_DATE_EPOCH').is_file():
        value = (repo / 'SOURCE_DATE_EPOCH').read_text().strip()
    if value is None:
        value = subprocess.check_output(['git', '-C', str(repo), 'log', '-1', '--format=%ct'], text=True).strip()
    epoch = int(value)
    if epoch < 0:
        raise ValueError('SOURCE_DATE_EPOCH must be nonnegative')
    return epoch


def path_is_epoch(directory, name, base):
    return Path(directory) / name == base / 'SOURCE_DATE_EPOCH'


def main():
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=base / 'dist')
    parser.add_argument('--version', default='0.4.0')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9][A-Za-z0-9.+~]*', args.version):
        parser.error('Invalid version')
    repo = base
    epoch = source_epoch(repo)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = args.output_dir.resolve()
    if output_dir == repo:
        parser.error('Output directory must not be the source root')
    output = output_dir / f'lifus-{args.version}-source.tar.gz'
    excluded = {'.git', 'dist', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.venv', 'venv'}
    with output.open('wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
                data = str(epoch).encode() + b'\n'
                info = tarfile.TarInfo(f'lifus-{args.version}/SOURCE_DATE_EPOCH')
                info.size, info.mtime, info.mode = len(data), epoch, 0o644
                archive.addfile(info, io.BytesIO(data))
                for directory, dirs, files in os.walk(repo):
                    dirs[:] = sorted(d for d in dirs if d not in excluded
                                     and (Path(directory) != repo or d in {'lifus', 'docs', 'tests', 'upstream', 'packaging'})
                                     and (Path(directory) / d).resolve() != output_dir)
                    for name in sorted(files):
                        if Path(directory) == repo and name not in {'README.md', 'LICENSE.txt', 'COPYING.md', '.gitignore', '.gitattributes', 'build-deb.py', 'build-source.py', 'lifus.desktop'}:
                            continue
                        if path_is_epoch(directory, name, base) or name in excluded or name.endswith(('.pyc', '.pyo')):
                            continue
                        path = Path(directory) / name
                        if path.resolve() == output:
                            continue
                        info = archive.gettarinfo(str(path), arcname=f'lifus-{args.version}/' + str(path.relative_to(repo)))
                        info.uid = info.gid = 0
                        info.uname = info.gname = ''
                        info.mtime = epoch
                        info.pax_headers = {}
                        if info.isfile():
                            info.mode = 0o755 if info.mode & 0o111 else 0o644
                            with path.open('rb') as stream:
                                archive.addfile(info, stream)
                        else:
                            archive.addfile(info)
    print(output)


if __name__ == '__main__':
    main()
