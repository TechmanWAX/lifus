"""Bounded, deterministic FAT32 construction on an already pinned descriptor.

Only the caller opens/partitions the target. Geometry and the entire namespace are
validated before mutation. No mounts, partition images, or extracted payloads.
UEFI only: the boot sector deliberately contains no BIOS boot loader.
"""
import hashlib
import math
import os
from pathlib import PurePosixPath
import selectors
import struct
import subprocess
import time

BLOCK = 1024 * 1024


def stream_command(args, check):
    """Drain both pipes with bounded reads; cancellation also works on silence."""
    check()
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    errors = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in (proc.stdout, proc.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                check()
                for key, _ in selector.select(0.2):
                    data = os.read(key.fd, BLOCK)
                    if not data:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is proc.stdout:
                        yield data
                    else:
                        errors.extend(data)
                        del errors[:-4096]
            while proc.poll() is None:
                check()
                try:
                    proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            check()
            if proc.returncode:
                raise ValueError('ISO stream failed: ' + errors.decode(errors='replace'))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def _lfn(name, alias):
    encoded = name.encode('utf-16le')
    units = [encoded[i:i+2] for i in range(0, len(encoded), 2)]
    count = (len(units) + 12) // 13
    units += [b'\x00\x00'] + [b'\xff\xff'] * 13
    checksum = 0
    for byte in alias:
        checksum = (((checksum & 1) << 7) + (checksum >> 1) + byte) & 255
    for index in range(count, 0, -1):
        entry = bytearray(32)
        entry[0] = index | (0x40 if index == count else 0)
        entry[11], entry[13] = 15, checksum
        fragment = b''.join(units[(index-1)*13:index*13])
        entry[1:11], entry[14:26], entry[28:32] = fragment[:10], fragment[10:22], fragment[22:26]
        yield bytes(entry)


def _entry(alias, directory, cluster, size=0):
    result = bytearray(32)
    result[:11] = alias
    result[11] = 16 if directory else 32
    struct.pack_into('<H', result, 20, cluster >> 16)
    struct.pack_into('<H', result, 26, cluster & 65535)
    struct.pack_into('<I', result, 28, size)
    # 1980-01-01: valid FAT dates, reproducible metadata.
    struct.pack_into('<H', result, 16, 33)
    struct.pack_into('<H', result, 18, 33)
    struct.pack_into('<H', result, 24, 33)
    return bytes(result)


class Layout:
    def __init__(self, records, size, offset, label):
        if size % 512 or offset < 0 or offset % 512 or not 64 * BLOCK <= size < 2**41:
            raise ValueError('Direct FAT32 partition must be sector aligned and 64 MiB–2 TiB')
        if not 1 <= len(label) <= 11 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for c in label):
            raise ValueError('Invalid FAT32 label')
        if len(records) > 100000:
            raise ValueError('Too many ISO members (limit 100000)')
        self.size, self.offset, self.label = size, offset, label
        self.sectors = size // 512
        self.spc = 1
        # Keep FAT32 cluster count legal, prefer >=4 KiB clusters on larger media.
        while self.spc < 64 and self.sectors // self.spc > 2**21:
            self.spc *= 2
        self.fat_sectors = math.ceil((self.sectors - 32 + 2*self.spc) / (128*self.spc + 2))
        self.data_sector = 32 + 2*self.fat_sectors
        self.clusters = (self.sectors - self.data_sector) // self.spc
        if not 65525 <= self.clusters < 0x0ffffff5 or self.clusters + 2 > self.fat_sectors * 128:
            raise ValueError('Unsupported FAT32 geometry')
        self.cluster_bytes = self.spc * 512
        self.nodes: dict[str, dict] = {'': dict(path='', directory=True, size=0, children=[])}
        seen = set()
        for record in records:
            name = record['path']
            parts = name.split('/')
            if (not name or len(name.encode('utf-8')) > 4096 or any(not p or p in ('.', '..') or p[-1] in ' .'
                    or any(ord(c) < 32 or c in '\\:*?"<>|' for c in p)
                    or len(p.encode('utf-16le')) > 510 for p in parts)):
                raise ValueError('Unsupported FAT filename: ' + name)
            if name.casefold() in seen:
                raise ValueError('Case-colliding ISO paths')
            seen.add(name.casefold())
            if not record['directory'] and not 0 <= record['size'] <= 2**32 - 1:
                raise ValueError('FAT32 file size limit exceeded')
            for depth in range(1, len(parts)+1):
                path = '/'.join(parts[:depth])
                key = path.casefold()
                directory = depth < len(parts) or record['directory']
                existing = self.nodes.get(key)
                if existing:
                    if existing['path'] != path or existing['directory'] != directory:
                        raise ValueError('Archive file/directory or case conflict: ' + path)
                    continue
                parent = '/'.join(parts[:depth-1]).casefold()
                node = dict(record if depth == len(parts) else {}, path=path,
                            directory=directory, size=0 if directory else record['size'],
                            children=[], parent=parent)
                if len(self.nodes) >= 100001:
                    raise ValueError('Too many FAT32 files/directories (limit 100000)')
                self.nodes[key] = node
                self.nodes[parent]['children'].append(key)
        # Every entry gets a unique generated short alias; long names preserve ISO spelling.
        self.ordered = list(self.nodes.values())
        next_cluster = 2
        for node in self.ordered:
            if node['directory']:
                entries = 1 if not node['path'] else 2  # label or dot/dotdot
                occupied = {PurePosixPath(self.nodes[k]['path']).name.casefold() for k in node['children']}
                index = 0
                for key in node['children']:
                    child = self.nodes[key]
                    while True:
                        index += 1
                        alias = 'F%07X' % index
                        if alias.casefold() not in occupied:
                            break
                    child['alias'] = alias.encode() + b'   '
                    entries += 1 + (len(PurePosixPath(child['path']).name.encode('utf-16le')) // 2 + 12) // 13
                length = (entries + 1) * 32  # zero end marker
            else:
                length = node['size']
            node['count'] = (length + self.cluster_bytes - 1) // self.cluster_bytes
            node['cluster'] = next_cluster if node['count'] else 0
            next_cluster += node['count']
        self.next_cluster = next_cluster
        if next_cluster > self.clusters + 2:
            raise ValueError('ISO contents and directories exceed FAT32 partition capacity')

    def position(self, node):
        return self.data_sector * 512 + (node['cluster'] - 2) * self.cluster_bytes

    def metadata(self):
        reserved = bytearray(32 * 512)
        boot = bytearray(512)
        boot[:11] = b'\xeb\x58\x90LIFUS   '
        struct.pack_into('<HBHBHHBHHHII', boot, 11, 512, self.spc, 32, 2, 0, 0, 248, 0,
                         63, 255, self.offset // 512, self.sectors)
        struct.pack_into('<IHHIHH', boot, 36, self.fat_sectors, 0, 0, 2, 1, 6)
        boot[64], boot[66] = 128, 41
        struct.pack_into('<I', boot, 67, 0x4c494655)
        boot[71:82] = self.label.encode().ljust(11, b' ')
        boot[82:90] = b'FAT32   '
        boot[90:94] = b'\xfa\xf4\xeb\xfd'  # halt if erroneously booted in BIOS mode
        boot[510:512] = b'\x55\xaa'
        info = bytearray(512)
        struct.pack_into('<I', info, 0, 0x41615252)
        struct.pack_into('<III', info, 484, 0x61417272,
                         self.clusters - (self.next_cluster - 2),
                         self.next_cluster if self.next_cluster < self.clusters + 2 else 0xffffffff)
        info[508:512] = b'\x00\x00\x55\xaa'
        reserved[:512] = reserved[3072:3584] = boot
        reserved[512:1024] = reserved[3584:4096] = info
        yield 0, bytes(reserved)
        ends = {n['cluster'] + n['count'] - 1 for n in self.ordered if n['count']}
        for fat in range(2):
            for first in range(0, self.fat_sectors * 128, BLOCK // 4):
                count = min(BLOCK // 4, self.fat_sectors * 128 - first)
                buf = bytearray(count * 4)
                for index in range(first, min(first + count, self.next_cluster)):
                    value = (0x0ffffff8 if index == 0 else 0x0fffffff
                             if index == 1 or index in ends else index + 1)
                    struct.pack_into('<I', buf, (index-first)*4, value)
                yield (32 + fat*self.fat_sectors)*512 + first*4, bytes(buf)
        for node in self.ordered:
            if not node['directory']:
                continue
            def entries():
                if not node['path']:
                    entry = bytearray(32)
                    entry[:11] = self.label.encode().ljust(11, b' ')
                    entry[11] = 8
                    yield bytes(entry)
                else:
                    yield _entry(b'.          ', True, node['cluster'])
                    parent = self.nodes[node['parent']]
                    yield _entry(b'..         ', True, parent['cluster'] if parent['path'] else 0)
                for key in node['children']:
                    child = self.nodes[key]
                    yield from _lfn(PurePosixPath(child['path']).name, child['alias'])
                    yield _entry(child['alias'], child['directory'], child['cluster'], child['size'])
            buf = bytearray()
            position = self.position(node)
            total = node['count'] * self.cluster_bytes
            written = 0
            for entry in entries():
                buf.extend(entry)
                if len(buf) == BLOCK:
                    yield position + written, bytes(buf)
                    written += len(buf)
                    buf.clear()
            # Directory tail is at most one block plus one cluster.
            buf.extend(bytes(total - written - len(buf)))
            if buf:
                yield position + written, bytes(buf)

    def write(self, fd, streams, check, progress, sync):
        from .operations import _pwrite_all
        written = 0
        total = sum(n['size'] for n in self.ordered if not n['directory'])
        done = 0
        dirty = 0
        last_phase, last_emit = None, 0.0
        def emit(phase, value=0):
            nonlocal last_phase, last_emit
            if progress:
                now = time.monotonic()
                # Pipe reads can be only a few KiB. Avoid tens of thousands
                # of JSON flushes / queued widget updates per second.
                if phase == last_phase and value != total and now - last_emit < 0.1:
                    return
                progress(dict(phase=phase, completed=value, total=total))
                last_phase, last_emit = phase, now
        def write(data, position):
            nonlocal written, dirty
            check()
            if position < 0 or position + len(data) > self.size:
                raise ValueError('Write outside FAT32 partition')
            _pwrite_all(fd, data, self.offset + position)
            written += len(data)
            dirty += len(data)
            if dirty >= 64 * BLOCK:
                emit('flush', done)
                os.fsync(fd)
                dirty = 0
                check()
                emit('write', done)
        emit('write')
        for position, data in self.metadata():
            write(data, position)
        digests = {}
        for node in self.ordered:
            if node['directory']:
                continue
            digest = hashlib.sha256()
            count = 0
            iterator = streams(node)
            try:
                for data in iterator:
                    check()
                    if len(data) > BLOCK or count + len(data) > node['size']:
                        raise ValueError('ISO stream exceeds declared size or buffer limit')
                    write(data, self.position(node) + count)
                    digest.update(data)
                    count += len(data)
                    done += len(data)
                    emit('write', done)
            finally:
                iterator.close()
            if count != node['size']:
                raise ValueError('ISO stream size changed')
            padding = node['count'] * self.cluster_bytes - count
            if padding:
                write(bytes(padding), self.position(node) + count)
            digests[node['path']] = digest.digest()
        emit('sync', done)
        sync(fd)
        emit('verify')
        for position, data in self.metadata():
            check()
            if os.pread(fd, len(data), self.offset + position) != data:
                raise IOError('FAT32 metadata read-back verification failed')
        done = 0
        for node in self.ordered:
            if node['directory']:
                continue
            digest = hashlib.sha256()
            for position in range(0, node['size'], BLOCK):
                check()
                count = min(BLOCK, node['size'] - position)
                data = os.pread(fd, count, self.offset + self.position(node) + position)
                if len(data) != count:
                    raise IOError('Short FAT32 readback')
                digest.update(data)
                done += count
                emit('verify', done)
            if digest.digest() != digests[node['path']]:
                raise IOError('FAT32 file hash read-back verification failed: ' + node['path'])
        check()
        return written
