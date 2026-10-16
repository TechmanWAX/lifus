"""Conservative disk imaging, with no implicit unmount or privilege elevation.

Writes stage decoded bytes in a temporary file before opening the destination;
this needs scratch space equal to decoded image size, but prevents corrupt
compressed input or changing source contents from causing partial preflight
writes. Cancellation after writing starts leaves an incomplete target.
"""
import bz2
from contextlib import contextmanager
import fcntl
import gzip
import hashlib
import lzma
import os
import stat
import struct
import tempfile

from . import devices, unmount
from .images import CHUNK_SIZE


class CancelledError(InterruptedError):
    """Operation cancelled; a destination may already be partially written."""


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise CancelledError('Operation cancelled')


def _identity(st):
    return st.st_dev, st.st_ino, st.st_rdev, stat.S_IFMT(st.st_mode)


def _capacity(fd):
    st = os.fstat(fd)
    if stat.S_ISREG(st.st_mode):
        return st.st_size
    return struct.unpack('Q', fcntl.ioctl(fd, 0x80081272, bytes(8)))[0]  # BLKGETSIZE64


@contextmanager
def _source(path, *, decode=False):
    path, before = devices._checked_stat(path)
    if not (stat.S_ISREG(before.st_mode) or stat.S_ISBLK(before.st_mode)):
        raise ValueError('Source must be a regular file or block device')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb', buffering=0) as raw:
        if _identity(before) != _identity(os.fstat(fd)):
            raise ValueError('Source changed while opening')
        if not decode:
            yield raw
            return
        # This detects ordinary concurrent file writes, not a filesystem snapshot:
        # block-device contents and writers able to evade metadata changes are
        # outside this check. Keep the source quiescent throughout staging.
        staged_before = os.fstat(fd)

        def check_staged_source():
            after = os.fstat(fd)
            if ((staged_before.st_size, staged_before.st_mtime_ns, staged_before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError('Source changed during staging')

        magic = raw.read(6)
        raw.seek(0)
        if magic.startswith(b'\x1f\x8b'):
            stream = gzip.GzipFile(fileobj=raw, mode='rb')
        elif magic.startswith(b'BZh'):
            stream = bz2.BZ2File(raw, 'rb')
        elif magic.startswith(b'\xfd7zXZ\x00'):
            stream = lzma.LZMAFile(raw, 'rb')
        else:
            yield raw
            check_staged_source()
            return
        with stream:
            yield stream
        check_staged_source()


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError('Short write: no progress')
        view = view[count:]


@contextmanager
def _pinned_parent(path):
    parent = os.path.dirname(path)
    before = os.stat(parent, follow_symlinks=False)
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if _identity(os.fstat(fd)) != _identity(before):
            raise ValueError('Destination changed while opening parent')
        yield fd
    finally:
        os.close(fd)


def backup_image(source, destination, *, progress=None, cancel=None):
    """Copy raw bytes from a read-only source to a new file, never overwrite.

    A mounted source is permitted, but its backup is not a consistent filesystem
    snapshot. Unmount or freeze filesystems before backing up important data.
    Partial files are removed on failure when still owned by this operation.
    """
    _cancelled(cancel)
    destination = os.path.abspath(os.fspath(destination))
    if os.path.realpath(destination) != destination:
        raise ValueError('Symlink destination paths are not allowed')
    with _source(source) as stream, _pinned_parent(destination) as parent_fd:
        total = _capacity(stream.fileno())
        name = os.path.basename(destination)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
        identity = _identity(os.fstat(fd))
        try:
            done = 0
            digest = hashlib.sha256()
            if progress:
                progress(0, total)
            while done < total:
                _cancelled(cancel)
                block = stream.read(min(CHUNK_SIZE, total - done))
                if not block:
                    raise OSError('Source ended before its reported size')
                _write_all(fd, block)
                digest.update(block)
                done += len(block)
                if progress:
                    progress(done, total)
            _cancelled(cancel)
            os.fsync(fd)
            os.fsync(parent_fd)
            try:
                intact = (
                    os.path.realpath(destination) == destination and
                    _identity(os.stat(os.path.dirname(destination), follow_symlinks=False)) ==
                    _identity(os.fstat(parent_fd)) and
                    _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) == identity)
            except FileNotFoundError:
                intact = False
            if not intact:
                raise ValueError('Destination changed during backup')
            return {'source': os.path.abspath(os.fspath(source)),
                    'destination': destination, 'bytes_written': done,
                    'size': done, 'sha256': digest.hexdigest()}
        except BaseException:
            try:
                if _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) == identity:
                    os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(fd)


