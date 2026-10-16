"""Build a FreeDOS BIOS disk image using the original Rufus boot assets.

The output is a NEW regular file. Physical-device access is intentionally left
 to the separately validated writer. GPL-3.0-or-later; boot code derives from
Rufus' ms-sys (GPL-2.0-or-later). FreeDOS binaries retain upstream licenses.
"""
from pathlib import Path
import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile


def _assets():
    # The checkout and extracted Debian package use the same sibling layout.
    candidates = [Path(__file__).resolve().parent.parent / 'upstream',
                  Path('/usr/share/lifus/upstream')]
    for path in candidates:
        if (path / 'res/freedos/KERNEL.SYS').is_file():
            return path
    raise ValueError('Rufus FreeDOS resources missing; reinstall lifus')


def _header(path):
    text = path.read_text(encoding='utf-8')
    body = text.split('{', 1)[1].split('}', 1)[0]
    body = re.sub(r'/\*.*?\*/|//[^\n]*', '', body, flags=re.S)
    return bytes(int(value, 16) for value in re.findall(r'0x([0-9a-fA-F]{1,2})\b', body))


def create_image(destination, *, size_mib=64, label='FREEDOS', progress=None, cancel=None):
    """Atomically publish a new FAT16/MBR FreeDOS image, without root or mounts."""
    if isinstance(size_mib, bool) or not isinstance(size_mib, int) or not 32 <= size_mib <= 2048:
        raise ValueError('FreeDOS image size must be an integer from 32 to 2048 MiB')
    if not re.fullmatch(r'[A-Za-z0-9_ -]{1,11}', label):
        raise ValueError('FAT label: 1–11 ASCII letters, digits, spaces, underscores or hyphens')
    dest = Path(os.path.abspath(os.fspath(destination)))
    if os.path.lexists(dest):
        raise FileExistsError('Destination already exists; never overwriting it')
    if not dest.parent.is_dir():
        raise ValueError('Destination directory does not exist')
    tools = {}
    for name in ('mkfs.fat', 'mcopy'):
        tools[name] = shutil.which(name)
        if not tools[name]:
            raise ValueError('Missing '+name+'; install dosfstools and mtools')
    root = _assets()
    headers = root / 'src/ms-sys/inc'
    mbr_code = _header(headers / 'mbr_win7.h')
    pbr_start = _header(headers / 'br_fat16_0x0.h')
    pbr_code = _header(headers / 'br_fat16fd_0x3e.h')
    if len(mbr_code) < 440 or len(pbr_start) != 11 or len(pbr_code) != 450:
        raise ValueError('Unexpected upstream boot-code layout')
    size = size_mib * 1024 * 1024
    offset = 1024 * 1024

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise InterruptedError('FreeDOS creation cancelled; destination not published')

    def run(args):
        check_cancel()
        completed = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if completed.returncode:
            raise ValueError(completed.stderr.strip() or completed.stdout.strip() or 'Tool failed')
        check_cancel()

    check_cancel()
    with tempfile.TemporaryDirectory(prefix='.lifus-freedos-', dir=dest.parent) as td:
        partition = Path(td) / 'fat.img'
        disk = Path(td) / 'disk.img'
        with partition.open('xb') as f:
            f.truncate(size - offset)
        run([tools['mkfs.fat'], '-F', '16', '-h', '2048', '-n', label.upper(), str(partition)])
        resources = root / 'res/freedos'
        # KERNEL first is intentional; include the matching command interpreter.
        for filename in ('KERNEL.SYS', 'COMMAND.COM'):
            run([tools['mcopy'], '-i', str(partition), str(resources / filename), '::'+filename])
        autoexec = Path(td) / 'AUTOEXEC.BAT'
        autoexec.write_bytes(b'@ECHO OFF\r\nECHO FreeDOS - Lifus native Linux build\r\n')
        run([tools['mcopy'], '-i', str(partition), str(autoexec), '::AUTOEXEC.BAT'])
        with partition.open('r+b') as f:
            f.write(pbr_start)
            f.seek(0x3e)
            f.write(pbr_code)
            f.seek(36)
            f.write(b'\x80')
            f.flush()
            os.fsync(f.fileno())
        mbr = bytearray(512)
        mbr[:440] = mbr_code[:440]
        # Active FAT16-LBA partition, 1MiB-aligned. Saturated ending CHS.
        mbr[446:462] = struct.pack('<B3sB3sII', 0x80, b'\x20\x21\x00', 0x0e,
                                   b'\xfe\xff\xff', 2048, (size-offset)//512)
        mbr[510:] = b'\x55\xaa'
        digest = hashlib.sha256()
        with disk.open('xb') as out, partition.open('rb') as src:
            out.write(mbr)
            out.write(bytes(offset-512))
            done = offset
            while chunk := src.read(1024*1024):
                check_cancel()
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, size)
            out.flush()
            os.fsync(out.fileno())
        with disk.open('rb') as f:
            while chunk := f.read(1024*1024):
                check_cancel()
                digest.update(chunk)
        check_cancel()
        # Atomic publication fails if any file/symlink appeared in the meantime.
        os.link(disk, dest)
        directory_fd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return {'path': str(dest), 'size': size, 'sha256': digest.hexdigest(),
            'partition_table': 'mbr', 'filesystem': 'fat16', 'boot': 'bios',
            'source': 'Rufus upstream FreeDOS resources'}
