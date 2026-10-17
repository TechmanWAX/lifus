"""Direct FAT32 tests: private regular files only; never physical media."""
import hashlib
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch

from lifus import direct_fat as fat, operations as op


def record(name, size=0, directory=False):
    return dict(path=name, size=size, directory=directory)


class DirectLayoutTests(unittest.TestCase):
    def test_progress_is_bounded_and_periodic_fsync_is_distinct(self):
        size = 65 * op.MiB
        layout = fat.Layout([record('large', size)], 94*op.MiB, 0, 'TEST')
        events = []
        real_sync = os.fsync
        sync_phases = []
        def sync(fd):
            sync_phases.append(events[-1]['phase'])
            real_sync(fd)
        def streams(node):
            for _ in range(size // 8192):
                yield bytes(8192)
        with tempfile.TemporaryFile() as f, patch.object(fat.os, 'fsync', side_effect=sync):
            f.truncate(94*op.MiB)
            start = time.monotonic()
            layout.write(f.fileno(), streams, lambda: None, events.append, op._sync_for_readback)
            elapsed = time.monotonic() - start
        self.assertTrue(sync_phases)
        self.assertEqual(sync_phases, ['flush', 'sync'])
        self.assertEqual([e['completed'] for e in events if e['phase'] == 'sync'], [size])
        self.assertLessEqual(len(events), 20 + elapsed * 12)
        for phase in ('write', 'verify'):
            values = [e['completed'] for e in events if e['phase'] == phase]
            self.assertEqual(values[-1], size)
            self.assertEqual(values, sorted(values))

    def test_generated_alias_cannot_shadow_a_real_long_name(self):
        layout = fat.Layout([record('other'), record('F0000001')], 94*op.MiB, op.MiB, 'TEST')
        aliases = [n['alias'].decode().strip().casefold() for n in layout.ordered if n['path']]
        self.assertNotIn('f0000001', aliases)

    def test_namespace_and_production_limits_before_mutation(self):
        bad = [[record('a'), record('a/b')], [record('A'), record('a')],
               [record('../x')], [record('/x')], [record('a?b')], [record('a.')],
               [record('x'*256)], [record('sources/install.esd', 2**32)],
               [record('a', -1)], [record('A/x'), record('a/y')]]
        for records in bad:
            with self.subTest(records=records), self.assertRaises(ValueError):
                fat.Layout(records, 94*op.MiB, op.MiB, 'TEST')
        layout = fat.Layout([record('install.esd', 2**32-1)], 8*1024*op.MiB, op.MiB, 'TEST')
        self.assertEqual(layout.nodes['install.esd']['size'], 2**32-1)
        with self.assertRaisesRegex(ValueError, 'capacity'):
            fat.Layout([record('large', 95*op.MiB)], 94*op.MiB, op.MiB, 'TEST')
        for offset, label in ((-512, 'TEST'), (0, 'A'*12), (0, 'é')):
            with self.subTest(offset=offset, label=label), self.assertRaises(ValueError):
                fat.Layout([], 94*op.MiB, offset, label)
        with self.assertRaises(ValueError):
            fat.Layout([record('/'.join(['a'*100]*50))], 94*op.MiB, op.MiB, 'TEST')

    def test_actual_write_is_bounded_and_verifies_hashes(self):
        # No formatting tool: direct library plus independent mtools reader below.
        records = [record('zero.bin', 3*op.MiB+1), record('empty'), record('folder', directory=True)]
        layout = fat.Layout(records, 94*op.MiB, op.MiB, 'TEST')
        with tempfile.TemporaryFile() as f:
            f.truncate(96*op.MiB)
            os.pwrite(f.fileno(), b'BEFORE', op.MiB-6)
            os.pwrite(f.fileno(), b'AFTER', 95*op.MiB)
            def streams(node):
                remaining = node['size']
                while remaining:
                    count = min(op.MiB, remaining)
                    yield bytes(count)
                    remaining -= count
            layout.write(f.fileno(), streams, lambda: None, None, op._sync_for_readback)
            self.assertEqual(os.pread(f.fileno(), 6, op.MiB-6), b'BEFORE')
            self.assertEqual(os.pread(f.fileno(), 5, 95*op.MiB), b'AFTER')
            def corrupt(fd):
                op._sync_for_readback(fd)
                os.pwrite(fd, b'X', op.MiB + layout.position(layout.nodes['zero.bin']))
            with self.assertRaisesRegex(IOError, 'hash'):
                layout.write(f.fileno(), streams, lambda: None, None, corrupt)
            def corrupt_metadata(fd):
                os.pwrite(fd, b'X', op.MiB+71)
            with self.assertRaisesRegex(IOError, 'metadata'):
                layout.write(f.fileno(), streams, lambda: None, None, corrupt_metadata)

    @unittest.skipUnless(shutil.which('fsck.fat'), 'read-only FAT checker required')
    def test_independent_readonly_fat_checker_accepts_metadata(self):
        records = [record('folder', directory=True), record('folder/'+'n'*13, 17),
                   record('folder/'+'n'*26, 29), record('empty')]
        layout = fat.Layout(records, 94*op.MiB, 0, 'TEST')
        with tempfile.NamedTemporaryFile() as f:
            f.truncate(94*op.MiB)
            def streams(node):
                if node['size']:
                    yield b'Z' * node['size']
            layout.write(f.fileno(), streams, lambda: None, None, op._sync_for_readback)
            result = op._run(['fsck.fat', '-n', f.name]).decode()
            self.assertNotIn('Leaving filesystem unchanged', result)

    def test_stream_size_failure_never_reports_verified(self):
        layout = fat.Layout([record('file', 10)], 94*op.MiB, op.MiB, 'TEST')
        for data in (b'short', b'too long for ten'):
            with self.subTest(data=data), tempfile.TemporaryFile() as f:
                f.truncate(96*op.MiB)
                with self.assertRaisesRegex(ValueError, 'size'):
                    layout.write(f.fileno(), lambda n: (x for x in [data]), lambda: None, None, os.fsync)

    def test_silent_and_stderr_blocked_process_cancellation_reaps_child(self):
        for body in ('time.sleep(20)', 'sys.stderr.buffer.write(b"x"*10000000); sys.stderr.flush(); time.sleep(20)'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as d:
                pidfile = Path(d, 'pid')
                start = time.monotonic()
                def check():
                    if time.monotonic()-start > 0.4:
                        raise op.OperationCancelled('cancel')
                command = [sys.executable, '-c',
                           f'import os,sys,time; open({str(pidfile)!r}, "w").write(str(os.getpid())); {body}']
                with self.assertRaises(op.OperationCancelled):
                    list(fat.stream_command(command, check))
                self.assertLess(time.monotonic()-start, 3)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pidfile.read_text()), 0)

    def test_large_pipe_stream_uses_bounded_memory(self):
        total = 0
        tracemalloc.start()
        try:
            for chunk in fat.stream_command([sys.executable, '-c',
                    'import sys; b=bytes(1024*1024); [sys.stdout.buffer.write(b) for _ in range(96)]'], lambda: None):
                self.assertLessEqual(len(chunk), op.MiB)
                total += len(chunk)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(total, 96*op.MiB)
        self.assertLess(peak, 8*op.MiB)


