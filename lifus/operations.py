"""Advanced media operations using argv-only subprocesses and no kernel mounts.

A preview is not authorization: execute regenerates it and revalidates identity.
Windows FAT32 streams directly into the bounded target partition; other filesystem
paths retain private-file staging. No operation acquires mounts or loop devices.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from . import unmount

MiB = 1024 * 1024
FAT_LIMIT = 2**32 - 1


class OperationCancelled(RuntimeError):
    pass


def _check_cancel(cancel):
    if cancel and (cancel() if callable(cancel) else cancel.is_set()):
        raise OperationCancelled('Cancelled; target may contain incomplete data')


def _run(args, *, input=None, cancel=None):
    if cancel is None:
        completed = subprocess.run(args, input=input, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
    else:
        _check_cancel(cancel)
        with subprocess.Popen(args, stdin=subprocess.PIPE if input is not None else None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            try:
                while True:
                    _check_cancel(cancel)
                    try:
                        stdout, stderr = proc.communicate(input=input, timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        input = None
                completed = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()
        _check_cancel(cancel)
    if completed.returncode:
        raise RuntimeError(f'{args[0]} failed: {completed.stderr.decode(errors="replace")[-4000:]}')
    return completed.stdout


def _validate(path, allow_file=False):
    from .devices import validate_target
    result = validate_target(path, allow_file=allow_file)
    if result.get('protected') or result.get('mountpoints'):
        raise ValueError('Mounted or protected targets cannot be modified')
    st = os.stat(result['path'])
    result = dict(result, stat_identity=[st.st_dev, st.st_ino, st.st_rdev])
    return result


def _safe_member(name):
    if (not name or name.startswith('/') or '\\' in name or ':' in name
            or any(part in ('..', '.') for part in name.split('/'))
            or any(ord(c) < 32 for c in name) or name.startswith('-')):
        raise ValueError(f'Unsafe archive member: {name!r}')
    return str(PurePosixPath(name))


def _members(source, *, archive_type=None, with_type=False, maximum=64*MiB, cancel=None):
    # Pin the detected archive parser too: extensionless /proc descriptors may
    # select the ISO9660 boot-only view instead of a Windows ISO's UDF tree.
    from .direct_fat import stream_command
    args = ['7z', 'l', '-slt'] + (['-t' + archive_type] if archive_type else [])
    output = bytearray()
    chunks = stream_command(args + ['--', source], lambda: _check_cancel(cancel))
    with contextlib.closing(chunks):
        for chunk in chunks:
            if len(output) + len(chunk) > maximum:
                raise ValueError('ISO metadata listing exceeds bounded memory limit')
            output.extend(chunk)
    text = output.decode('utf-8', 'strict')
    if '----------\n' not in text:
        raise ValueError('Unrecognized archive listing')
    records = []
    seen = set()
    for block in text.split('----------\n', 1)[1].split('\n\n'):
        fields = dict(line.split(' = ', 1) for line in block.splitlines() if ' = ' in line)
        if 'Path' not in fields:
            continue
        name = _safe_member(fields['Path'])
        key = name.casefold()
        if key in seen:
            raise ValueError('Case-colliding ISO paths are not supported')
        seen.add(key)
        if fields.get('Symbolic Link') or fields.get('Hard Link') or 'l' in fields.get('Mode', '')[:1]:
            raise ValueError('ISO links are not supported')
        directory = fields.get('Folder') == '+' or fields.get('Attributes', '').startswith('D')
        records.append(dict(path=name, size=int(fields.get('Size') or 0), directory=directory))
    if not records:
        raise ValueError('Empty or unsupported ISO')
    if with_type:
        match = re.search(r'(?m)^Type = (\w+)$', text.split('----------\n', 1)[0])
        if not match:
            raise ValueError('Archive parser type is missing')
        return records, match.group(1)
    return records


def _read_member(source, member, maximum=MiB, cancel=None):
    data = _run(['7z', 'x', '-so', '-spd', '--', source, member], cancel=cancel)
    if len(data) > maximum:
        raise ValueError('Metadata member too large')
    return data


def _source_safe(source, target, cancel=None):
    if not source:
        return
    source_stat = os.stat(source)
    target_stat = os.stat(target)
    if os.path.samefile(source, target):
        raise ValueError('Source is the target')
    if stat.S_ISBLK(target_stat.st_mode):
        tree = json.loads(_run(['lsblk', '--json', '-o', 'PATH,MAJ:MIN', target], cancel=cancel))
        def walk(nodes):
            for node in nodes:
                yield node['maj:min']
                yield from walk(node.get('children', []))
        dev = source_stat.st_rdev if stat.S_ISBLK(source_stat.st_mode) else source_stat.st_dev
        if f'{os.major(dev)}:{os.minor(dev)}' in set(walk(tree['blockdevices'])):
            raise ValueError('Source or staging directory is on target disk')


def _stat_identity(s):
    if not stat.S_ISREG(s.st_mode):
        raise ValueError('Source must be a regular file')
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


@contextlib.contextmanager
def _open_approved(path, identity):
    # NONBLOCK is essential: a regular file can be swapped for a FIFO before open.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if _stat_identity(os.fstat(fd)) != identity:
            raise ValueError('Source changed while opening')
        yield fd
        if _stat_identity(os.fstat(fd)) != identity:
            raise ValueError('Source changed while reading')
    finally:
        os.close(fd)


def _read_approved(path, identity):
    if identity[2] > MiB:
        raise ValueError('Invalid unattend XML: larger than 1 MiB')
    with _open_approved(path, identity) as fd:
        data = bytearray()
        while len(data) <= identity[2]:
            chunk = os.read(fd, identity[2] + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != identity[2]:
            raise ValueError('Source changed while reading')
    return bytes(data)


def _source_identity(path):
    s = os.stat(path)
    if not stat.S_ISREG(s.st_mode):
        raise ValueError('Source must be a regular ISO file')
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def _persistence_layout(source, target, size, records, requested, cancel=None):
    paths = {r['path'].lower(): r for r in records}
    if 'live/filesystem.squashfs' not in paths or '.disk/info' not in paths:
        raise ValueError('Persistence requires a recognized Debian Live image')
    info = _read_member(source, paths['.disk/info']['path'], cancel=cancel).decode(errors='replace')
    if 'Debian' not in info:
        raise ValueError('Only Debian Live persistence is supported')
    configs = [r for r in records if r['path'].lower().endswith(('.cfg', '.conf')) and r['size'] < MiB]
    enabled = any(re.search(rb'(?m)^\s*(?:linux|linuxefi|append)\s+[^\n]*\bpersistence\b',
                            _read_member(source, r['path'], cancel=cancel)) for r in configs)
    if not enabled:
        raise ValueError('ISO must already have a boot entry with the persistence kernel parameter')
    table = json.loads(_run(['sfdisk', '--json', target], cancel=cancel))['partitiontable']
    parts = table.get('partitions', [])
    if table['label'] != 'dos' or table.get('sectorsize', 512) != 512 or not 1 <= len(parts) < 4:
        raise ValueError('Persistence requires an existing MBR hybrid ISO with a free primary slot')
    if any(str(p['type']).lower() in ('5', 'f', '85', 'ee') for p in parts):
        raise ValueError('Extended/protective MBR is not supported')
    end = max(p['start'] + p['size'] for p in parts) * 512
    start = ((max(end, os.path.getsize(source)) + MiB - 1) // MiB) * MiB
    length = int(requested or ((size - start) // MiB * MiB))
    if length < 64 * MiB or start + length > size:
        raise ValueError('Not enough unallocated space for persistence; no shrinking is performed')
    return start, length


def plan_operation(action, source, target, *, cancel=None, **options) -> dict:
    if 'partition_scheme' in options:
        options['partition_table'] = options.pop('partition_scheme')
    if 'persistence_mib' in options:
        options['persistence_size'] = int(options.pop('persistence_mib')) * MiB
    if action not in ('format', 'windows', 'persistence', 'badblocks'):
        raise ValueError('Unknown operation')
    allowed = {'allow_file', 'filesystem', 'partition_table', 'label', 'passes', 'persistence_size', 'staging_dir', 'unattend'}
    if set(options) - allowed:
        raise ValueError(f'Unknown options: {sorted(set(options) - allowed)}')
    info = _validate(target, options.get('allow_file', False))
    target = info['path']
    unattend_identity = None
    if options.get('unattend'):
        if action != 'windows':
            raise ValueError('unattend is supported only for Windows media')
        xml_path = os.path.abspath(options['unattend'])
        if os.path.islink(xml_path):
            raise ValueError('Unattend symlinks are not supported')
        unattend_identity = _source_identity(xml_path)
        _source_safe(xml_path, target, cancel)
        if unattend_identity[2] > MiB:
            raise ValueError('Invalid unattend XML: larger than 1 MiB')
        try:
            data = _read_approved(xml_path, unattend_identity)
            if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
                raise ValueError('Invalid unattend XML: DTD/entities forbidden')
            if ET.fromstring(data).tag != '{urn:schemas-microsoft-com:unattend}unattend':
                raise ValueError('Invalid unattend XML: wrong root namespace')
        except ET.ParseError as exc:
            raise ValueError('Invalid unattend XML') from exc
        options['unattend'] = xml_path
    source = os.path.realpath(source) if source else None
    _source_safe(source, target, cancel)
    filesystem = options.get('filesystem', 'fat32')
    table = options.get('partition_table', 'gpt')
    if filesystem not in ('fat32', 'ntfs', 'exfat', 'ext2', 'ext3', 'ext4'):
        raise ValueError('Unsupported filesystem')
    if table not in ('gpt', 'mbr'):
        raise ValueError('Partition table must be gpt or mbr')
    if table == 'mbr' and info['size'] >= 2**41:
        raise ValueError('MBR is restricted to targets smaller than 2 TiB')
    label = options.get('label', 'LIFUS')
    if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,11}', label):
        raise ValueError('Label must be 1–11 ASCII letters, digits, underscore or hyphen')
    passes = int(options.get('passes', 1))
    if passes < 1 or passes > 4:
        raise ValueError('passes must be 1–4')
    start = MiB
    length = (info['size'] // MiB - 2) * MiB
    efi_offset = None
    if action == 'windows' and filesystem == 'ntfs':
        length -= MiB
        efi_offset = start + length
    if length < 64 * MiB:
        raise ValueError('Target must be at least 66 MiB')
    steps = ['Revalidate unmounted/unprotected target and identity', 'Build filesystem in a private staging file',
             f'Overwrite partition table with {table}', 'Copy staged filesystem to partition', 'fsync target']
    warning = 'ALL target data will be destroyed.'
    tools = ['parted', 'mkfs.' + ('vfat' if filesystem == 'fat32' else filesystem)]
    records = []
    archive_type = None
    source_id = None
    if action in ('windows', 'persistence'):
        if not source:
            raise ValueError('This operation requires a source ISO')
        source_id = _source_identity(source)
        records, archive_type = _members(source, with_type=True, cancel=cancel)
        tools += ['7z']
    if action == 'windows':
        if filesystem not in ('fat32', 'ntfs'):
            raise ValueError('Windows UEFI creation supports FAT32 or NTFS')
        paths = {r['path'].lower() for r in records}
        if not any(p in paths for p in ('efi/boot/bootx64.efi', 'efi/boot/bootaa64.efi', 'efi/boot/bootia32.efi')):
            raise ValueError('ISO has no supported removable-media UEFI loader')
        if 'sources/boot.wim' not in paths:
            raise ValueError('Not a Windows installation ISO')
        if sum(r['size'] for r in records) + 64*MiB > length:
            raise ValueError('ISO contents do not fit target with filesystem overhead')
        for r in records:
            if filesystem == 'fat32' and r['size'] > FAT_LIMIT and r['path'].lower() != 'sources/install.wim':
                raise ValueError('Oversize FAT32 file cannot be split: ' + r['path'])
        tools += ['wimlib-imagex']
        if filesystem == 'ntfs':
            _uefi_ntfs_asset()
            steps = ['Revalidate target and stage ISO files without mounts',
                     'Populate NTFS using wimlib capture/apply (no FAT32 file size limit)',
                     'Create data partition and upstream UEFI:NTFS EFI partition',
                     'Write and verify both filesystems; original disk data is erased']
    elif action == 'persistence':
        start, length = _persistence_layout(source, target, info['size'], records, options.get('persistence_size'), cancel=cancel)
        filesystem, label = 'ext4', 'persistence'
        tools = ['7z', 'sfdisk', 'mkfs.ext4']
        warning = 'Append a Debian persistence partition in unused space; existing media can be damaged if interrupted.'
        steps = ['Require Debian Live MBR ISO and existing persistence-enabled boot entry',
                 'Verify target prefix byte-for-byte against source ISO',
                 'Build ext4 persistence filesystem with / union in persistence.conf',
                 'Append primary partition without shrinking existing partitions', 'Copy filesystem and fsync']
    elif action == 'badblocks':
        tools = []
        start, length = 0, info['size']
        steps = [f'Write address-dependent pseudorandom data to EVERY byte ({passes} passes)',
                 'fsync, evict cache where supported, then read and compare EVERY byte',
                 'Stop at first mismatch; original contents are NOT restored']
    staging_bytes = length
    if action == 'windows':
        payload = sum(r['size'] for r in records)
        staging_bytes = length + 2 * payload
        if filesystem == 'fat32':
            warning += ' Quick format, not secure erasure: free clusters retain old bytes.'
            from .direct_fat import Layout
            large = [r for r in records if not r['directory'] and r['size'] > FAT_LIMIT]
            if large and any(re.fullmatch(r'sources/install\d*\.swm', r['path'].casefold()) for r in records):
                raise ValueError('Split WIM output collides with ISO files')
            preview_records = [dict(r, size=0) if r in large else r for r in records]
            Layout(preview_records, length, start, label)  # namespace/geometry before destruction
            staging_bytes = sum(2 * r['size'] + 64 * MiB for r in large)
            tools = ['parted', '7z'] + (['wimlib-imagex'] if large else [])
            warning += ' Cancellation/read errors can leave incomplete installation media.'
            steps = ['Validate ISO namespace, source identity, capacity and pinned target',
                     'Pre-split oversized install.wim only (exceptional temporary staging)' if large
                     else 'No temporary payloads or partition image: staging 0 bytes',
                     f'Create {table} and FAT32 directly on the target (UEFI only)',
                     'Stream ISO files with bounded buffers; hash input bytes',
                     'fsync, verify FAT metadata and read back every file hash']
    if action == 'badblocks':
        staging_bytes = 0
    normalized = dict(options, filesystem=filesystem, partition_table=table, label=label, passes=passes)
    return dict(action=action, source=source, source_identity=source_id, target=target, identity=info,
                unattend_identity=unattend_identity, efi_offset=efi_offset,
                archive_type=archive_type,
                member_fingerprint=hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
                options=normalized, destructive=True, warning=warning, steps=steps,
                offset=start, size=length, required_tools=tools,
                missing_tools=[t for t in tools if not shutil.which(t)],
                staging_bytes=staging_bytes)


def _uefi_ntfs_asset():
    from .freedos import _assets
    path = _assets() / 'res/uefi/uefi-ntfs.img'
    if not path.is_file() or path.stat().st_size != MiB:
        raise ValueError('Original Rufus UEFI:NTFS image missing or has unexpected size')
    return path


def _format_image(path, size, filesystem, label, contents=None, cancel=None):
    with open(path, 'xb') as stream:
        stream.truncate(size)
    if filesystem == 'fat32':
        args = ['mkfs.vfat', '-F', '32', '-n', label, path]
    elif filesystem == 'ntfs':
        args = ['mkfs.ntfs', '-F', '-Q', '-L', label, path]
    elif filesystem == 'exfat':
        args = ['mkfs.exfat', '-L', label, path]
    else:
        args = ['mkfs.' + filesystem, '-F', '-L', label]
        if contents:
            args += ['-d', contents]
        args += [path]
    _run(args, cancel=cancel)


def _extract(source, records, destination, cancel, archive_type=None):
    for record in records:
        _check_cancel(cancel)
        name = _safe_member(record['path'])
        output = Path(destination, name)
        if record['directory']:
            output.mkdir(parents=True, exist_ok=True)
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        # 7z receives no filesystem output directory. Links become ordinary bytes,
        # and exclusive creates prevent duplicate paths from replacing anything.
        from .direct_fat import stream_command
        chunks = stream_command(['7z', 'x', '-so', '-spd'] + (['-t' + archive_type] if archive_type else []) + ['--', source, name],
                                lambda: _check_cancel(cancel))
        with open(output, 'xb') as stream, contextlib.closing(chunks):
            copied = 0
            for chunk in chunks:
                copied += len(chunk)
                if copied > record['size']:
                    raise ValueError('Archive output exceeds declared size')
                stream.write(chunk)
            if copied != record['size']:
                raise ValueError('ISO extraction failed or size changed')


def _populate_fat32(source, records, destination, image, options, progress, cancel):
    """Keep at most one extracted input (or the original WIM + its splits)."""
    directories = set()
    def mkdir(name):
        if name in ('', '.') or name in directories:
            return
        mkdir(str(PurePosixPath(name).parent))
        _check_cancel(cancel)
        _run(['mmd', '-i', image, '::/' + name], cancel=cancel)
        directories.add(name)

    def copy(path, name):
        mkdir(str(PurePosixPath(name).parent))
        _check_cancel(cancel)
        _run(['mcopy', '-i', image, str(path), '::/' + name], cancel=cancel)
        path.unlink()
        if progress:
            progress(dict(phase='prepare', completed=0, total=0))

    # Validate the entire tree before releasing individual inputs; otherwise a
    # file 'a' followed by 'a/b' could evade the old exclusive-create protection.
    files = {r['path'].casefold() for r in records if not r['directory']}
    for record in records:
        name = _safe_member(record['path'])
        if any(str(p).casefold() in files for p in PurePosixPath(name).parents):
            raise ValueError('Archive file/directory conflict')
    ordered = sorted(records, key=lambda r: r['size'] <= FAT_LIMIT)
    for record in ordered:
        _check_cancel(cancel)
        name = record['path']
        if options.get('unattend') and name.casefold() == 'autounattend.xml':
            if record['directory']:
                raise ValueError('ISO autounattend.xml is not a regular file')
            continue
        if record['directory']:
            mkdir(name)
            continue
        _extract(source, [record], destination, cancel)
        path = Path(destination, name)
        if record['size'] > FAT_LIMIT:
            if name.casefold() != 'sources/install.wim':
                raise ValueError('Oversize FAT32 file cannot be split')
            if any(re.fullmatch(r'sources/install\d*\.swm', f) for f in files):
                raise ValueError('Split WIM output collides with ISO files')
            _run(['wimlib-imagex', 'split', str(path), str(path.with_suffix('.swm')), '3800'], cancel=cancel)
            _check_cancel(cancel)
            path.unlink()
            for part in sorted(path.parent.glob(path.stem + '*.swm')):
                if part.stat().st_size > FAT_LIMIT:
                    raise ValueError('Split WIM part exceeds FAT32 file limit')
                copy(part, str(PurePosixPath(name).parent / part.name))
        else:
            copy(path, name)
    if options.get('unattend'):
        path = Path(destination, 'autounattend.xml')
        path.write_bytes(_read_approved(options['unattend'], _source_identity(options['unattend'])))
        copy(path, 'autounattend.xml')


def _fat32_ranges(path, cancel=None):
    """Yield metadata and live cluster ranges; never infer liveness from holes.

    Free clusters deliberately retain old target bytes (quick format, not erase).
    Even zero-filled live files must be copied over a dirty target. Validate the
    private mkfs-generated geometry before any target writes; memory stays bounded.
    """
    with open(path, 'rb') as stream:
        boot = stream.read(512)
        def number(offset, count):
            return int.from_bytes(boot[offset:offset + count], 'little')
        sector, cluster = number(11, 2), number(13, 1)
        reserved, fats, fat_sectors = number(14, 2), number(16, 1), number(36, 4)
        sectors = number(32, 4)
        data_sector = reserved + fats * fat_sectors
        size = os.fstat(stream.fileno()).st_size
        if (len(boot) != 512 or boot[510:512] != b'\x55\xaa'
                or sector not in (512, 1024, 2048, 4096)
                or cluster not in (1, 2, 4, 8, 16, 32, 64, 128)
                or fats != 2 or reserved < 1 or fat_sectors < 1
                or number(17, 2) or number(22, 2) or number(40, 2)
                or not data_sector < sectors or sectors * sector > size):
            raise ValueError('Invalid staged FAT32 geometry')
        clusters = (sectors - data_sector) // cluster
        if clusters < 65525 or clusters + 2 > fat_sectors * sector // 4:
            raise ValueError('Invalid staged FAT32 cluster count')
        yield 0, data_sector * sector
        stream.seek(reserved * sector + 8)
        run_start = None
        for first in range(0, clusters, 16384):
            _check_cancel(cancel)
            count = min(16384, clusters - first)
            entries = stream.read(count * 4)
            if len(entries) != count * 4:
                raise ValueError('Truncated staged FAT')
            for index in range(count):
                allocated = int.from_bytes(entries[index*4:index*4+4], 'little') & 0x0fffffff
                n = first + index
                if allocated and run_start is None:
                    run_start = n
                elif not allocated and run_start is not None:
                    yield (data_sector + run_start * cluster) * sector, (n - run_start) * cluster * sector
                    run_start = None
        if run_start is not None:
            yield (data_sector + run_start * cluster) * sector, (clusters - run_start) * cluster * sector


def _prepare_direct(source, records, destination, current, cancel):
    """Only an oversized WIM is staged, split and validated before destruction."""
    from .direct_fat import Layout, stream_command
    options = current['options']
    result = []
    for record in records:
        _check_cancel(cancel)
        name = record['path']
        if options.get('unattend') and name.casefold() == 'autounattend.xml':
            if record['directory']:
                raise ValueError('ISO autounattend.xml is not a regular file')
            continue
        if not record['directory'] and record['size'] > FAT_LIMIT:
            path = Path(destination, 'install.wim')
            copied = 0
            chunks = stream_command(['7z', 'x', '-so', '-spd', '-t' + current['archive_type'], '--', source, name],
                                    lambda: _check_cancel(cancel))
            with path.open('xb') as stream, contextlib.closing(chunks):
                for data in chunks:
                    copied += len(data)
                    if copied > record['size']:
                        raise ValueError('ISO output exceeds declared size')
                    stream.write(data)
            if copied != record['size']:
                raise ValueError('ISO stream size changed')
            _run(['wimlib-imagex', 'split', str(path), str(path.with_suffix('.swm')), '3800'], cancel=cancel)
            path.unlink()
            parts = sorted(Path(destination).glob('install*.swm'))
            if not parts:
                raise ValueError('WIM splitter produced no parts')
            for part in parts:
                identity = _source_identity(part)
                size = identity[2]
                if size > FAT_LIMIT:
                    raise ValueError('Split WIM part exceeds FAT32 file limit')
                result.append(dict(path=str(PurePosixPath(name).parent / part.name),
                                   size=size, directory=False, local=str(part), local_identity=identity))
        else:
            result.append(dict(record, archive_type=current['archive_type']))
    if options.get('unattend'):
        result.append(dict(path='autounattend.xml', size=current['unattend_identity'][2],
                           directory=False, data=_read_approved(options['unattend'], current['unattend_identity'])))
    return Layout(result, current['size'], current['offset'], options['label'])


def _direct_stream(source, node, cancel):
    from .direct_fat import stream_command
    if 'data' in node:
        _check_cancel(cancel)
        yield node['data']
    elif node.get('local'):
        fd = node['local_fd']
        if _stat_identity(os.fstat(fd)) != node['local_identity']:
            raise ValueError('Source changed before streaming')
        offset = 0
        while True:
            _check_cancel(cancel)
            chunk = os.pread(fd, MiB, offset)
            if not chunk:
                break
            offset += len(chunk)
            yield chunk
        if _stat_identity(os.fstat(fd)) != node['local_identity']:
            raise ValueError('Source changed during streaming')
    else:
        chunks = stream_command(['7z', 'x', '-so', '-spd', '-t' + node['archive_type'], '--', source, node['path']],
                                lambda: _check_cancel(cancel))
        with contextlib.closing(chunks):
            yield from chunks


def _same_identity(before, after):
    return all(before.get(k) == after.get(k) for k in ('path', 'size', 'model', 'serial', 'transport', 'stat_identity'))


def _verify_prefix(source, target, cancel):
    with open(source, 'rb') as left, open(target, 'rb') as right:
        while True:
            _check_cancel(cancel)
            data = left.read(4*MiB)
            if not data:
                return
            if right.read(len(data)) != data:
                raise ValueError('Target does not contain an unchanged copy of source ISO')


def _sync_for_readback(fd):
    """Flush writes and request cache eviction before verifying target bytes."""
    os.fsync(fd)
    if hasattr(os, 'posix_fadvise') and hasattr(os, 'POSIX_FADV_DONTNEED'):
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


def _badblocks(fd, size, passes, progress, cancel):
    # Different bytes at every address detect wrapped/fake-capacity flash, unlike
    # constant-pattern tests. A new seed is retained for each write/read pass.
    block = 4*MiB
    for passno in range(passes):
        seed = os.urandom(32)
        for phase in ('write', 'verify'):
            if progress:
                progress(dict(phase=phase, completed=0, total=size, pass_number=passno+1))
            for offset in range(0, size, block):
                _check_cancel(cancel)
                count = min(block, size-offset)
                expected = hashlib.shake_256(seed + offset.to_bytes(8, 'little')).digest(count)
                if phase == 'write':
                    _pwrite_all(fd, expected, offset)
                elif os.pread(fd, count, offset) != expected:
                    raise IOError(f'Bad-block/capacity mismatch at byte offset {offset}')
                if progress:
                    progress(dict(phase=phase, completed=offset+count, total=size, pass_number=passno+1))
            if progress:
                progress(dict(phase='sync', completed=size, total=size, pass_number=passno+1))
            _sync_for_readback(fd)


def _pwrite_all(fd, data, offset):
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        if written <= 0:
            raise IOError('Short write')
        view = view[written:]
        offset += written


def execute_operation(plan, *, progress=None, cancel=None, expected_identity=None):
    """Execute a preview, optionally bound to an original plan_unmount identity.

    The confirmed unmount identity is separate from the operation preview and
    is checked through staging, opening and final strict pre-write validation.
    """
    if os.geteuid() != 0:
        raise PermissionError('Advanced execution requires explicit root invocation; preview is unprivileged')
    _check_cancel(cancel)
    if progress:
        progress(dict(phase='prepare', completed=0, total=plan['size']))
    current = plan_operation(plan['action'], plan.get('source'), plan['target'], cancel=cancel, **plan['options'])
    if expected_identity is not None:
        unmount.assert_target_identity(plan['target'], expected_identity)
    if not _same_identity(plan['identity'], current['identity']):
        raise ValueError('Target identity changed since preview')
    if plan.get('source_identity') != current.get('source_identity'):
        raise ValueError('Source changed since preview')
    if plan.get('unattend_identity') != current.get('unattend_identity'):
        raise ValueError('Unattend changed since preview')
    if current['missing_tools']:
        raise RuntimeError('Missing tools: ' + ', '.join(current['missing_tools']))
    action, target = current['action'], current['target']
    options = current['options']
    source = current['source']
    if action == 'persistence':
        _verify_prefix(source, target, cancel)
    stage_parent = options.get('staging_dir') or tempfile.gettempdir()
    _source_safe(stage_parent, target, cancel)
    free = shutil.disk_usage(stage_parent).free
    if action != 'badblocks' and free < current['staging_bytes']:
        raise ValueError(f"Insufficient staging space at {stage_parent}: required "
                         f"{current['staging_bytes']} bytes, free {free} bytes")
    direct_fat = action == 'windows' and options['filesystem'] == 'fat32'
    bytes_written = current['size']
    with tempfile.TemporaryDirectory(prefix='lifus-', dir=stage_parent) as work, contextlib.ExitStack() as stack:
        filesystem = os.path.join(work, 'partition.img')
        contents = os.path.join(work, 'contents')
        os.mkdir(contents)
        if action == 'windows':
            source_fd = stack.enter_context(_open_approved(source, current['source_identity']))
            source_handle = f'/proc/{os.getpid()}/fd/{source_fd}'
        if direct_fat:
            if _source_identity(source_handle) != current['source_identity']:
                raise ValueError('Source changed while opening')
            pinned_records = _members(source_handle, archive_type=current['archive_type'], cancel=cancel)
            if hashlib.sha256(json.dumps(pinned_records, sort_keys=True).encode()).hexdigest() != current['member_fingerprint']:
                raise ValueError('Pinned ISO namespace differs from preflight')
            layout = _prepare_direct(source_handle, pinned_records, contents, current, cancel)
            for node in layout.ordered:
                if node.get('local'):
                    node['local_fd'] = stack.enter_context(_open_approved(node['local'], node['local_identity']))
        elif action == 'windows':
            _extract(source_handle, _members(source_handle, archive_type=current['archive_type'], cancel=cancel),
                     contents, cancel, archive_type=current['archive_type'])
            if options.get('unattend'):
                # Explicit option replaces a pre-existing answer file, never silently.
                for entry in Path(contents).iterdir():
                    if entry.name.casefold() == 'autounattend.xml':
                        if not entry.is_file():
                            raise ValueError('ISO autounattend.xml is not a regular file')
                        entry.unlink()
                Path(contents, 'autounattend.xml').write_bytes(_read_approved(options['unattend'], current['unattend_identity']))
        elif action == 'persistence':
            Path(contents, 'persistence.conf').write_text('/ union\n')
        if action != 'badblocks' and not direct_fat:
            _format_image(filesystem, current['size'], options['filesystem'], options['label'],
                          contents if action == 'persistence' else None, cancel=cancel)
        if action == 'windows' and not direct_fat:
            capture = os.path.join(work, 'payload.wim')
            _run(['wimlib-imagex', 'capture', contents, capture, '--no-acls', '--compress=none'], cancel=cancel)
            _check_cancel(cancel)
            _run(['wimlib-imagex', 'apply', capture, '1', filesystem], cancel=cancel)
        ranges = [] if direct_fat else [(0, current['size'])]
        transfer_size = sum(length for _, length in ranges)
        if direct_fat:
            bytes_written = transfer_size
        _check_cancel(cancel)
        again = _validate(target, options.get('allow_file', False))
        if not _same_identity(current['identity'], again):
            raise ValueError('Target identity changed during staging')
        if source:
            _source_safe(source, target, cancel)
            if _source_identity(source) != current['source_identity']:
                raise ValueError('Source changed during staging')
        if options.get('unattend') and _source_identity(options['unattend']) != current['unattend_identity']:
            raise ValueError('Unattend changed during staging')
        # Keep an exclusive block-device handle throughout writing. No mounts or
        # loop devices are acquired, so cancellation cannot leak either resource.
        st = os.stat(target)
        if [st.st_dev, st.st_ino, st.st_rdev] != again['stat_identity']:
            raise ValueError('Target changed after final validation')
        flags = os.O_RDWR | os.O_NOFOLLOW
        if stat.S_ISBLK(st.st_mode):
            flags |= os.O_EXCL
        if expected_identity is not None:
            unmount.assert_target_identity(target, expected_identity)
        fd = os.open(target, flags)
        destructive_started = False
        try:
            _check_cancel(cancel)
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_rdev) != (st.st_dev, st.st_ino, st.st_rdev):
                raise ValueError('Target changed while opening')
            if expected_identity is not None:
                unmount.assert_target_identity(target, expected_identity, fd=fd)
            final = _validate(target, options.get('allow_file', False))
            if not _same_identity(current['identity'], final):
                raise ValueError('Target identity changed after opening')
            if expected_identity is not None:
                unmount.assert_target_identity(target, expected_identity, fd=fd)
            if action == 'badblocks':
                _badblocks(fd, current['size'], options['passes'], progress, cancel)
            else:
                # Child tools address the already-open inode via /proc rather
                # than reopening an attacker-replaced target pathname.
                handle = f'/proc/{os.getpid()}/fd/{fd}'
                if action == 'persistence':
                    entry = f"start={current['offset']//512}, size={current['size']//512}, type=83\n"
                    _check_cancel(cancel)
                    _run(['sfdisk', '--append', '--no-reread', '--no-tell-kernel', handle], input=entry.encode(), cancel=cancel)
                else:
                    table = 'msdos' if options['partition_table'] == 'mbr' else 'gpt'
                    _check_cancel(cancel)
                    destructive_started = True
                    _run(['parted', '-s', handle, 'mklabel', table], cancel=cancel)
                    hint = 'fat32' if options['filesystem'] == 'fat32' else ('ntfs' if options['filesystem'] in ('ntfs', 'exfat') else 'ext2')
                    _check_cancel(cancel)
                    _run(['parted', '-s', handle, 'unit', 'B', 'mkpart', 'primary', hint,
                          str(current['offset']), str(current['offset']+current['size']-1)], cancel=cancel)
                    if action == 'windows':
                        if current['efi_offset'] is not None:
                            _check_cancel(cancel)
                            _run(['parted', '-s', handle, 'unit', 'B', 'mkpart', 'UEFI_NTFS' if table == 'gpt' else 'primary', 'fat16',
                                  str(current['efi_offset']), str(current['efi_offset']+MiB-1)], cancel=cancel)
                            _check_cancel(cancel)
                            _run(['parted', '-s', handle, 'set', '2', 'esp', 'on'], cancel=cancel)
                        else:
                            _check_cancel(cancel)
                            _run(['parted', '-s', handle, 'set', '1', 'esp' if table == 'gpt' else 'boot', 'on'], cancel=cancel)
                if direct_fat:
                    bytes_written = layout.write(fd, lambda node: _direct_stream(source_handle, node, cancel),
                                                 lambda: _check_cancel(cancel), progress, _sync_for_readback)
                    if (_source_identity(source) != current['source_identity']
                            or _source_identity(source_handle) != current['source_identity']):
                        raise ValueError('Source changed during streaming')
                    if options.get('unattend') and _source_identity(options['unattend']) != current['unattend_identity']:
                        raise ValueError('Unattend changed during streaming')
                    if not _same_identity(current['identity'], _validate(target, options.get('allow_file', False))):
                        raise ValueError('Target identity changed during streaming')
                for phase in (() if direct_fat else ('write', 'verify')):
                    if progress:
                        progress(dict(phase=phase, completed=0, total=transfer_size))
                    done = 0
                    with open(filesystem, 'rb') as stream:
                        for start, length in ranges:
                            stream.seek(start)
                            for position in range(start, start + length, 4 * MiB):
                                _check_cancel(cancel)
                                count = min(4 * MiB, start + length - position)
                                data = stream.read(count)
                                if len(data) != count:
                                    raise IOError('Truncated staged filesystem')
                                if phase == 'write':
                                    _pwrite_all(fd, data, current['offset'] + position)
                                elif os.pread(fd, count, current['offset'] + position) != data:
                                    raise IOError('Filesystem read-back verification failed')
                                done += count
                                if progress:
                                    progress(dict(phase=phase, completed=done, total=transfer_size))
                    if phase == 'write':
                        if progress:
                            progress(dict(phase='sync', completed=done, total=transfer_size))
                        _sync_for_readback(fd)
                if current['efi_offset'] is not None:
                    if progress:
                        progress(dict(phase='finalizing', completed=done, total=current['size']))
                    _check_cancel(cancel)
                    efi = _uefi_ntfs_asset().read_bytes()
                    _pwrite_all(fd, efi, current['efi_offset'])
                    if progress:
                        progress(dict(phase='sync', completed=done, total=current['size']))
                    _sync_for_readback(fd)
                    if os.pread(fd, len(efi), current['efi_offset']) != efi:
                        raise IOError('UEFI:NTFS read-back verification failed')
            if progress:
                progress(dict(phase='sync', completed=current['size'], total=current['size']))
            os.fsync(fd)
            _check_cancel(cancel)
        except Exception as exc:
            if direct_fat and destructive_started and not isinstance(exc, OperationCancelled):
                # Preserve useful exception types without ever reporting partial media as success.
                message = str(exc) + '; target contains incomplete installation media'
                if isinstance(exc, OSError):
                    raise OSError(message) from exc
                exc.args = (message,)
            raise
        finally:
            os.close(fd)
    return dict(status='completed', action=action, target=target, bytes_written=bytes_written,
                verified=True, warning='Bad-block tests cannot certify hardware authenticity or long-term reliability.'
                if action == 'badblocks' else 'Bootability depends on supplied firmware and image; not boot-tested.')
