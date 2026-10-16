"""Fail-closed, explicit USB unmount preflight.

Only ordinary umount is used. Checks cover this process's mount namespace and
are best-effort TOCTOU protection, not a lock against automounters or concurrent
privileged topology changes. Callers must still validate immediately before I/O.
A warning from plan_unmount must be shown before invoking unmount_target.
"""
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from . import devices

_SYSTEM = ('/', '/boot', '/boot/efi', '/usr', '/var', '/home', '/etc')


def _number(number):
    return f'{os.major(number)}:{os.minor(number)}'


def _decode(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def _under(path, parent):
    return path == parent or path.startswith(parent.rstrip('/') + '/')


def _mounts():
    result = []
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        for line in stream:
            fields = line.split()
            separator = fields.index('-')
            if separator < 6 or len(fields) < separator + 4:
                raise ValueError('Malformed mountinfo')
            point = _decode(fields[4])
            if not point.startswith('/') or not re.fullmatch(r'\d+:\d+', fields[2]):
                raise ValueError('Unknown mount topology')
            result.append({'id': int(fields[0]), 'parent': int(fields[1]),
                           'device': fields[2], 'root': _decode(fields[3]),
                           'mountpoint': point, 'fstype': fields[separator + 1],
                           'source': _decode(fields[separator + 2])})
    if len({m['id'] for m in result}) != len(result):
        raise ValueError('Duplicate mount IDs')
    return result


def _backing_number(path, *, missing_ok=False):
    path = os.path.abspath(os.fspath(path))
    while True:
        try:
            value = os.stat(path)
            return _number(value.st_rdev if stat.S_ISBLK(value.st_mode) else value.st_dev)
        except FileNotFoundError:
            if not missing_ok or path == '/':
                raise
            path = os.path.dirname(path)


def _plan(target, protected_paths):
    target, st = devices._checked_stat(target)
    if not stat.S_ISBLK(st.st_mode):
        raise ValueError('Target must be a whole USB/removable block device')
    result = subprocess.run(
        ['/usr/bin/lsblk', '--json', '--bytes', '--paths', '--output',
         'NAME,PATH,TYPE,SIZE,MODEL,SERIAL,RM,TRAN,MOUNTPOINTS,RO,MAJ:MIN'],
        check=True, capture_output=True, text=True, timeout=15)
    roots = json.loads(result.stdout)['blockdevices']
    all_nodes = [node for root in roots for node in devices._walk(root)]
    matches = [n for n in all_nodes if n.get('path') == target]
    if len(matches) != 1 or matches[0].get('type') != 'disk':
        raise ValueError('Target is not a unique whole disk')
    disk = matches[0]
    if not any(disk is root for root in roots):
        raise ValueError('Nested or stacked whole disk refused')
    if not (disk.get('rm') in (True, 1, '1') or disk.get('tran') == 'usb'):
        raise ValueError('Target is not USB or removable')
    if disk.get('maj:min') != _number(st.st_rdev):
        raise ValueError('Target device number changed')
    nodes = list(devices._walk(disk))
    topology = []
    numbers = set()
    expected_mounts = set()
    for node in nodes:
        number = node.get('maj:min')
        if not isinstance(number, str) or not re.fullmatch(r'\d+:\d+', number):
            raise ValueError('Unknown device topology')
        if number in numbers or sum(n.get('maj:min') == number for n in all_nodes) != 1:
            raise ValueError('Ambiguous or stacked topology')
        numbers.add(number)
        if node.get('type') != ('disk' if node is disk else 'part'):
            raise ValueError('Stacked device refused')
        if node is not disk and node.get('children'):
            raise ValueError('Nested partition topology refused')
        if node.get('ro') not in (False, 0, '0'):
            raise ValueError('Read-only or unknown read-only state')
        holders = Path('/sys/dev/block') / number / 'holders'
        if not holders.is_dir() or any(holders.iterdir()):
            raise ValueError('Device has holders or unknown holders')
        node_path, node_st = devices._checked_stat(node['path'])
        if not stat.S_ISBLK(node_st.st_mode) or _number(node_st.st_rdev) != number:
            raise ValueError('Device node does not match topology')
        size = int(node['size'])
        if size <= 0:
            raise ValueError('Unknown device size')
        points = node['mountpoints']
        if not isinstance(points, list):
            raise ValueError('Unknown mountpoints')
        for point in points:
            if point is None:
                continue
            if not isinstance(point, str) or not point.startswith('/'):
                raise ValueError('Swap or invalid mountpoint')
            expected_mounts.add((number, point))
        topology.append({'path': node_path, 'device': number, 'size': size,
                         'inode': node_st.st_ino, 'dev': node_st.st_dev,
                         'rdev': node_st.st_rdev})
    mounts = _mounts()
    selected = [m for m in mounts if m['device'] in numbers]
    if {(m['device'], m['mountpoint']) for m in selected} != expected_mounts:
        raise ValueError('lsblk and mountinfo disagree')
    for mount in selected:
        point = mount['mountpoint']
        if os.path.realpath(point) != point or _number(os.stat(point).st_dev) != mount['device']:
            raise ValueError('Mountpoint is hidden, symlinked or changed')
        if point == '/' or any(_under(point, p) for p in _SYSTEM if p != '/'):
            raise ValueError('System filesystem may not be unmounted')
        # Path-based umount cannot safely select an overmounted/hidden mount.
        if sum(m['mountpoint'] == point for m in mounts) != 1:
            raise ValueError('Overmounted mountpoint refused')
        if any(_under(m['mountpoint'], point) and m['device'] not in numbers for m in mounts):
            raise ValueError('Foreign mounted filesystem beneath target mount')
    for path in _SYSTEM:
        if _backing_number(path, missing_ok=True) in numbers:
            raise ValueError('Target backs a system directory')
    for path in protected_paths:
        if _backing_number(path, missing_ok=True) in numbers:
            raise ValueError('Target backs a protected source/staging path')
    with open('/proc/swaps', encoding='utf-8') as stream:
        header = next(stream).split()
        if not header or header[0] != 'Filename':
            raise ValueError('Malformed swap inventory')
        for line in stream:
            fields = line.split()
            if len(fields) != 5:
                raise ValueError('Malformed swap entry')
            if _backing_number(_decode(fields[0])) in numbers:
                raise ValueError('Target contains active swap')
    identity = {'dev': st.st_dev, 'inode': st.st_ino, 'rdev': st.st_rdev,
                'model': disk.get('model') or '', 'serial': disk.get('serial') or '',
                'size': int(disk['size']), 'topology': sorted(topology, key=lambda n: n['path'])}
    selected.sort(key=lambda m: (-m['mountpoint'].count('/'), m['mountpoint']))
    return {'target': target, 'identity': identity, 'mountpoints': [m['mountpoint'] for m in selected],
            'mounts': selected,
            'warning': ('Mounted filesystems will be unmounted before writing; close files and applications using this drive.'
                        if selected else 'No mounted filesystems need unmounting.')}


def plan_unmount(target, *, protected_paths=()):
    """Read-only preflight, usable without root; unsafe/unknown state raises ValueError.

    protected_paths should contain ISO, unattend and staging paths. Nonexistent
    output paths protect their closest existing ancestor's backing filesystem.
    identity is a JSON-serializable snapshot for unmount_target.
    """
    try:
        return _plan(target, protected_paths)
    except (OSError, ValueError, KeyError, TypeError, StopIteration,
            subprocess.SubprocessError) as exc:
        raise ValueError(f'Cannot safely plan unmount: {exc}') from exc


def assert_target_identity(target, expected_identity, *, fd=None) -> dict:
    """Compare a fresh read-only topology and optional open handle to confirmation.

    This does not replace strict validate_target: mounted devices can be planned.
    No new baseline is accepted, and an open handle must have the exact capacity.
    """
    current = plan_unmount(target)
    if current['identity'] != expected_identity:
        raise ValueError('Target identity differs from confirmed preflight')
    if fd is not None:
        import fcntl
        import struct
        opened = os.fstat(fd)
        if (not stat.S_ISBLK(opened.st_mode) or
                (opened.st_dev, opened.st_ino, opened.st_rdev) !=
                (expected_identity['dev'], expected_identity['inode'], expected_identity['rdev'])):
            raise ValueError('Open target identity differs from confirmed preflight')
        capacity = struct.unpack('Q', fcntl.ioctl(fd, 0x80081272, bytes(8)))[0]
        if capacity != expected_identity['size']:
            raise ValueError('Open target capacity differs from confirmed preflight')
    return current


def _cancelled(cancel):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise InterruptedError('Unmount cancelled; already unmounted filesystems remain unmounted')


def unmount_target(target, *, protected_paths=(), expected_identity=None, cancel=None):
    """Unmount only verified target mounts, deepest first, then strictly validate.

    Must run as root (never invokes sudo). cancel may be an Event or predicate.
    Returns the final plan plus unmounted and validated_target. Errors stop at
    once; previously unmounted filesystems are NOT automatically remounted.
    The caller is responsible for presenting the preflight warning first.
    """
    if os.geteuid() != 0:
        raise PermissionError('Unmount requires root privileges')
    _cancelled(cancel)
    protected_paths = tuple(protected_paths)
    initial = plan_unmount(target, protected_paths=protected_paths)
    identity = initial['identity']
    if expected_identity is not None and identity != expected_identity:
        raise ValueError('Target identity differs from confirmed preflight')
    remaining = list(initial['mounts'])
    unmounted = []
    while remaining:
        _cancelled(cancel)
        current = plan_unmount(target, protected_paths=protected_paths)
        if current['identity'] != identity or current['mounts'] != remaining:
            raise ValueError('Target identity, topology or mount IDs changed before unmount')
        mount = remaining[0]
        _cancelled(cancel)
        try:
            subprocess.run(['/usr/bin/umount', '--', mount['mountpoint']],
                           check=True, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"Cannot unmount {mount['mountpoint']}: {exc}; close files and applications using the drive") from exc
        unmounted.append(mount['mountpoint'])
        remaining.pop(0)
        after = plan_unmount(target, protected_paths=protected_paths)
        if after['identity'] != identity or after['mounts'] != remaining:
            raise ValueError('Unmount did not leave the expected device and mount topology')
    _cancelled(cancel)
    final = plan_unmount(target, protected_paths=protected_paths)
    if final['identity'] != identity or final['mounts']:
        raise ValueError('Target changed or was remounted after unmount')
    validated = devices.validate_target(target)
    final = plan_unmount(target, protected_paths=protected_paths)
    if final['identity'] != identity or final['mounts']:
        raise ValueError('Target identity changed or was remounted during final validation')
    return dict(final, identity=identity, unmounted=unmounted, validated_target=validated)
