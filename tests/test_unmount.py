"""Unmount tests use exclusively synthetic topology and intercepted commands."""
import io
import json
import os
import stat
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lifus import unmount


class UnmountTests(unittest.TestCase):
    def setUp(self):
        self.real_stat = os.stat
        self.real_open = open
        self.target = '/dev/fake-usb'
        self.part = {'path': '/dev/fake-usb1', 'name': '/dev/fake-usb1',
                     'type': 'part', 'size': 900, 'ro': False, 'maj:min': '8:17',
                     'mountpoints': ['/media/USB']}
        self.disk = {'path': self.target, 'name': self.target, 'type': 'disk',
                     'size': 1000, 'model': 'Fake', 'serial': '123', 'rm': True,
                     'tran': 'usb', 'ro': False, 'maj:min': '8:16',
                     'mountpoints': [None], 'children': [self.part]}
        self.roots = [self.disk]
        self.mountinfo = '20 1 8:17 / /media/USB rw - vfat /dev/fake-usb1 rw\n'
        self.swaps = 'Filename Type Size Used Priority\n'
        self.holders = []
        self.stats = {}
        self.commands = []
        self.inode = 100
        self.after_lsblk = None
        self.after_umount = None
        for name, replacement in (
            ('builtins.open', self.open),
            ('lifus.unmount.subprocess.run', self.run_command),
            ('lifus.unmount.os.stat', self.stat),
            ('lifus.unmount.os.lstat', self.stat),
            ('lifus.unmount.os.path.realpath', lambda x: os.fspath(x)),
            ('lifus.unmount.os.geteuid', lambda: 0),
            ('lifus.unmount.Path.is_dir', lambda p: True),
            ('lifus.unmount.Path.iterdir', lambda p: iter(self.holders)),
            ('lifus.unmount.devices.validate_target', lambda p: {'path': p}),
        ):
            mock = patch(name, replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def stat(self, path, *args, **kwargs):
        path = os.fspath(path)
        if path in self.stats:
            return self.stats[path]
        if path in (self.target, self.part['path']):
            return SimpleNamespace(st_mode=stat.S_IFBLK, st_dev=5,
                                   st_ino=self.inode,
                                   st_rdev=os.makedev(8, 16 if path == self.target else 17))
        if path.startswith('/media/USB'):
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=os.makedev(8, 17), st_ino=3, st_rdev=0)
        if path in unmount._SYSTEM:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=os.makedev(0, 1), st_ino=1, st_rdev=0)
        return self.real_stat(path, *args, **kwargs)

    def open(self, path, *args, **kwargs):
        data = {'/proc/self/mountinfo': self.mountinfo, '/proc/swaps': self.swaps}
        if os.fspath(path) not in data:
            return self.real_open(path, *args, **kwargs)
        return io.StringIO(data[os.fspath(path)])

    def run_command(self, args, **kwargs):
        self.commands.append(args)
        if os.path.basename(args[0]) == 'lsblk':
            result = SimpleNamespace(stdout=json.dumps({'blockdevices': self.roots}))
            if self.after_lsblk:
                self.after_lsblk()
            return result
        self.assertEqual(args[:2], ['/usr/bin/umount', '--'])
        point = args[2]
        self.mountinfo = ''.join(line for line in self.mountinfo.splitlines(True)
                                 if line.split()[4] != point)
        for node in [self.disk, self.part]:
            node['mountpoints'] = [p for p in node['mountpoints'] if p != point]
        if self.after_umount:
            self.after_umount()
        return SimpleNamespace(stdout='')

    def actions(self):
        return [c for c in self.commands if c[0] == '/usr/bin/umount']

    def test_real_temporary_source_path_backing_mapping_is_protected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'source.iso')
            with self.real_open(source, 'wb') as stream:
                stream.write(b'not an actual ISO; never written to a disk')
            original = self.real_stat(source)
            self.stats[source] = SimpleNamespace(st_mode=original.st_mode,
                st_dev=os.makedev(8, 17), st_rdev=0)
            try:
                with self.assertRaises(ValueError):
                    unmount.unmount_target(self.target, protected_paths=[source])
                self.assertEqual(self.actions(), [])
            finally:
                del self.stats[source]

    def test_child_identity_changes_refused(self):
        initial = unmount.plan_unmount(self.target)['identity']
        self.part['size'] = 800
        with self.assertRaises(ValueError):
            unmount.unmount_target(self.target, expected_identity=initial)
        self.assertEqual(self.actions(), [])

    def test_remount_after_first_unmount_stops_without_retry(self):
        def remount():
            self.part['mountpoints'] = ['/media/USB']
            self.mountinfo = '99 1 8:17 / /media/USB rw - vfat /dev/fake-usb1 rw\n'
        self.after_umount = remount
        with self.assertRaises(ValueError):
            unmount.unmount_target(self.target)
        self.assertEqual(len(self.actions()), 1)

    def test_no_mounts_is_noop_but_still_validated(self):
        self.part['mountpoints'] = []
        self.mountinfo = ''
        with patch('lifus.unmount.devices.validate_target', return_value={'path': self.target}) as validate:
            self.assertEqual(unmount.unmount_target(self.target)['unmounted'], [])
        validate.assert_called_once_with(self.target)
        self.assertEqual(self.actions(), [])

    def test_cancel_between_unmounts(self):
        import threading
        cancel = threading.Event()
        self.part['mountpoints'].append('/media/USB/sub')
        self.mountinfo += '21 20 8:17 /sub /media/USB/sub rw - vfat /dev/fake-usb1 rw\n'
        self.after_umount = cancel.set
        with self.assertRaises(InterruptedError):
            unmount.unmount_target(self.target, cancel=cancel)
        self.assertEqual(self.actions(), [['/usr/bin/umount', '--', '/media/USB/sub']])

    def test_mountpoint_escapes_and_shell_metacharacters_are_literal(self):
        point = '/media/USB name;$(not-a-command)'
        self.part['mountpoints'] = [point]
        self.mountinfo = '20 1 8:17 / /media/USB\\040name;$(not-a-command) rw - vfat /dev/fake-usb1 rw\n'
        # Fixture removes escaped mountinfo separately, like the kernel does.
        self.after_umount = lambda: setattr(self, 'mountinfo', '')
        self.assertEqual(unmount.unmount_target(self.target)['unmounted'], [point])
        self.assertEqual(self.actions(), [['/usr/bin/umount', '--', point]])

    def test_block_swap_refused(self):
        self.swaps += '/dev/fake-usb1 partition 100 0 -2\n'
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)

    def test_missing_holders_and_inventory_read_failures_refused(self):
        with patch('lifus.unmount.Path.is_dir', return_value=False):
            with self.assertRaises(ValueError):
                unmount.plan_unmount(self.target)
        for filename in ['/proc/swaps', '/proc/self/mountinfo']:
            def broken(path, *args, **kwargs):
                if path == filename:
                    raise PermissionError('cannot inspect')
                return self.open(path, *args, **kwargs)
            with patch('builtins.open', side_effect=broken):
                with self.assertRaises(ValueError):
                    unmount.plan_unmount(self.target)

    def test_duplicate_and_malformed_topology_refused(self):
        self.roots.append(self.disk)
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)
        self.roots.pop()
        self.mountinfo = 'malformed\n'
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)

    def test_protected_missing_output_uses_existing_parent(self):
        self.stats['/stage'] = SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=os.makedev(8, 17))
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target, protected_paths=['/stage/not-yet-created/out'])

    def test_system_backing_stat_refused_without_mountpoint_name(self):
        self.stats['/home'] = SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=os.makedev(8, 17))
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)

    def test_overmounted_same_device_refused(self):
        self.mountinfo += '21 20 8:17 /other /media/USB rw - vfat /dev/fake-usb1 rw\n'
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)

    def test_size_serial_model_and_child_changes_between_actions_refused(self):
        for key, value in [('size', 1001), ('serial', 'new'), ('model', 'new')]:
            with self.subTest(key=key):
                initial = unmount.plan_unmount(self.target)['identity']
                old = self.disk[key]
                self.disk[key] = value
                with self.assertRaises(ValueError):
                    unmount.unmount_target(self.target, expected_identity=initial)
                self.disk[key] = old
        self.assertEqual(self.actions(), [])

    def test_readonly_partition_and_swap_marker_refused(self):
        self.part['ro'] = True
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)
        self.part['ro'] = False
        self.part['mountpoints'] = ['[SWAP]']
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)

    def test_hidden_or_replaced_mountpoint_refused(self):
        self.stats['/media/USB'] = SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=os.makedev(0, 99))
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)

    def test_symlink_mountpoint_refused(self):
        with patch('lifus.unmount.os.path.realpath', side_effect=lambda p: '/other' if p == '/media/USB' else p):
            with self.assertRaises(ValueError):
                unmount.plan_unmount(self.target)

    def test_nested_disk_refused(self):
        self.roots = [dict(self.disk, path='/dev/other', children=[self.disk], **{'maj:min': '8:0'})]
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)

    def test_unmount_normal_exact_command_and_final_validation(self):
        snapshot = unmount.plan_unmount(self.target)
        with patch('lifus.unmount.devices.validate_target', return_value={'path': self.target}) as validate:
            result = unmount.unmount_target(self.target, expected_identity=snapshot['identity'])
        self.assertEqual(self.actions(), [['/usr/bin/umount', '--', '/media/USB']])
        self.assertEqual(result['unmounted'], ['/media/USB'])
        self.assertEqual(result['mountpoints'], [])
        validate.assert_called_once_with(self.target)

    def test_root_required_and_cancellation(self):
        with patch('lifus.unmount.os.geteuid', return_value=1000):
            with self.assertRaises(PermissionError):
                unmount.unmount_target(self.target)
        with self.assertRaises(InterruptedError):
            unmount.unmount_target(self.target, cancel=lambda: True)
        self.assertEqual(self.actions(), [])

    def test_wrong_identity_never_unmounts(self):
        identity = unmount.plan_unmount(self.target)['identity']
        identity['serial'] = 'other'
        with self.assertRaises(ValueError):
            unmount.unmount_target(self.target, expected_identity=identity)
        self.assertEqual(self.actions(), [])

    def test_deepest_first(self):
        self.part['mountpoints'].append('/media/USB/sub')
        self.mountinfo += '21 20 8:17 /sub /media/USB/sub rw - vfat /dev/fake-usb1 rw\n'
        result = unmount.unmount_target(self.target)
        self.assertEqual(result['unmounted'], ['/media/USB/sub', '/media/USB'])

    def test_remount_new_id_before_action_refused(self):
        count = 0
        def change():
            nonlocal count
            count += 1
            if count == 2:
                self.mountinfo = self.mountinfo.replace('20 1 ', '30 1 ')
        self.after_lsblk = change
        with self.assertRaises(ValueError):
            unmount.unmount_target(self.target)
        self.assertEqual(self.actions(), [])

    def test_device_replacement_before_action_refused(self):
        self.after_lsblk = lambda: setattr(self, 'inode', self.inode + 1)
        with self.assertRaises(ValueError):
            unmount.unmount_target(self.target)
        self.assertEqual(self.actions(), [])

    def test_busy_failure_does_not_retry_force_or_lazy(self):
        real = self.run_command
        def run(args, **kwargs):
            if args[0] == '/usr/bin/umount':
                self.commands.append(args)
                raise subprocess.CalledProcessError(32, args, stderr='busy')
            return real(args, **kwargs)
        with patch('lifus.unmount.subprocess.run', side_effect=run):
            with self.assertRaises(ValueError):
                unmount.unmount_target(self.target)
        self.assertEqual(self.actions(), [['/usr/bin/umount', '--', '/media/USB']])

    def test_success_status_without_unmount_is_refused(self):
        real = self.run_command
        def run(args, **kwargs):
            if args[0] == '/usr/bin/umount':
                return SimpleNamespace(stdout='')
            return real(args, **kwargs)
        with patch('lifus.unmount.subprocess.run', side_effect=run):
            with self.assertRaises(ValueError):
                unmount.unmount_target(self.target)

    def test_final_strict_validator_cannot_be_bypassed(self):
        with patch('lifus.unmount.devices.validate_target', side_effect=ValueError('still unsafe')):
            with self.assertRaisesRegex(ValueError, 'still unsafe'):
                unmount.unmount_target(self.target)

    def test_preflight_is_read_only_and_warns(self):
        with patch('lifus.unmount.os.geteuid', return_value=1000):
            result = unmount.plan_unmount(self.target)
        self.assertEqual(result['target'], self.target)
        self.assertEqual(result['mountpoints'], ['/media/USB'])
        self.assertTrue(result['identity'])
        self.assertIn('unmount', result['warning'].lower())
        self.assertEqual(self.actions(), [])

    def test_non_usb_readonly_stacked_unknown_and_partition_refused(self):
        for key, value in [('ro', True), ('type', 'part'), ('maj:min', None),
                           ('children', [dict(self.part, type='crypt')])]:
            with self.subTest(key=key):
                old = self.disk[key]
                self.disk[key] = value
                with self.assertRaises(ValueError):
                    unmount.plan_unmount(self.target)
                self.disk[key] = old
        self.disk.update(rm=False, tran='sata')
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)

    def test_holders_and_symlinks_refused(self):
        self.holders.append('dm-0')
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)
        self.holders.clear()
        with patch('lifus.unmount.os.path.realpath', return_value='/dev/other'):
            with self.assertRaises(ValueError):
                unmount.plan_unmount(self.target)

    def test_system_mounts_refused(self):
        for point in ['/', '/boot', '/boot/efi', '/usr', '/var', '/home', '/home/user']:
            with self.subTest(point=point):
                self.part['mountpoints'] = [point]
                self.mountinfo = f'20 1 8:17 / {point} rw - ext4 /dev/fake-usb1 rw\n'
                with self.assertRaises(ValueError):
                    unmount.plan_unmount(self.target)

    def test_source_backed_by_target_refused(self):
        self.stats['/iso/source.iso'] = SimpleNamespace(st_mode=stat.S_IFREG,
            st_dev=os.makedev(8, 17), st_rdev=0)
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target, protected_paths=['/iso/source.iso'])

    def test_swap_file_backed_by_target_refused(self):
        self.stats['/swapfile'] = SimpleNamespace(st_mode=stat.S_IFREG,
            st_dev=os.makedev(8, 17), st_rdev=0)
        self.swaps += '/swapfile file 100 0 -2\n'
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)

    def test_foreign_mount_subtree_and_disagreement_refused(self):
        self.mountinfo += '21 20 0:55 / /media/USB/foreign rw - tmpfs tmpfs rw\n'
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)
        self.mountinfo = ''
        with self.assertRaises(ValueError):
            unmount.plan_unmount(self.target)
