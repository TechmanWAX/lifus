"""Advanced media operations using argv-only subprocesses and no kernel mounts.

A preview is not authorization: execute regenerates it and revalidates identity.
All filesystem construction happens in private regular files before disk writes.
"""
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


def _run(args, *, input=None):
    completed = subprocess.run(args, input=input, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, check=False)
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


def _members(source):
    # Listing only. Never let an archive extractor choose filesystem destinations.
    text = _run(['7z', 'l', '-slt', '--', source]).decode('utf-8', 'strict')
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
    return records


def _read_member(source, member, maximum=MiB):
    data = _run(['7z', 'x', '-so', '-spd', '--', source, member])
    if len(data) > maximum:
        raise ValueError('Metadata member too large')
    return data


def _source_safe(source, target):
    if not source:
        return
    source_stat = os.stat(source)
    target_stat = os.stat(target)
    if os.path.samefile(source, target):
        raise ValueError('Source is the target')
    if stat.S_ISBLK(target_stat.st_mode):
        tree = json.loads(_run(['lsblk', '--json', '-o', 'PATH,MAJ:MIN', target]))
        def walk(nodes):
            for node in nodes:
                yield node['maj:min']
                yield from walk(node.get('children', []))
        dev = source_stat.st_rdev if stat.S_ISBLK(source_stat.st_mode) else source_stat.st_dev
        if f'{os.major(dev)}:{os.minor(dev)}' in set(walk(tree['blockdevices'])):
            raise ValueError('Source or staging directory is on target disk')


def _source_identity(path):
    s = os.stat(path)
    if not stat.S_ISREG(s.st_mode):
        raise ValueError('Source must be a regular ISO file')
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]


def _persistence_layout(source, target, size, records, requested):
    paths = {r['path'].lower(): r for r in records}
    if 'live/filesystem.squashfs' not in paths or '.disk/info' not in paths:
        raise ValueError('Persistence requires a recognized Debian Live image')
    info = _read_member(source, paths['.disk/info']['path']).decode(errors='replace')
    if 'Debian' not in info:
        raise ValueError('Only Debian Live persistence is supported')
    configs = [r for r in records if r['path'].lower().endswith(('.cfg', '.conf')) and r['size'] < MiB]
    enabled = any(re.search(rb'(?m)^\s*(?:linux|linuxefi|append)\s+[^\n]*\bpersistence\b',
                            _read_member(source, r['path'])) for r in configs)
    if not enabled:
        raise ValueError('ISO must already have a boot entry with the persistence kernel parameter')
    table = json.loads(_run(['sfdisk', '--json', target]))['partitiontable']
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


def plan_operation(action, source, target, **options) -> dict:
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
        _source_safe(xml_path, target)
        if unattend_identity[2] > MiB:
            raise ValueError('Invalid unattend XML: larger than 1 MiB')
        try:
            data = Path(xml_path).read_bytes()
            if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
                raise ValueError('Invalid unattend XML: DTD/entities forbidden')
            if ET.fromstring(data).tag != '{urn:schemas-microsoft-com:unattend}unattend':
                raise ValueError('Invalid unattend XML: wrong root namespace')
        except ET.ParseError as exc:
            raise ValueError('Invalid unattend XML') from exc
        options['unattend'] = xml_path
    source = os.path.realpath(source) if source else None
    _source_safe(source, target)
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
    source_id = None
    if action in ('windows', 'persistence'):
        if not source:
            raise ValueError('This operation requires a source ISO')
        source_id = _source_identity(source)
        records = _members(source)
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
        tools += ['wimlib-imagex'] + (['mcopy'] if filesystem == 'fat32' else [])
        steps[1:1] = ['Extract individual ISO files through stdout into private staging (no mounts)',
                      'Split install.wim over FAT32 limit into 3800 MiB .swm parts',
                      'Populate FAT32 image using mtools; preserve supplied UEFI loaders']
        if filesystem == 'ntfs':
            _uefi_ntfs_asset()
            steps = ['Revalidate target and stage ISO files without mounts',
                     'Populate NTFS using wimlib capture/apply (no FAT32 file size limit)',
                     'Create data partition and upstream UEFI:NTFS EFI partition',
                     'Write and verify both filesystems; original disk data is erased']
    elif action == 'persistence':
        start, length = _persistence_layout(source, target, info['size'], records, options.get('persistence_size'))
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
    normalized = dict(options, filesystem=filesystem, partition_table=table, label=label, passes=passes)
    return dict(action=action, source=source, source_identity=source_id, target=target, identity=info,
                unattend_identity=unattend_identity, efi_offset=efi_offset,
                options=normalized, destructive=True, warning=warning, steps=steps,
                offset=start, size=length, required_tools=tools,
                missing_tools=[t for t in tools if not shutil.which(t)],
                staging_bytes=(length + sum(r['size'] for r in records)*2 if action == 'windows' else length)
                if action != 'badblocks' else 0)


def _uefi_ntfs_asset():
    from .freedos import _assets
    path = _assets() / 'res/uefi/uefi-ntfs.img'
    if not path.is_file() or path.stat().st_size != MiB:
        raise ValueError('Original Rufus UEFI:NTFS image missing or has unexpected size')
    return path


def _format_image(path, size, filesystem, label, contents=None):
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
    _run(args)


