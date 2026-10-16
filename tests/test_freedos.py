"""Real file-image tests: never open physical disks."""
import importlib
import pathlib
import shutil
import struct
import subprocess
import tempfile
import unittest


class FreeDOSTests(unittest.TestCase):
    def test_native_freedos_builder_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('lifus.freedos'))

    @unittest.skipUnless(shutil.which('mcopy') and shutil.which('mkfs.fat'), 'mtools/dosfstools required')
    def test_build_bootable_layout_and_kernel(self):
        from lifus.freedos import create_image
        with tempfile.TemporaryDirectory() as td:
            image = pathlib.Path(td) / 'dos.img'
            result = create_image(str(image))
            self.assertEqual(result['size'], 64 * 1024 * 1024)
            with image.open('rb') as f:
                mbr = f.read(512)
                self.assertEqual(mbr[510:], b'\x55\xaa')
                self.assertEqual(mbr[446], 0x80)
                self.assertEqual(struct.unpack_from('<I', mbr, 454)[0], 2048)
                f.seek(1048576)
                pbr = f.read(512)
                self.assertEqual(pbr[510:], b'\x55\xaa')
                self.assertEqual(pbr[54:62], b'FAT16   ')
                self.assertIn(b'KERNEL  SYS', pbr)
                self.assertEqual(struct.unpack_from('<I', pbr, 28)[0], 2048)
            listing = subprocess.check_output(['mdir', '-i', str(image)+'@@1048576', '::'], text=True)
            self.assertIn('KERNEL', listing)
            self.assertIn('COMMAND', listing)

    def test_never_clobbers_existing_destination(self):
        from lifus.freedos import create_image
        with tempfile.TemporaryDirectory() as td:
            image = pathlib.Path(td) / 'existing'
            image.write_bytes(b'valuable')
            with self.assertRaises((ValueError, FileExistsError)):
                create_image(str(image))
            self.assertEqual(image.read_bytes(), b'valuable')

    def test_invalid_size_and_label(self):
        from lifus.freedos import create_image
        with tempfile.TemporaryDirectory() as td:
            for kwargs in [{'size_mib': 1}, {'size_mib': 4096}, {'label': 'BAD/LABEL'}]:
                with self.assertRaises(ValueError):
                    create_image(str(pathlib.Path(td)/'out.img'), **kwargs)

if __name__ == '__main__':
    unittest.main()