def write_image(source, target, *, allow_file=False, verify=True,
                progress=None, cancel=None, expected_identity=None):
    """Write decoded bytes; regular destination files require explicit opt-in.

    Progress reports write-phase bytes only. Readback verifies precisely the
    image range; a larger target's tail is intentionally preserved.
    expected_identity, when supplied, is the original plan_unmount identity;
    it supplements (never replaces) strict safety validation at every phase.
    """
    _cancelled(cancel)
    info = devices.validate_target(target, allow_file=allow_file)
    if expected_identity is not None:
        unmount.assert_target_identity(target, expected_identity)
    target, before = devices._checked_stat(target)
    source_path, source_stat = devices._checked_stat(source)
    if _identity(before) == _identity(source_stat):
        raise ValueError('Source and destination are the same file/device')
    size = 0
    digest = hashlib.sha256()
    with tempfile.TemporaryFile() as snapshot:
        with _source(source_path, decode=True) as stream:
            while True:
                _cancelled(cancel)
                block = stream.read(CHUNK_SIZE)
                if not block:
                    break
                size += len(block)
                if size > info['size']:
                    raise ValueError('Decoded image exceeds target capacity')
                snapshot.write(block)
                digest.update(block)
        if not size:
            raise ValueError('Image is empty')
        _cancelled(cancel)
        devices.validate_target(target, allow_file=allow_file)
        if expected_identity is not None:
            unmount.assert_target_identity(target, expected_identity)
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        if stat.S_ISBLK(before.st_mode):
            flags |= os.O_EXCL  # Linux refuses an in-use block device.
        fd = os.open(target, flags)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if _identity(os.fstat(fd)) != _identity(before):
                raise ValueError('Target changed while opening')
            if expected_identity is not None:
                unmount.assert_target_identity(target, expected_identity, fd=fd)
            devices.validate_target(target, allow_file=allow_file)
            if _identity(os.stat(target, follow_symlinks=False)) != _identity(before):
                raise ValueError('Target path changed after opening')
            if _capacity(fd) < size:
                raise ValueError('Target shrank during preflight')
            snapshot.seek(0)
            done = 0
            if progress:
                progress(0, size)
            if expected_identity is not None:
                unmount.assert_target_identity(target, expected_identity, fd=fd)
            while True:
                _cancelled(cancel)
                block = snapshot.read(CHUNK_SIZE)
                if not block:
                    break
                _write_all(fd, block)
                done += len(block)
                if progress:
                    progress(done, size)
            os.fsync(fd)
            if verify:
                # Flush first, then ask the kernel to evict clean cached pages.
                # Length zero covers the final partial page as well. This is
                # advisory: kernels may retain pages and device/controller caches
                # remain in play; verified is not a power-loss durability proof.
                if hasattr(os, 'posix_fadvise') and hasattr(os, 'POSIX_FADV_DONTNEED'):
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                os.lseek(fd, 0, os.SEEK_SET)
                actual = hashlib.sha256()
                remaining = size
                while remaining:
                    _cancelled(cancel)
                    block = os.read(fd, min(CHUNK_SIZE, remaining))
                    if not block:
                        raise OSError('Unexpected EOF during readback')
                    actual.update(block)
                    remaining -= len(block)
                if actual.digest() != digest.digest():
                    raise OSError('Readback verification failed')
            _cancelled(cancel)
            return {'source': source_path, 'target': target, 'bytes_written': done,
                    'size': size, 'sha256': digest.hexdigest(), 'verified': bool(verify)}
        finally:
            os.close(fd)
