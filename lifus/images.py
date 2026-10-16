"""Image streams and checksums (stdlib only).

Inspection scans compressed images completely: size is decoded size, not the
unreliable modulo-2**32 gzip trailer. Corrupt streams fail before target writes.
Checksums intentionally cover the stored file, suitable for download checksums.
"""
import bz2
import gzip
import hashlib
import lzma
import io
import os
import stat

from .devices import _checked_stat

CHUNK_SIZE = 1024 * 1024


def _raw_image(path):
    path, before = _checked_stat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('Image must be a regular file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    stream = os.fdopen(fd, 'rb')
    after = os.fstat(fd)
    if (before.st_dev, before.st_ino, before.st_mode) != (after.st_dev, after.st_ino, after.st_mode):
        stream.close()
        raise ValueError('Image changed while opening')
    return stream


def _compression(path):
    with _raw_image(path) as stream:
        magic = stream.read(6)
    if magic.startswith(b'\x1f\x8b'):
        return 'gzip'
    if magic.startswith(b'BZh'):
        return 'bz2'
    if magic.startswith(b'\xfd7zXZ\x00'):
        return 'xz'
    return None


class _DecodedReader(io.BufferedReader):
    """Close both the decoder and its explicitly supplied raw file."""
    def __init__(self, decoder, owner):
        self._owner = owner
        super().__init__(decoder)

    def close(self):
        try:
            super().close()
        finally:
            self._owner.close()


def open_image(path):
    """Return a binary stream, decoded by magic bytes; caller must close it."""
    raw = _raw_image(path)
    try:
        magic = raw.read(6)
        raw.seek(0)
        if magic.startswith(b'\x1f\x8b'):
            stream = gzip.GzipFile(fileobj=raw, mode='rb')
        elif magic.startswith(b'BZh'):
            stream = bz2.BZ2File(raw, 'rb')
        elif magic.startswith(b'\xfd7zXZ\x00'):
            stream = lzma.LZMAFile(raw, 'rb')
        else:
            return raw
        return _DecodedReader(stream, raw)
    except BaseException:
        raw.close()
        raise


def checksum(path, algorithm='sha256'):
    digest = hashlib.new(algorithm)
    with _raw_image(path) as stream:
        for block in iter(lambda: stream.read(CHUNK_SIZE), b''):
            digest.update(block)
    return digest.hexdigest()


def inspect_image(path):
    path = os.path.abspath(os.fspath(path))
    kind = _compression(path)
    size = 0
    with open_image(path) as stream:
        for block in iter(lambda: stream.read(CHUNK_SIZE), b''):
            size += len(block)
    return {'path': path, 'size': size, 'uncompressed_size': size,
            'stored_size': os.path.getsize(path), 'compression': kind}