def _extract(source, records, destination, cancel):
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
        with open(output, 'xb') as stream, tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(['7z', 'x', '-so', '-spd', '--', source, name],
                                    stdout=subprocess.PIPE, stderr=errors)
            copied = 0
            try:
                while True:
                    _check_cancel(cancel)
                    chunk = proc.stdout.read(MiB)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > record['size']:
                        raise ValueError('Archive output exceeds declared size')
                    stream.write(chunk)
                if proc.wait() or copied != record['size']:
                    raise ValueError('ISO extraction failed or size changed')
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdout.close()


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
    current = plan_operation(plan['action'], plan.get('source'), plan['target'], **plan['options'])
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
    _source_safe(stage_parent, target)
    if action != 'badblocks' and shutil.disk_usage(stage_parent).free < current['staging_bytes']:
        raise ValueError('Insufficient staging space; select staging_dir on another filesystem')
    with tempfile.TemporaryDirectory(prefix='lifus-', dir=stage_parent) as work:
        filesystem = os.path.join(work, 'partition.img')
        contents = os.path.join(work, 'contents')
        os.mkdir(contents)
        if action == 'windows':
            _extract(source, _members(source), contents, cancel)
            for file in Path(contents).rglob('*'):
                if options['filesystem'] == 'fat32' and file.is_file() and file.stat().st_size > FAT_LIMIT:
                    _run(['wimlib-imagex', 'split', str(file), str(file.with_suffix('.swm')), '3800'])
                    file.unlink()
            if options.get('unattend'):
                # Explicit option replaces a pre-existing answer file, never silently.
                for entry in Path(contents).iterdir():
                    if entry.name.casefold() == 'autounattend.xml':
                        if not entry.is_file():
                            raise ValueError('ISO autounattend.xml is not a regular file')
                        entry.unlink()
                shutil.copyfile(options['unattend'], Path(contents, 'autounattend.xml'))
        elif action == 'persistence':
            Path(contents, 'persistence.conf').write_text('/ union\n')
        if action != 'badblocks':
            _format_image(filesystem, current['size'], options['filesystem'], options['label'],
                          contents if action == 'persistence' else None)
        if action == 'windows':
            if options['filesystem'] == 'ntfs':
                capture = os.path.join(work, 'payload.wim')
                _run(['wimlib-imagex', 'capture', contents, capture, '--no-acls', '--compress=none'])
                _check_cancel(cancel)
                _run(['wimlib-imagex', 'apply', capture, '1', filesystem])
            else:
                for entry in sorted(Path(contents).iterdir()):
                    _check_cancel(cancel)
                    _run(['mcopy', '-i', filesystem, '-s', str(entry), '::/'])
        _check_cancel(cancel)
        again = _validate(target, options.get('allow_file', False))
        if not _same_identity(current['identity'], again):
            raise ValueError('Target identity changed during staging')
        if source:
            _source_safe(source, target)
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
                    _run(['sfdisk', '--append', '--no-reread', '--no-tell-kernel', handle], input=entry.encode())
                else:
                    table = 'msdos' if options['partition_table'] == 'mbr' else 'gpt'
                    _check_cancel(cancel)
                    _run(['parted', '-s', handle, 'mklabel', table])
                    hint = 'fat32' if options['filesystem'] == 'fat32' else ('ntfs' if options['filesystem'] in ('ntfs', 'exfat') else 'ext2')
                    _check_cancel(cancel)
                    _run(['parted', '-s', handle, 'unit', 'B', 'mkpart', 'primary', hint,
                          str(current['offset']), str(current['offset']+current['size']-1)])
                    if action == 'windows':
                        if current['efi_offset'] is not None:
                            _check_cancel(cancel)
                            _run(['parted', '-s', handle, 'unit', 'B', 'mkpart', 'UEFI_NTFS' if table == 'gpt' else 'primary', 'fat16',
                                  str(current['efi_offset']), str(current['efi_offset']+MiB-1)])
                            _check_cancel(cancel)
                            _run(['parted', '-s', handle, 'set', '2', 'esp', 'on'])
                        else:
                            _check_cancel(cancel)
                            _run(['parted', '-s', handle, 'set', '1', 'esp' if table == 'gpt' else 'boot', 'on'])
                with open(filesystem, 'rb') as stream:
                    done = 0
                    while True:
                        _check_cancel(cancel)
                        data = stream.read(4*MiB)
                        if not data:
                            break
                        _pwrite_all(fd, data, current['offset']+done)
                        done += len(data)
                        if progress:
                            progress(dict(phase='write', completed=done, total=current['size']))
                _sync_for_readback(fd)
                # Read back the exact written range rather than trusting exit codes.
                with open(filesystem, 'rb') as stream:
                    done = 0
                    while True:
                        _check_cancel(cancel)
                        data = stream.read(4*MiB)
                        if not data:
                            break
                        if os.pread(fd, len(data), current['offset']+done) != data:
                            raise IOError('Filesystem read-back verification failed')
                        done += len(data)
                if current['efi_offset'] is not None:
                    _check_cancel(cancel)
                    efi = _uefi_ntfs_asset().read_bytes()
                    _pwrite_all(fd, efi, current['efi_offset'])
                    _sync_for_readback(fd)
                    if os.pread(fd, len(efi), current['efi_offset']) != efi:
                        raise IOError('UEFI:NTFS read-back verification failed')
            os.fsync(fd)
        finally:
            os.close(fd)
    return dict(status='completed', action=action, target=target, bytes_written=current['size'],
                verified=True, warning='Bad-block tests cannot certify hardware authenticity or long-term reliability.'
                if action == 'badblocks' else 'Bootability depends on supplied firmware and image; not boot-tested.')