@unittest.skipUnless(all(shutil.which(t) for t in ('xorriso', '7z', 'parted', 'mtype')), 'ISO tools required')
class DirectISOTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('genisoimage'), 'genisoimage required')
    def test_udf_parser_is_pinned_with_descriptor_and_all_members_survive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tree = root/'tree'; (tree/'efi/boot').mkdir(parents=True)
            (tree/'sources').mkdir()
            data = {'efi/boot/bootx64.efi': b'efi', 'sources/boot.wim': bytes(65537),
                    'sources/long filename.bin': b'long-name-data'}
            for name, value in data.items():
                (tree/name).write_bytes(value)
            iso = root/'fixture.iso'
            op._run(['genisoimage', '-udf', '-J', '-o', str(iso), str(tree)])
            records, kind = op._members(str(iso), with_type=True)
            self.assertEqual(kind, 'Udf')
            with iso.open('rb') as source:
                handle = f'/proc/{os.getpid()}/fd/{source.fileno()}'
                self.assertEqual(op._members(handle, archive_type=kind), records)
            with self.assertRaisesRegex(ValueError, 'listing'):
                op._members(str(iso), maximum=128)
            disk = root/'disk.img'
            with disk.open('wb') as f:
                f.truncate(96*op.MiB)
            plan = op.plan_operation('windows', str(iso), str(disk), allow_file=True)
            self.assertEqual(plan['archive_type'], 'Udf')
            with patch('os.geteuid', return_value=0):
                result = op.execute_operation(plan)
            self.assertTrue(result['verified'])
            for name, value in data.items():
                self.assertEqual(op._run(['mtype', '-i', str(disk)+'@@1048576', '::/'+name]), value)

    def test_streaming_failures_mark_media_incomplete(self):
        for failure in ('cancel', 'cancel-verify', 'source-read-error', 'io-error',
                        'source-change', 'source-replace', 'target-replace'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                tree = root/'tree'; (tree/'efi/boot').mkdir(parents=True)
                (tree/'sources').mkdir()
                (tree/'efi/boot/bootx64.efi').write_bytes(b'efi')
                (tree/'sources/boot.wim').write_bytes(bytes(op.MiB))
                iso = root/'fixture.iso'
                op._run(['xorriso', '-as', 'mkisofs', '-J', '-R', '-o', str(iso), str(tree)])
                disk = root/'disk.img'
                with disk.open('wb') as f:
                    f.truncate(96*op.MiB)
                plan = op.plan_operation('windows', str(iso), str(disk), allow_file=True)
                fired = False
                def event(e):
                    nonlocal fired
                    phase = 'verify' if failure == 'cancel-verify' else 'write'
                    if fired or e['phase'] != phase or e['completed'] == 0:
                        return
                    fired = True
                    if failure == 'io-error':
                        raise OSError(5, 'Injected I/O error')
                    if failure == 'source-read-error':
                        with iso.open('r+b') as f:
                            f.truncate(32)
                    elif failure == 'source-change':
                        st = iso.stat()
                        with iso.open('r+b') as f:
                            f.seek(-1, 2); f.write(b'X')
                        os.utime(iso, ns=(st.st_atime_ns, st.st_mtime_ns))
                    elif failure == 'source-replace':
                        iso.rename(root/'old.iso')
                        shutil.copyfile(root/'old.iso', iso)
                    elif failure == 'target-replace':
                        disk.rename(root/'old.img')
                        disk.write_bytes(b'untouched replacement')
                with patch('os.geteuid', return_value=0):
                    with self.assertRaisesRegex((ValueError, OSError, op.OperationCancelled), 'incomplete'):
                        op.execute_operation(plan, progress=event, cancel=lambda: fired and failure.startswith('cancel'))
                self.assertTrue(fired)
                if failure == 'target-replace':
                    self.assertEqual(disk.read_bytes(), b'untouched replacement')
