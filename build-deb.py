#!/usr/bin/python3
"""Build an architecture-independent Debian package without installing anything."""
import argparse
import os
import runpy
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent / 'dist')
    parser.add_argument('--version', default='0.4.0')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9][A-Za-z0-9.+~]*', args.version):
        parser.error('Version must be a Debian upstream version without a revision')
    base = Path(__file__).resolve().parent
    repo = base
    epoch = runpy.run_path(str(base / 'build-source.py'))['source_epoch'](repo)
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir.resolve() / f'lifus_{args.version}-1_all.deb'
    with tempfile.TemporaryDirectory(prefix='lifus-deb-') as temp:
        root = Path(temp)
        def copy(source, target):
            dest = root / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            dest.chmod(0o644)
        shutil.copytree(base / 'lifus', root / 'usr/share/lifus/lifus', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        copy(base / 'packaging/lifus', 'usr/bin/lifus')
        (root / 'usr/bin/lifus').chmod(0o755)
        copy(base / 'packaging/lifus-privileged.py', 'usr/share/lifus/lifus-privileged.py')
        copy(base / 'packaging/lifus-askpass', 'usr/libexec/lifus-askpass')
        (root / 'usr/libexec/lifus-askpass').chmod(0o755)
        copy(base / 'lifus.desktop', 'usr/share/applications/lifus.desktop')
        copy(base / 'packaging/lifus.svg', 'usr/share/icons/hicolor/scalable/apps/lifus.svg')
        copy(repo / 'COPYING.md', 'usr/share/doc/lifus/copyright')
        copy(repo / 'COPYING.md', 'usr/share/doc/lifus/COPYING.md')
        copy(repo / 'LICENSE.txt', 'usr/share/doc/lifus/LICENSE.txt')
        copy(base / 'README.md', 'usr/share/doc/lifus/README.md')
        copy(base / 'docs/FEATURES.md', 'usr/share/doc/lifus/FEATURES.md')
        copy(base / 'docs/SOURCE.md', 'usr/share/doc/lifus/SOURCE.md')
        for name in ('WINDOWS_CONFIG.md', 'WINDOWS_STAGING.md', 'CONTAINERS.md', 'VALIDATION.md', 'UNMOUNT.md'):
            copy(base / 'docs' / name, 'usr/share/doc/lifus/' + name)
        upstream = base / 'upstream'
        for path in sorted(upstream.rglob('*')):
            if path.is_file():
                copy(path, 'usr/share/lifus/upstream/' + str(path.relative_to(upstream)))
        control = root / 'DEBIAN/control'
        control.parent.mkdir()
        control.write_text(f'''Package: lifus
Version: {args.version}-1
Section: utils
Priority: optional
Architecture: all
Maintainer: Lifus contributors <lifus@localhost>
Depends: python3 (>= 3.10), python3-pyside6.qtcore, python3-pyside6.qtgui, python3-pyside6.qtwidgets, python3-tk, sudo, util-linux, fdisk, parted, dosfstools, e2fsprogs, exfatprogs, ntfs-3g, mtools, p7zip-full, wimtools
Description: Unofficial native Linux Rufus-inspired USB utility
 Native PySide6/Qt and CLI application for image writing, verification, backup,
 FreeDOS image creation and guarded advanced disk operations.
 This is not an official Rufus release or a full feature-parity port.
''', encoding='utf-8')
        for path in [*root.rglob('*'), root]:
            if path.is_dir():
                path.chmod(0o755)
            elif path not in (root / 'usr/bin/lifus', root / 'usr/libexec/lifus-askpass'):
                path.chmod(0o644)
            os.utime(path, (epoch, epoch), follow_symlinks=False)
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(root), str(output)], check=True, env=env)
    print(output)


if __name__ == '__main__':
    main()
