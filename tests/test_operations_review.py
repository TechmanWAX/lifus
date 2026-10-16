"""Destructive-boundary regressions; targets are disposable regular files only."""
import contextlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from lifus import operations


class OperationsReviewTests(unittest.TestCase):
    @contextlib.contextmanager
    def operation(self, action='format', efi=False):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, 'target.img')
            target.write_bytes(b'original' * 2048)
            st = target.stat()
            identity = dict(path=str(target), size=st.st_size,
                            stat_identity=[st.st_dev, st.st_ino, st.st_rdev])
            plan = dict(action=action, source=None, target=str(target), identity=identity,
                        options=dict(allow_file=True, staging_dir=directory, filesystem='ntfs',
                                     label='TEST', partition_table='gpt'),
                        missing_tools=[], staging_bytes=32, offset=4096, size=32,
                        efi_offset=8192 if efi else None)
            def stage(path, *args):
                Path(path).write_bytes(b'x' * 32)
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(operations.os, 'geteuid', return_value=0))
                stack.enter_context(patch.object(operations, 'plan_operation', return_value=plan))
                stack.enter_context(patch.object(operations, '_validate', return_value=identity))
                stack.enter_context(patch.object(operations, '_format_image', side_effect=stage))
                stack.enter_context(patch.object(operations, '_verify_prefix'))
                stack.enter_context(patch.object(operations, '_members', return_value=[]))
                stack.enter_context(patch.object(operations, '_extract'))
                runner = stack.enter_context(patch.object(operations, '_run'))
                yield plan, target, runner

    def test_cancel_during_final_validation_does_not_destroy_target(self):
        for action in ('format', 'persistence', 'windows'):
            with self.subTest(action=action), self.operation(action) as (plan, target, runner):
                before = target.read_bytes()
                event = threading.Event()
                def validate(*args):
                    event.set()
                    return plan['identity']
                with patch.object(operations, '_validate', side_effect=validate):
                    with self.assertRaises(operations.OperationCancelled):
                        operations.execute_operation(plan, cancel=event)
                destructive = [c for c in runner.call_args_list if c.args[0][0] in ('parted', 'sfdisk')]
                self.assertEqual(destructive, [])
                self.assertEqual(target.read_bytes(), before)

    def test_cancel_between_partition_commands(self):
        for stop in range(1, 4):
            with self.subTest(stop=stop), self.operation('windows', efi=True) as (plan, target, runner):
                event = threading.Event()
                commands = []
                def run(args, **kwargs):
                    if args[0] == 'parted':
                        commands.append(args)
                        if len(commands) == stop:
                            event.set()
                runner.side_effect = run
                with self.assertRaises(operations.OperationCancelled):
                    operations.execute_operation(plan, cancel=event)
                self.assertEqual(len(commands), stop)

    def test_readback_evicts_after_sync_for_both_partitions(self):
        with self.operation('windows', efi=True) as (plan, target, runner):
            asset = target.with_name('efi.img')
            asset.write_bytes(b'efi-test')
            events = []
            real_write, real_read, real_sync = os.pwrite, os.pread, os.fsync
            def write(fd, data, offset):
                events.append(('write', offset))
                return real_write(fd, data, offset)
            def read(fd, count, offset):
                events.append(('read', offset))
                return real_read(fd, count, offset)
            def sync(fd):
                events.append(('sync',))
                real_sync(fd)
            def evict(fd, offset, count, advice):
                self.assertEqual(advice, os.POSIX_FADV_DONTNEED)
                events.append(('evict',))
            with patch.object(operations, '_uefi_ntfs_asset', return_value=asset), \
                    patch.object(operations.os, 'pwrite', side_effect=write), \
                    patch.object(operations.os, 'pread', side_effect=read), \
                    patch.object(operations.os, 'fsync', side_effect=sync), \
                    patch.object(operations.os, 'posix_fadvise', side_effect=evict, create=True):
                operations.execute_operation(plan)
            self.assertEqual(events, [('write', 4096), ('sync',), ('evict',),
                                      ('read', 4096), ('write', 8192), ('sync',),
                                      ('evict',), ('read', 8192), ('sync',)])

    def test_cancel_during_target_open(self):
        with self.operation() as (plan, target, runner):
            before = target.read_bytes()
            event = threading.Event()
            real_open = os.open
            def opening(path, *args, **kwargs):
                fd = real_open(path, *args, **kwargs)
                if path == str(target):
                    event.set()
                return fd
            with patch.object(operations.os, 'open', side_effect=opening):
                with self.assertRaises(operations.OperationCancelled):
                    operations.execute_operation(plan, cancel=event)
            runner.assert_not_called()
            self.assertEqual(target.read_bytes(), before)
