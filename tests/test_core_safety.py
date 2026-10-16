"""Destructive-path regression tests: only private regular tempfiles are written."""
import bz2
import fcntl
import gzip
import json
import lzma
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from lifus import devices, writer


class WriterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.target = self.root / 'target'
        self.data = b'content\x00' * 1000
        self.source.write_bytes(self.data)
        self.target.write_bytes(b'X' * len(self.data))

    def write(self, **kwargs):
        return writer.write_image(self.source, self.target, allow_file=True, **kwargs)

    def test_all_formats_verified(self):
        for compress in (lambda x: x, gzip.compress, bz2.compress, lzma.compress):
            with self.subTest(compressor=compress):
                self.source.write_bytes(compress(self.data))
                self.assertTrue(self.write()['verified'])
                self.assertEqual(self.target.read_bytes(), self.data)

    def test_corrupt_compression_leaves_target_untouched(self):
        for compress in (gzip.compress, bz2.compress, lzma.compress):
            self.source.write_bytes(compress(self.data)[:-8])
            with self.assertRaises((EOFError, OSError, lzma.LZMAError)):
                self.write()
            self.assertEqual(self.target.read_bytes(), b'X' * len(self.data))

    def test_empty_source_refused(self):
        self.source.write_bytes(b'')
        with self.assertRaises(ValueError):
            self.write()
        self.assertEqual(self.target.read_bytes(), b'X' * len(self.data))

    def test_hardlink_samefile_refused(self):
        self.target.unlink()
        os.link(self.source, self.target)
        with self.assertRaises(ValueError):
            self.write()
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_target_symlink_refused(self):
        original = self.root / 'original'
        self.target.rename(original)
        self.target.symlink_to(original)
        with self.assertRaises(ValueError):
            self.write()
        self.assertEqual(original.read_bytes(), b'X' * len(self.data))

    def test_parent_symlink_refused(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            writer.write_image(self.source, alias / 'target', allow_file=True)

    def test_lock_contention_refused_before_write(self):
        with self.target.open('r+b') as locked:
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.write()
        self.assertEqual(self.target.read_bytes(), b'X' * len(self.data))

    def test_target_inode_swap_is_refused(self):
        original_open = os.open
        swapped = False
        def raced_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if os.fspath(path) == str(self.target) and flags & os.O_RDWR and not swapped:
                swapped = True
                self.target.rename(self.root / 'old')
                self.target.write_bytes(b'Y' * len(self.data))
            return original_open(path, flags, *args, **kwargs)
        with patch.object(writer.os, 'open', side_effect=raced_open):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.write()
        self.assertEqual(self.target.read_bytes(), b'Y' * len(self.data))
        self.assertEqual((self.root / 'old').read_bytes(), b'X' * len(self.data))

    def test_prewrite_revalidation_refuses_new_protection(self):
        real_validate = devices.validate_target
        count = 0
        def changed(*args, **kwargs):
            nonlocal count
            count += 1
            if count > 1:
                raise ValueError('New mount appeared')
            return real_validate(*args, **kwargs)
        with patch.object(devices, 'validate_target', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'New mount'):
                self.write()
        self.assertEqual(self.target.read_bytes(), b'X' * len(self.data))

    def test_short_writes_are_completed(self):
        original_write = os.write
        with patch.object(writer.os, 'write', side_effect=lambda fd, data: original_write(fd, data[:13])):
            self.assertTrue(self.write()['verified'])
        self.assertEqual(self.target.read_bytes(), self.data)

    def test_readback_detects_corruption(self):
        with patch.object(writer.os, 'read', side_effect=lambda fd, count: b'Z' * count):
            with self.assertRaisesRegex(OSError, 'verification failed'):
                self.write()

    def test_cancel_during_write_never_reports_success(self):
        event = threading.Event()
        def progress(done, total):
            if done:
                event.set()
        with self.assertRaises(InterruptedError):
            self.write(progress=progress, cancel=event)

    def test_backup_source_opened_read_only(self):
        original_open = os.open
        flags_seen = []
        def checked(path, flags, *args, **kwargs):
            if os.fspath(path) == str(self.source):
                flags_seen.append(flags & os.O_ACCMODE)
            return original_open(path, flags, *args, **kwargs)
        with patch.object(writer.os, 'open', side_effect=checked):
            writer.backup_image(self.source, self.root / 'backup')
        self.assertEqual(flags_seen, [os.O_RDONLY])

    def test_backup_symlink_never_clobbers(self):
        link = self.root / 'backup'
        link.symlink_to(self.source)
        with self.assertRaises((ValueError, FileExistsError)):
            writer.backup_image(self.source, link)
        self.assertEqual(self.source.read_bytes(), self.data)


class DiscoverySafetyTests(unittest.TestCase):
    def discover(self, children=(), **extra):
        disk = {'path': '/dev/test', 'name': '/dev/test', 'type': 'disk',
                'size': 100000, 'tran': 'usb', 'rm': False,
                'maj:min': '8:0', 'children': list(children)}
        disk.update(extra)
        result = type('Result', (), {'stdout': json.dumps({'blockdevices': [disk]})})()
        with patch.object(devices.subprocess, 'run', return_value=result), \
             patch.object(devices, '_active_device_numbers', return_value={'8:1'}), \
             patch.object(devices, '_has_holders', return_value=False):
            return devices.list_devices()[0]

    def test_root_boot_swap_and_other_mounts_protected(self):
        for mount in ('/', '/boot', '/boot/efi', '[SWAP]', '/media/usb'):
            with self.subTest(mount=mount):
                self.assertTrue(self.discover([{'type': 'part', 'mountpoints': [mount]}])['protected'])

    def test_active_device_without_mountpoint_protected(self):
        self.assertTrue(self.discover([{'type': 'part', 'maj:min': '8:1'}])['protected'])

    def test_stacked_devices_protected(self):
        for kind in ('crypt', 'lvm', 'raid1', 'mpath'):
            with self.subTest(kind=kind):
                self.assertTrue(self.discover([{'type': 'part', 'children': [{'type': kind}]}])['protected'])

    def test_internal_and_readonly_protected(self):
        self.assertTrue(self.discover(tran='nvme')['protected'])
        self.assertTrue(self.discover(ro=True)['protected'])
        self.assertFalse(self.discover()['protected'])
        self.assertFalse(self.discover(tran='mmc', rm=True)['protected'])

    def test_discovery_failures_fail_closed(self):
        with patch.object(devices.subprocess, 'run', side_effect=OSError('missing lsblk')):
            with self.assertRaises(ValueError):
                devices.list_devices()

    def test_block_partition_refused(self):
        fake = type('Stat', (), {'st_mode': stat.S_IFBLK})()
        with patch.object(devices, '_checked_stat', return_value=('/dev/test1', fake)), \
             patch.object(devices, 'list_devices', return_value=[{'path': '/dev/test', 'protected': False}]):
            with self.assertRaisesRegex(ValueError, 'partitions'):
                devices.validate_target('/dev/test1')


if __name__ == '__main__':
    unittest.main()
