"""Fail-closed Linux whole-device discovery and destructive-target policy."""
import json
import os
from pathlib import Path
import stat
import subprocess


def _active_device_numbers():
    """Include mount namespaces visible here and swap files' backing devices."""
    active = set()
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        for line in stream:
            active.add(line.split()[2])
    with open('/proc/swaps', encoding='utf-8') as stream:
        next(stream)
        for line in stream:
            path = line.split()[0].replace('\\040', ' ').replace('\\134', '\\')
            st = os.stat(path)
            number = st.st_rdev if stat.S_ISBLK(st.st_mode) else st.st_dev
            active.add(f'{os.major(number)}:{os.minor(number)}')
    # Even overlay-root systems must never expose root's backing device as safe.
    for path in ('/', '/boot', '/boot/efi'):
        if os.path.exists(path):
            number = os.stat(path).st_dev
            active.add(f'{os.major(number)}:{os.minor(number)}')
    return active


def _has_holders(node):
    number = node.get('maj:min')
    if not number:
        return True  # Missing topology is not evidence that a device is safe.
    directory = Path('/sys/dev/block') / number / 'holders'
    return not directory.is_dir() or any(directory.iterdir())


def _walk(node):
    yield node
    for child in node.get('children', []):
        yield from _walk(child)


def list_devices():
    """List whole disks; protected includes internal, mounted and stacked disks.

    No unmounting is attempted. Failure to inspect topology raises, never
    silently returns a permissive device list. Requires util-linux lsblk.
    """
    try:
        result = subprocess.run(
            ['lsblk', '--json', '--bytes', '--paths', '--output',
             'NAME,PATH,TYPE,SIZE,MODEL,SERIAL,RM,TRAN,MOUNTPOINTS,RO,MAJ:MIN'],
            check=True, capture_output=True, text=True, timeout=15)
        roots = json.loads(result.stdout)['blockdevices']
        active = _active_device_numbers()
        disks = {}
        for root in roots:
            for disk in _walk(root):
                if disk.get('type') != 'disk':
                    continue
                nodes = list(_walk(disk))
                mounts = sorted({p for n in nodes for p in
                                 (n.get('mountpoints') or []) if p})
                removable = disk.get('rm') in (True, 1, '1')
                transport = disk.get('tran') or ''
                reasons = []
                if not (removable or transport == 'usb'):
                    reasons.append('not USB or removable')
                if mounts:
                    reasons.append('mounted or swap')
                if any(n.get('maj:min') in active for n in nodes):
                    reasons.append('active filesystem or swap')
                if any(n.get('type') not in ('disk', 'part') or _has_holders(n)
                       for n in nodes):
                    reasons.append('stacked device or unknown holders')
                if any(n.get('ro') in (True, 1, '1') for n in nodes):
                    reasons.append('read-only')
                item = {'path': disk.get('path') or disk['name'],
                        'size': int(disk.get('size') or 0),
                        'model': (disk.get('model') or '').strip(),
                        'serial': (disk.get('serial') or '').strip(),
                        'removable': removable, 'transport': transport,
                        'mountpoints': mounts, 'protected': bool(reasons),
                        'reasons': reasons, 'type': 'disk'}
                previous = disks.get(item['path'])
                if previous and previous['protected']:
                    continue
                disks[item['path']] = item
        return list(disks.values())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise ValueError(f'Cannot safely discover block devices: {exc}') from exc


def _checked_stat(path):
    path = os.path.abspath(os.fspath(path))
    # Reject symlinks in every component, not just the final component.
    if os.path.realpath(path) != path:
        raise ValueError('Symlink paths are not allowed')
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise ValueError(f'Cannot access target: {exc}') from exc
    if stat.S_ISLNK(st.st_mode):
        raise ValueError('Symlink paths are not allowed')
    return path, st


def validate_target(path, *, allow_file=False):
    path, st = _checked_stat(path)
    if stat.S_ISREG(st.st_mode) and allow_file:
        return {'path': path, 'size': st.st_size, 'model': 'Test file',
                'serial': '', 'removable': False, 'transport': 'file',
                'mountpoints': [], 'protected': False, 'type': 'file'}
    if not stat.S_ISBLK(st.st_mode):
        raise ValueError('Target must be a whole USB/removable block device')
    for disk in list_devices():
        if disk['path'] == path:
            if disk['protected']:
                raise ValueError('Unsafe target: ' + ', '.join(disk['reasons']))
            return disk
    raise ValueError('Target is not a discovered whole disk (partitions are refused)')
