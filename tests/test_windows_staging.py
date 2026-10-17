"""Bounded Windows staging, exercised only on private regular-file images."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from lifus import operations as op


TOOLS = ('xorriso', '7z', 'mkfs.vfat', 'mcopy', 'mmd', 'mtype', 'parted', 'sfdisk')


class SubprocessCancellationTests(unittest.TestCase):
    def test_source_identity_detects_same_size_rewrite_with_restored_mtime(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'original'); f.flush()
            before = op._source_identity(f.name)
            st = os.stat(f.name)
            f.seek(0); f.write(b'replaced'); f.flush()
            os.utime(f.name, ns=(st.st_atime_ns, st.st_mtime_ns))
            self.assertNotEqual(before, op._source_identity(f.name))

    def test_tool_can_be_cancelled_while_running(self):
        import sys
        import time
        start = time.monotonic()
        with self.assertRaises(op.OperationCancelled):
            op._run([sys.executable, '-c', 'import time; time.sleep(20)'],
                    cancel=lambda: time.monotonic() - start > 0.2)
        self.assertLess(time.monotonic() - start, 3)


@unittest.skipUnless(all(shutil.which(t) for t in TOOLS), 'Windows image tools required')
class WindowsStagingTests(unittest.TestCase):
    def test_staging_failure_leaves_target_untouched(self):
        for failure in ('cancel', 'source-change', 'target-symlink'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                tree = root / 'tree'; (tree / 'efi/boot').mkdir(parents=True)
                (tree / 'sources').mkdir()
                (tree / 'efi/boot/bootx64.efi').write_bytes(b'efi')
                (tree / 'sources/boot.wim').write_bytes(b'boot')
                iso = root / 'fixture.iso'
                op._run(['xorriso', '-as', 'mkisofs', '-J', '-R', '-o', str(iso), str(tree)])
                disk = root / 'disk.img'
                with disk.open('wb') as f:
                    f.write(b'original target'); f.truncate(96 * op.MiB)
                plan = op.plan_operation('windows', str(iso), str(disk), allow_file=True)
                populate = op._prepare_direct
                cancelled = False
                def interrupted(*args):
                    nonlocal cancelled
                    result = populate(*args)
                    if failure == 'cancel':
                        cancelled = True
                    elif failure == 'source-change':
                        st = iso.stat()
                        with iso.open('r+b') as f:
                            f.write(b'changed!')
                        os.utime(iso, ns=(st.st_atime_ns, st.st_mtime_ns))
                    else:
                        disk.rename(root / 'original.img')
                        disk.symlink_to(root / 'original.img')
                    return result
                with patch('os.geteuid', return_value=0), patch.object(op, '_prepare_direct', side_effect=interrupted):
                    with self.assertRaises((op.OperationCancelled, ValueError)):
                        op.execute_operation(plan, cancel=lambda: cancelled)
                with disk.open('rb') as f:
                    self.assertEqual(f.read(15), b'original target')

    def test_real_wim_split_roundtrip_at_reduced_test_threshold(self):
        if not shutil.which('wimlib-imagex'):
            self.skipTest('wimlib required')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / 'wim-source'; source.mkdir()
            for i in range(5):
                (source / str(i)).write_bytes(os.urandom(768 * 1024))
            tree = root / 'tree'; (tree / 'sources').mkdir(parents=True)
            (tree / 'efi/boot').mkdir(parents=True)
            (tree / 'efi/boot/bootx64.efi').write_bytes(b'efi')
            (tree / 'sources/boot.wim').write_bytes(b'boot')
            op._run(['wimlib-imagex', 'capture', str(source), str(tree / 'sources/install.wim'), '--compress=none'])
            iso = root / 'fixture.iso'
            op._run(['xorriso', '-as', 'mkisofs', '-J', '-R', '-o', str(iso), str(tree)])
            disk = root / 'disk.img'
            with disk.open('wb') as f:
                f.truncate(96 * op.MiB)
            run = op._run
            def small_split(args, **kwargs):
                if args[:2] == ['wimlib-imagex', 'split']:
                    args = args[:-1] + ['1']
                return run(args, **kwargs)
            with patch.object(op, 'FAT_LIMIT', 2 * op.MiB), patch.object(op, '_run', side_effect=small_split):
                plan = op.plan_operation('windows', str(iso), str(disk), allow_file=True)
                with patch('os.geteuid', return_value=0):
                    op.execute_operation(plan)
            extracted = root / 'split'; extracted.mkdir()
            op._run(['mcopy', '-i', str(disk) + '@@1048576', '::/sources/*.swm', str(extracted)])
            parts = list(extracted.glob('*.swm'))
            self.assertGreater(len(parts), 1)
            self.assertTrue(all(p.stat().st_size < 2 * op.MiB for p in parts))
            applied = root / 'applied'; applied.mkdir()
            op._run(['wimlib-imagex', 'apply', str(extracted / 'install.swm'), '1', str(applied),
                     '--ref=' + str(extracted / '*.swm')])
            for original in source.iterdir():
                self.assertEqual(original.read_bytes(), (applied / original.name).read_bytes())
            listing = op._run(['mdir', '-i', str(disk) + '@@1048576', '::/sources']).lower()
            self.assertNotIn(b'install  wim', listing)

    def test_space_error_reports_path_required_and_free(self):
        with tempfile.TemporaryDirectory() as d:
            disk = Path(d, 'disk.img')
            with disk.open('wb') as f:
                f.truncate(96 * op.MiB)
            plan = op.plan_operation('format', None, str(disk), allow_file=True, staging_dir=d)
            usage = shutil.disk_usage(d)._replace(free=1)
            with patch('os.geteuid', return_value=0), patch.object(op.shutil, 'disk_usage', return_value=usage):
                with self.assertRaises(ValueError) as raised:
                    op.execute_operation(plan)
            self.assertIn(d, str(raised.exception))
            self.assertIn(str(plan['staging_bytes']), str(raised.exception))
            self.assertIn('free 1 bytes', str(raised.exception))
            self.assertEqual(disk.read_bytes()[:512], bytes(512))

    def test_bad_fat_geometry_and_tree_conflicts_refused(self):
        with tempfile.TemporaryDirectory() as d:
            image = str(Path(d, 'fat.img'))
            op._format_image(image, 96 * op.MiB, 'fat32', 'TEST')
            with open(image, 'r+b') as f:
                f.seek(13); f.write(b'\x00')
            with self.assertRaisesRegex(ValueError, 'geometry'):
                list(op._fat32_ranges(image))
            records = [dict(path='a', size=1, directory=False),
                       dict(path='a/b', size=1, directory=False)]
            with self.assertRaisesRegex(ValueError, 'conflict'):
                op._populate_fat32('unused', records, d, image, {}, None, None)

    def test_bounded_staging_and_quick_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d, 'tree')
            (tree / 'efi/boot').mkdir(parents=True)
            (tree / 'sources').mkdir()
            files = {'efi/boot/bootx64.efi': b'fixture-efi',
                     'sources/boot.wim': b'fixture-wim',
                     'sources/zeros.bin': bytes(2 * op.MiB),
                     'long directory name/file name.txt': b'long names'}
            for name, data in files.items():
                p = tree / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            iso = str(Path(d, 'fixture.iso'))
            op._run(['xorriso', '-as', 'mkisofs', '-J', '-R', '-o', iso, str(tree)])
            for scheme in ('gpt', 'mbr'):
                disk = Path(d, scheme + '.img')
                # All old data is nonzero, including future zero-filled file clusters.
                with disk.open('wb') as f:
                    for _ in range(256):
                        f.write(b'X' * op.MiB)
                stage = Path(d, 'stage')
                stage.mkdir(exist_ok=True)
                plan = op.plan_operation('windows', iso, str(disk), allow_file=True,
                                         staging_dir=str(stage), partition_table=scheme)
                self.assertEqual(plan['staging_bytes'], 0)
                self.assertIn('not secure erasure', plan['warning'])
                peak = 0
                events = []
                def observe(event):
                    nonlocal peak
                    events.append(event)
                    peak = max(peak, sum(p.stat().st_blocks * 512 for p in stage.rglob('*') if p.is_file()))
                real_run = op._run
                def measured_run(*args, **kwargs):
                    observe(dict(phase='measurement'))
                    result = real_run(*args, **kwargs)
                    observe(dict(phase='measurement'))
                    return result
                with patch('os.geteuid', return_value=0), patch.object(op, '_run', side_effect=measured_run):
                    result = op.execute_operation(plan, progress=observe)
                self.assertEqual(peak, 0)
                self.assertLess(result['bytes_written'], 16 * op.MiB)
                self.assertTrue(result['verified'])
                for name, data in files.items():
                    self.assertEqual(op._run(['mtype', '-i', str(disk) + '@@1048576', '::/' + name]), data)
                table = json.loads(op._run(['sfdisk', '--json', str(disk)]))['partitiontable']
                self.assertEqual(table['partitions'][0]['start'] * 512, plan['offset'])
                self.assertEqual(table['partitions'][0]['size'] * 512, plan['size'])
                self.assertEqual(disk.stat().st_size, 256 * op.MiB)
                self.assertEqual(list(stage.iterdir()), [])
                self.assertIn('verify', [e['phase'] for e in events])
                print(f'FAT32 {scheme}: logical={plan["size"]} peak_staging={peak} estimate={plan["staging_bytes"]} written={result["bytes_written"]}')


if __name__ == '__main__':
    unittest.main()
