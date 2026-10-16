import unittest
from unittest.mock import patch
from lifus import operations


class OperationsTests(unittest.TestCase):
    def test_format_preview_has_no_writes(self):
        target = dict(path='/dev/test', size=256*1024*1024, model='test', serial='x',
                      removable=True, transport='usb', mountpoints=[], protected=False)
        with patch.object(operations, '_validate', return_value=target):
            plan = operations.plan_operation('format', None, '/dev/test', filesystem='fat32')
        self.assertTrue(plan['destructive'])
        self.assertEqual(plan['options']['partition_table'], 'gpt')
        self.assertIn('ALL', plan['warning'])

    def test_cli_option_aliases(self):
        target = dict(path='/dev/test', size=256*1024*1024, model='test', serial='x',
                      removable=True, transport='usb', mountpoints=[], protected=False)
        with patch.object(operations, '_validate', return_value=target):
            plan = operations.plan_operation('format', None, '/dev/test', partition_scheme='mbr', persistence_mib=128)
        self.assertEqual(plan['options']['partition_table'], 'mbr')
        self.assertEqual(plan['options']['persistence_size'], 128*1024*1024)

    def test_rejects_unsafe_archive_paths(self):
        for name in ('../etc/passwd', '/etc/passwd', 'a/../../b', 'a\\b', 'C:/x'):
            with self.assertRaises(ValueError):
                operations._safe_member(name)
        self.assertEqual(operations._safe_member('sources/install.wim'), 'sources/install.wim')

    def test_non_root_execution_rejected(self):
        with patch('os.geteuid', return_value=1000):
            with self.assertRaises(PermissionError):
                operations.execute_operation({})

    def test_changed_inode_is_refused(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            target = Path(d, 'target.img')
            replacement = Path(d, 'replacement.img')
            for p in (target, replacement):
                with open(p, 'wb') as f:
                    f.truncate(96*1024*1024)
            plan = operations.plan_operation('format', None, str(target), allow_file=True)
            replacement.replace(target)
            with patch('os.geteuid', return_value=0), self.assertRaisesRegex(ValueError, 'identity changed'):
                operations.execute_operation(plan)

    def test_cancel_before_any_writes(self):
        with patch('os.geteuid', return_value=0), self.assertRaises(operations.OperationCancelled):
            operations.execute_operation({}, cancel=lambda: True)

    def test_source_target_alias_rejected(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            with self.assertRaisesRegex(ValueError, 'Source is the target'):
                operations._source_safe(f.name, f.name)

    def test_badblocks_real_file(self):
        import tempfile
        import os
        with tempfile.TemporaryFile() as f:
            f.truncate(1024*1024+7)
            operations._badblocks(f.fileno(), 1024*1024+7, 1, None, None)
            self.assertNotEqual(os.pread(f.fileno(), 32, 0), os.pread(f.fileno(), 32, 4096))

    def test_all_filesystems_real_images(self):
        import tempfile
        import os
        import shutil
        for filesystem in ('fat32', 'ntfs', 'exfat', 'ext2', 'ext3', 'ext4'):
            with self.subTest(filesystem=filesystem), tempfile.TemporaryDirectory() as directory:
                tool = 'mkfs.' + ('vfat' if filesystem == 'fat32' else filesystem)
                if not shutil.which(tool):
                    continue
                path = os.path.join(directory, 'fs.img')
                operations._format_image(path, 96*1024*1024, filesystem, 'TEST')
                self.assertEqual(os.path.getsize(path), 96*1024*1024)
                with open(path, 'rb') as f:
                    self.assertNotEqual(f.read(4096), bytes(4096))

    def test_windows_userspace_integration(self):
        import tempfile
        import os
        import shutil
        from pathlib import Path
        if not all(shutil.which(t) for t in ('xorriso', '7z', 'mcopy', 'wimlib-imagex')):
            self.skipTest('Windows ISO tools unavailable')
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d, 'tree')
            (tree/'efi/boot').mkdir(parents=True)
            (tree/'sources').mkdir()
            (tree/'efi/boot/bootx64.efi').write_bytes(b'test-loader-not-real-firmware')
            (tree/'sources/boot.wim').write_bytes(b'test-wim-not-real-windows')
            iso = str(Path(d, 'fixture.iso'))
            operations._run(['xorriso', '-as', 'mkisofs', '-J', '-R', '-o', iso, str(tree)])
            disk = str(Path(d, 'disk.img'))
            with open(disk, 'wb') as f:
                f.truncate(96*1024*1024)
            plan = operations.plan_operation('windows', iso, disk, allow_file=True)
            with patch('os.geteuid', return_value=0):
                result = operations.execute_operation(plan)
            self.assertTrue(result['verified'])
            data = operations._run(['mtype', '-i', disk+'@@1048576', '::/efi/boot/bootx64.efi'])
            self.assertEqual(data, b'test-loader-not-real-firmware')

    def test_persistence_real_file(self):
        import tempfile
        import shutil
        from pathlib import Path
        if not all(shutil.which(t) for t in ('xorriso', '7z', 'sfdisk', 'mkfs.ext4', 'debugfs')):
            self.skipTest('Persistence tools unavailable')
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d, 'tree')
            (tree/'live').mkdir(parents=True)
            (tree/'.disk').mkdir()
            (tree/'boot').mkdir()
            (tree/'live/filesystem.squashfs').write_bytes(b'fixture-not-real-rootfs')
            (tree/'.disk/info').write_text('Debian Live test fixture')
            (tree/'boot/live.cfg').write_text('append boot=live persistence\n')
            iso = str(Path(d, 'fixture.iso'))
            operations._run(['xorriso', '-as', 'mkisofs', '-J', '-R', '-o', iso, str(tree)])
            sectors = Path(iso).stat().st_size // 512
            operations._run(['sfdisk', '--no-reread', iso], input=f'label: dos\nstart=1,size={sectors-1},type=17\n'.encode())
            disk = str(Path(d, 'disk.img'))
            shutil.copyfile(iso, disk)
            with open(disk, 'r+b') as f:
                f.truncate(128*1024*1024)
            plan = operations.plan_operation('persistence', iso, disk, allow_file=True)
            with patch('os.geteuid', return_value=0):
                result = operations.execute_operation(plan)
            self.assertTrue(result['verified'])
            partition = str(Path(d, 'part.img'))
            with open(disk, 'rb') as f, open(partition, 'wb') as out:
                f.seek(plan['offset'])
                out.write(f.read(plan['size']))
            text = operations._run(['debugfs', '-R', 'cat persistence.conf', partition])
            self.assertEqual(text, b'/ union\n')

    def test_real_file_format(self):
        import tempfile
        import os
        import shutil
        if not shutil.which('parted') or not shutil.which('mkfs.vfat'):
            self.skipTest('format tools required for integration')
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'disk.img')
            with open(path, 'wb') as stream:
                stream.truncate(96 * 1024 * 1024)
            plan = operations.plan_operation('format', None, path, allow_file=True)
            # Only the privilege predicate is mocked: real tools operate on our
            # private regular file, never a block device or mount.
            with patch('os.geteuid', return_value=0):
                result = operations.execute_operation(plan)
            self.assertEqual(result['status'], 'completed')
            with open(path, 'rb') as stream:
                stream.seek(512)
                self.assertEqual(stream.read(8), b'EFI PART')
                stream.seek(1024*1024+82)
                self.assertEqual(stream.read(8), b'FAT32   ')


if __name__ == '__main__':
    unittest.main()
