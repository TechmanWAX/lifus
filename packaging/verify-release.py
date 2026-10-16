#!/usr/bin/python3
"""Validate release contents, packaged tests and Git-free reproducibility."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', args.version):
        parser.error('Version must be X.Y.Z')
    root = Path(__file__).resolve().parents[1]
    dist = root / 'dist'
    deb = dist / f'lifus_{args.version}-1_all.deb'
    source = dist / f'lifus-{args.version}-source.tar.gz'
    with tempfile.TemporaryDirectory(prefix='lifus-release-check-') as tmp:
        tmp = Path(tmp)
        package = tmp / 'package'
        run('dpkg-deb', '-x', str(deb), str(package))
        module_root = package / 'usr/share/lifus'
        for path in (root / 'lifus').glob('*.py'):
            if path.read_bytes() != (module_root / 'lifus' / path.name).read_bytes():
                raise RuntimeError('Packaged module mismatch: ' + path.name)
        env = dict(os.environ, PYTHONPATH=str(module_root))
        run('/usr/bin/python3', '-m', 'unittest', 'discover', '-s', str(root / 'tests'), '-v',
            cwd=tmp, env=env)
        unpacked = tmp / 'source'
        with tarfile.open(source) as archive:
            for member in archive.getmembers():
                parts = Path(member.name).parts
                if any(p in {'.git', 'dist', '__pycache__', '.venv', 'venv'} or
                       p == '.env' or p.startswith('.env.') for p in parts):
                    raise RuntimeError('Unexpected archive member: ' + member.name)
            archive.extractall(unpacked, filter='data')
        checkout = unpacked / f'lifus-{args.version}'
        env = dict(os.environ)
        env.pop('SOURCE_DATE_EPOCH', None)
        run('/usr/bin/python3', str(checkout / 'build-deb.py'), '--version', args.version,
            cwd=checkout, env=env)
        rebuilt = checkout / 'dist' / deb.name
        if deb.read_bytes() != rebuilt.read_bytes():
            raise RuntimeError('Git-free source rebuild is not byte-identical')
    lines = [hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name
             for p in (deb, source)]
    (dist / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')
    print('Verified packaged modules, tests and reproducible Git-free rebuild.')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
