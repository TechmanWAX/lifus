"""Native fixed VHD format and file-only conversion tests."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
import uuid


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_fixed_footer_and_byte_exact_roundtrip(self):
        self.assertIsNotNone(importlib.util.find_spec('lifus.containers'))
        from lifus.containers import raw_to_vhd, vhd_to_raw
        data = bytes(range(256)) * 8192
        source, vhd, raw = [self.root / n for n in ('source', 'image.vhd', 'restored')]
        source.write_bytes(data)
        events = []
        result = raw_to_vhd(source, vhd, progress=lambda a, b: events.append((a, b)))
        image = vhd.read_bytes()
        self.assertEqual(image[:-512], data)
        footer = image[-512:]
        self.assertEqual(footer[:8], b'conectix')
        self.assertEqual(struct.unpack_from('>IIQ', footer, 8), (2, 0x10000, 0xffffffffffffffff))
        self.assertEqual(struct.unpack_from('>QQ', footer, 40), (len(data), len(data)))
        self.assertEqual(struct.unpack_from('>HBB', footer, 56), (60, 4, 17))
        self.assertEqual(struct.unpack_from('>I', footer, 60)[0], 2)
        self.assertEqual(struct.unpack_from('>I', footer, 64)[0], (~sum(footer[:64] + bytes(4) + footer[68:])) & 0xffffffff)
        self.assertEqual(uuid.UUID(bytes=footer[68:84]).version, 4)
        self.assertEqual(footer[84:], bytes(428))
        self.assertEqual(result['sha256'], hashlib.sha256(image).hexdigest())
        self.assertEqual(result['bytes_written'], len(image))
        self.assertEqual(events[0], (0, len(data)))
        self.assertEqual(events[-1], (len(data), len(data)))
        result = vhd_to_raw(vhd, raw)
        self.assertEqual(raw.read_bytes(), data)
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())

    def test_replaced_output_is_not_success(self):
        from lifus.containers import raw_to_vhd
        source, out = self.root / 'source', self.root / 'out'
        source.write_bytes(bytes(512))
        def replace(done, total):
            if done:
                out.unlink()
                out.write_bytes(b'replacement')
        with self.assertRaisesRegex(ValueError, 'Destination changed'):
            raw_to_vhd(source, out, progress=replace)
        self.assertEqual(out.read_bytes(), b'replacement')

    def test_io_bounds_sync_and_geometry(self):
        from unittest.mock import patch
        import os
        from lifus import containers
        source, out = self.root / 'source', self.root / 'out'
        source.write_bytes(bytes(4096))
        events = []
        with patch.object(containers, 'CHUNK_SIZE', 512), \
             patch.object(containers.os, 'fsync', wraps=os.fsync) as sync:
            containers.raw_to_vhd(source, out, progress=lambda a, b: events.append(a))
            sync.assert_called_once()
        self.assertTrue(all(b - a <= 512 for a, b in zip(events, events[1:])))
        for sectors, expected in ((1, (0, 4, 17)), (4096, (60, 4, 17)),
                                  (1032192, (1024, 16, 63)),
                                  (65535 * 16 * 63, (16191, 16, 255)),
                                  (65535 * 16 * 255 + 1, (65535, 16, 255))):
            self.assertEqual(containers._geometry(sectors * 512), expected)

    def test_malformed_and_unsupported(self):
        from lifus.containers import raw_to_vhd, vhd_to_raw
        source, vhd, out = [self.root / n for n in ('src', 'vhd', 'out')]
        source.write_bytes(bytes(4096))
        raw_to_vhd(source, vhd)
        original = vhd.read_bytes()
        for offset, fmt, value in ((8, '>I', 0), (12, '>I', 2),
                                   (16, '>Q', 0), (40, '>Q', 4097),
                                   (48, '>Q', 4608), (60, '>I', 3),
                                   (60, '>I', 4)):
            with self.subTest(offset=offset, value=value):
                image = bytearray(original)
                struct.pack_into(fmt, image, len(image) - 512 + offset, value)
                image[-448:-444] = bytes(4)
                struct.pack_into('>I', image, len(image) - 448,
                                 (~sum(image[-512:])) & 0xffffffff)
                vhd.write_bytes(image)
                with self.assertRaises(ValueError):
                    vhd_to_raw(vhd, out)
                self.assertFalse(out.exists())
        for image in (original[:-1], original + b'x', b'bad',
                      original[:-512] + bytes(512),
                      original[:-1] + b'x'):
            vhd.write_bytes(image)
            with self.assertRaises(ValueError):
                vhd_to_raw(vhd, out)
        for magic, label in ((b'vhdxfile', 'VHDX'), (bytes(4) + b'SignedImage ', 'FFU')):
            source.write_bytes(magic.ljust(1024, b'\0'))
            for converter in (raw_to_vhd, vhd_to_raw):
                with self.assertRaisesRegex(ValueError, label):
                    converter(source, out)
        for size in (0, 1, 513):
            source.write_bytes(bytes(size))
            with self.assertRaises(ValueError):
                raw_to_vhd(source, out)

    def test_file_safety_and_cancel(self):
        import os
        import threading
        from lifus.containers import raw_to_vhd, vhd_to_raw, CancelledError
        source, out = self.root / 'source', self.root / 'out'
        source.write_bytes(bytes(4096))
        for converter in (raw_to_vhd, vhd_to_raw):
            if converter is vhd_to_raw:
                raw_to_vhd(source, self.root / 'input.vhd')
                source = self.root / 'input.vhd'
            for existing in (source, out):
                if existing == out:
                    os.link(source, out)
                before = existing.read_bytes()
                with self.assertRaises((OSError, ValueError)):
                    converter(source, existing)
                self.assertEqual(existing.read_bytes(), before)
                if existing == out:
                    out.unlink()
            link = self.root / 'link'
            link.symlink_to(source)
            with self.assertRaises((OSError, ValueError)):
                converter(link, out)
            link.unlink()
            link.symlink_to(self.root / 'absent')
            with self.assertRaises((OSError, ValueError)):
                converter(source, link)
            self.assertFalse((self.root / 'absent').exists())
            link.unlink()
            link.symlink_to(self.root, target_is_directory=True)
            with self.assertRaises((OSError, ValueError)):
                converter(source, link / 'out')
            link.unlink()
            cancel = threading.Event()
            def progress(done, total):
                if done:
                    cancel.set()
            with self.assertRaises(CancelledError):
                converter(source, out, progress=progress, cancel=cancel)
            self.assertFalse(out.exists())
            def replace_output(done, total):
                if done:
                    out.unlink()
                    out.write_bytes(b'not owned')
                    cancel.set()
            cancel.clear()
            with self.assertRaises(CancelledError):
                converter(source, out, progress=replace_output, cancel=cancel)
            self.assertEqual(out.read_bytes(), b'not owned')
            out.unlink()
        with self.assertRaises((OSError, ValueError)):
            raw_to_vhd(self.root, out)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            raw_to_vhd(fifo, out)


if __name__ == '__main__':
    unittest.main()
