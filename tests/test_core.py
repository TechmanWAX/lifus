import bz2
import gzip
import hashlib
import importlib.util
import lzma
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_target_policy_and_discovery(self):
        self.assertIsNotNone(importlib.util.find_spec('lifus.devices'), 'devices missing')
        from lifus import devices
        target = self.root / 'target'
        target.write_bytes(b'0' * 32)
        with self.assertRaises(ValueError):
            devices.validate_target(target)
        self.assertEqual(devices.validate_target(target, allow_file=True)['size'], 32)
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            devices.validate_target(link, allow_file=True)
        tree = {'blockdevices': [
            {'path': '/dev/a', 'type': 'disk', 'size': 4096, 'rm': True,
             'tran': 'usb', 'children': [{'path': '/dev/a1', 'type': 'part',
                                         'mountpoints': ['/']} ]},
            {'path': '/dev/b', 'type': 'disk', 'size': 4096, 'rm': True,
             'tran': 'usb', 'children': [{'path': '/dev/b1', 'type': 'part',
                 'children': [{'path': '/dev/dm-0', 'type': 'crypt'}]}]},
            {'path': '/dev/c', 'type': 'disk', 'size': 4096, 'rm': False, 'tran': 'sata'},
            {'path': '/dev/d', 'type': 'disk', 'size': 4096, 'rm': False, 'tran': 'usb'}]}
        import json
        result = type('Result', (), {'stdout': json.dumps(tree)})()
        with patch.object(devices.subprocess, 'run', return_value=result), \
             patch.object(devices, '_active_device_numbers', return_value=set()), \
             patch.object(devices, '_has_holders', return_value=False):
            found = devices.list_devices()
        self.assertEqual([d['protected'] for d in found], [True, True, True, False])
        self.assertEqual(found[0]['mountpoints'], ['/'])

    def test_verified_compressed_write_and_refusals(self):
        self.assertIsNotNone(importlib.util.find_spec('lifus.writer'), 'writer missing')
        from lifus import writer
        data = b'payload' * 10000
        source = self.root / 'image.gz'
        source.write_bytes(gzip.compress(data))
        target = self.root / 'target'
        target.write_bytes(b'X' * (len(data) + 32))
        events = []
        result = writer.write_image(source, target, allow_file=True,
                                    progress=lambda a, b: events.append((a, b)))
        self.assertTrue(result['verified'])
        self.assertEqual(result['bytes_written'], len(data))
        self.assertEqual(target.read_bytes(), data + b'X' * 32)
        self.assertEqual([event for event in events if isinstance(event[0], int)][-1],
                         (len(data), len(data)))
        self.assertEqual(events[-1][0]['phase'], 'verify')
        with self.assertRaises(ValueError):
            writer.write_image(source, target)
        with self.assertRaises(ValueError):
            writer.write_image(target, target, allow_file=True)
        tiny = self.root / 'tiny'
        tiny.write_bytes(b'abc')
        with self.assertRaises(ValueError):
            writer.write_image(source, tiny, allow_file=True)
        self.assertEqual(tiny.read_bytes(), b'abc')
        event = threading.Event()
        event.set()
        before = target.read_bytes()
        with self.assertRaises(InterruptedError):
            writer.write_image(source, target, allow_file=True, cancel=event)
        self.assertEqual(target.read_bytes(), before)

    def test_backup_is_read_only_and_never_clobbers(self):
        from lifus import writer
        self.assertTrue(hasattr(writer, 'backup_image'), 'backup missing')
        source = self.root / 'source'
        data = b'backup' * 1234
        source.write_bytes(data)
        destination = self.root / 'backup.img'
        events = []
        result = writer.backup_image(source, destination,
                                     progress=lambda a, b: events.append((a, b)))
        self.assertEqual(result['bytes_written'], len(data))
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(source.read_bytes(), data)
        self.assertEqual([event for event in events if isinstance(event[0], int)][-1],
                         (len(data), len(data)))
        self.assertEqual(events[-1][0]['phase'], 'sync')
        with self.assertRaises(FileExistsError):
            writer.backup_image(source, destination)
        event = threading.Event()
        with self.assertRaises(InterruptedError):
            writer.backup_image(source, self.root / 'cancelled', cancel=event,
                                progress=lambda a, b: event.set())
        self.assertFalse((self.root / 'cancelled').exists())

    def test_image_helpers_refuse_symlinks(self):
        from lifus import images
        source = self.root / 'source.img'
        source.write_bytes(b'image')
        link = self.root / 'link.img'
        link.symlink_to(source)
        for function in (images.inspect_image, images.checksum):
            with self.subTest(function=function.__name__), self.assertRaises(ValueError):
                function(link)
        with self.assertRaises(ValueError):
            with images.open_image(link) as stream:
                stream.read()

    def test_open_image_returns_binary_stream(self):
        from lifus import images
        for compress in (lambda x: x, gzip.compress, bz2.compress, lzma.compress):
            source = self.root / 'source.img'
            source.write_bytes(compress(b'abc'))
            stream = images.open_image(source)
            try:
                self.assertTrue(hasattr(stream, 'read'), 'must return a binary stream')
                self.assertEqual(stream.read(), b'abc')
            finally:
                if hasattr(stream, 'close'):
                    stream.close()

    def test_images_decode_and_inspect(self):
        self.assertIsNotNone(importlib.util.find_spec('lifus'), 'core package missing')
        from lifus import images
        payload = b'image data\0' * 1024
        for suffix, encode in [('.img', lambda x: x), ('.gz', gzip.compress),
                               ('.bz2', bz2.compress), ('.xz', lzma.compress)]:
            p = self.root / ('disk.img' + suffix)
            p.write_bytes(encode(payload))
            with images.open_image(p) as stream:
                self.assertEqual(stream.read(), payload)
            info = images.inspect_image(p)
            self.assertEqual(info['size'], len(payload))
            self.assertEqual(images.checksum(p), hashlib.sha256(p.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
