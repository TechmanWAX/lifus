"""File-only raw ↔ fixed VHD conversion (512-byte sectors, no dependencies)."""
from contextlib import contextmanager
import hashlib
import stat
import os
import struct
import time
import uuid

from .images import CHUNK_SIZE
from .writer import CancelledError, _cancelled, _write_all

VHD_EPOCH = 946684800
MAX_SIZE = 2040 * 1024**3


def _geometry(size):
    sectors = min(size // 512, 65535 * 16 * 255)
    if sectors >= 65535 * 16 * 63:
        spt, heads = 255, 16
        cylinder_heads = sectors // spt
    else:
        spt = 17
        cylinder_heads = sectors // spt
        heads = max(4, (cylinder_heads + 1023) // 1024)
        if cylinder_heads >= heads * 1024 or heads > 16:
            spt, heads = 31, 16
            cylinder_heads = sectors // spt
        if cylinder_heads >= heads * 1024:
            spt, heads = 63, 16
            cylinder_heads = sectors // spt
    return cylinder_heads // heads, heads, spt


def _footer(size):
    footer = bytearray(512)
    struct.pack_into('>8sIIQI4sI4sQQHBBII16s', footer, 0,
                     b'conectix', 2, 0x10000, 0xffffffffffffffff,
                     int(time.time()) - VHD_EPOCH, b'Lifu', 0x10000, b'Wi2k',
                     size, size, *_geometry(size), 2, 0, uuid.uuid4().bytes)
    struct.pack_into('>I', footer, 64, (~sum(footer)) & 0xffffffff)
    return bytes(footer)


@contextmanager
def _parent(path):
    """Pin each directory with no-follow opens, including ancestor components."""
    parts = path.split('/')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[1:-1]:
            if part:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY |
                                  os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        yield fd, parts[-1]
    finally:
        os.close(fd)


@contextmanager
def _source(path):
    with _parent(path) as (parent, name):
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Source must be a regular file, not a symlink or device')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                     os.O_CLOEXEC, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or not stat.S_ISREG(after.st_mode):
                raise ValueError('Source changed while opening')
            yield stream


def _valid_size(size):
    if size <= 0 or size % 512 or size > MAX_SIZE:
        raise ValueError('Image size must be a positive multiple of 512, at most 2040 GiB')


def _parse_footer(footer, file_size):
    if len(footer) != 512 or footer[:8] != b'conectix':
        raise ValueError('Missing trailing fixed VHD footer')
    checksum = struct.unpack_from('>I', footer, 64)[0]
    if checksum != (~sum(footer[:64] + bytes(4) + footer[68:])) & 0xffffffff:
        raise ValueError('Invalid VHD footer checksum')
    disk_type = struct.unpack_from('>I', footer, 60)[0]
    if disk_type != 2:
        raise ValueError('Unsupported dynamic/differencing VHD; only fixed type 2 is supported')
    features, version, offset = struct.unpack_from('>IIQ', footer, 8)
    if features not in (2, 3) or version != 0x10000 or offset != 0xffffffffffffffff:
        raise ValueError('Unsupported VHD features, version or data offset')
    original, current = struct.unpack_from('>QQ', footer, 40)
    _valid_size(original)
    _valid_size(current)
    if file_size != current + 512:
        raise ValueError('VHD payload size does not match trailing footer')
    return current


def _convert(source, destination, export, progress, cancel):
    _cancelled(cancel)
    source = os.path.abspath(os.fspath(source))
    destination = os.path.abspath(os.fspath(destination))
    with _source(source) as stream, _parent(destination) as (parent, name):
        before = os.fstat(stream.fileno())
        size = before.st_size
        magic = stream.read(16)
        if magic.startswith(b'vhdxfile'):
            raise ValueError('VHDX is unsupported')
        if magic[4:16] == b'SignedImage ':
            raise ValueError('FFU is unsupported')
        if export:
            if magic.startswith(b'conectix'):
                raise ValueError('Unsupported dynamic/differencing VHD input; raw input required')
            _valid_size(size)
            footer = _footer(size)
        else:
            if size < 1024:
                raise ValueError('Missing trailing fixed VHD footer')
            stream.seek(-512, os.SEEK_END)
            size = _parse_footer(stream.read(512), before.st_size)
            footer = b''
        stream.seek(0)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        identity = os.fstat(fd)
        try:
            digest = hashlib.sha256()
            done = 0
            if progress:
                progress(0, size)
            while done < size:
                _cancelled(cancel)
                block = stream.read(min(CHUNK_SIZE, size - done))
                if not block:
                    raise OSError('Source ended before reported size')
                _write_all(fd, block)
                digest.update(block)
                done += len(block)
                if progress:
                    progress(done, size)
            _cancelled(cancel)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('Source changed during conversion')
            if footer:
                _write_all(fd, footer)
                digest.update(footer)
            os.fsync(fd)
            _cancelled(cancel)
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise ValueError('Destination changed during conversion')
            return {'source': source, 'destination': destination,
                    'size': size, 'bytes_written': done + len(footer),
                    'sha256': digest.hexdigest(), 'format': 'vhd' if export else 'raw'}
        except BaseException:
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(fd)


def raw_to_vhd(source, destination, *, progress=None, cancel=None) -> dict:
    """Export a raw regular file as a new fixed VHD file."""
    return _convert(source, destination, True, progress, cancel)


def vhd_to_raw(source, destination, *, progress=None, cancel=None) -> dict:
    """Extract a fixed VHD payload into a new raw regular file."""
    return _convert(source, destination, False, progress, cancel)
