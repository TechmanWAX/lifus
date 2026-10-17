"""Progress boundaries exercised on private regular files, never devices."""
import contextlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lifus import operations


class OperationsProgressTests(unittest.TestCase):
    @contextlib.contextmanager
    def operation(self, efi=False):
        with tempfile.TemporaryDirectory() as directory:
            size = 4 * operations.MiB + 17
            target = Path(directory, 'target.img')
            with target.open('wb') as stream:
                stream.truncate(size + 2 * operations.MiB)
            st = target.stat()
            identity = dict(path=str(target), size=st.st_size,
                            stat_identity=[st.st_dev, st.st_ino, st.st_rdev])
            plan = dict(action='windows' if efi else 'format', source=None,
                        target=str(target), identity=identity,
                        options=dict(allow_file=True, staging_dir=directory, filesystem='ntfs',
                                     label='TEST', partition_table='gpt'),
                        missing_tools=[], staging_bytes=size, offset=4096, size=size,
                        efi_offset=size + 4096 if efi else None)
            events = []
            if efi:
                source = Path(directory, 'source.iso')
                source.write_bytes(b'fixture')
                plan.update(source=str(source), source_identity=operations._source_identity(source), archive_type='Iso')
            def stage(path, *args, **kwargs):
                events.append(('stage',))
                Path(path).write_bytes(b'x' * size)
            def planning(*args, **kwargs):
                events.append(('plan',))
                return plan
            real_write, real_read, real_sync = os.pwrite, os.pread, os.fsync
            def write(fd, data, offset):
                events.append(('write', offset))
                return real_write(fd, data, offset)
            def read(fd, count, offset):
                events.append(('read', offset))
                return real_read(fd, count, offset)
            def sync(fd):
                events.append(('flush',))
                return real_sync(fd)
            with contextlib.ExitStack() as stack:
                for obj, name, kwargs in [
                    (operations.os, 'geteuid', dict(return_value=0)),
                    (operations, 'plan_operation', dict(side_effect=planning)),
                    (operations, '_validate', dict(return_value=identity)),
                    (operations, '_format_image', dict(side_effect=stage)),
                    (operations, '_members', dict(return_value=[])),
                    (operations, '_extract', {}),
                    (operations, '_run', {}),
                    (operations.os, 'pwrite', dict(side_effect=write)),
                    (operations.os, 'pread', dict(side_effect=read)),
                    (operations.os, 'fsync', dict(side_effect=sync)),
                ]:
                    stack.enter_context(patch.object(obj, name, **kwargs))
                yield plan, target, events

    def test_filesystem_progress_surrounds_slow_work_and_readback(self):
        with self.operation() as (plan, target, events):
            result = operations.execute_operation(plan, progress=events.append)
            self.assertTrue(result['verified'])
            self.assertEqual(events[0], dict(phase='prepare', completed=0, total=plan['size']))
            self.assertLess(events.index(events[0]), events.index(('plan',)))
            progress = [e for e in events if isinstance(e, dict)]
            self.assertEqual([e['phase'] for e in progress],
                             ['prepare', 'write', 'write', 'write', 'sync',
                              'verify', 'verify', 'verify', 'sync'])
            self.assertEqual([e['completed'] for e in progress if e['phase'] == 'verify'],
                             [0, 4 * operations.MiB, plan['size']])
            self.assertTrue(all(e['total'] == plan['size'] for e in progress))
            for index, event in enumerate(events):
                if event == ('write', plan['offset']):
                    self.assertEqual(events[index - 1]['phase'], 'write')
                    self.assertEqual(events[index - 1]['completed'], 0)
                elif event == ('read', plan['offset']):
                    self.assertEqual(events[index - 1]['phase'], 'verify')
                elif event == ('flush',):
                    self.assertEqual(events[index - 1]['phase'], 'sync')
            self.assertEqual(events[-1], ('flush',))

    def test_efi_work_announces_finalizing_and_sync(self):
        with self.operation(efi=True) as (plan, target, events):
            asset = target.with_name('efi.img')
            asset.write_bytes(b'efi-test')
            with patch.object(operations, '_uefi_ntfs_asset', return_value=asset):
                result = operations.execute_operation(plan, progress=events.append)
            self.assertTrue(result['verified'])
            index = events.index(('write', plan['efi_offset']))
            self.assertEqual(events[index - 1]['phase'], 'finalizing')
            self.assertEqual(events[index + 1]['phase'], 'sync')
            self.assertEqual(events[index + 2], ('flush',))
            self.assertEqual(events[-2]['phase'], 'sync')
            self.assertEqual(events[-1], ('flush',))

    def test_badblocks_announces_each_pass_phase_before_io(self):
        size = 4 * operations.MiB + 7
        events = []
        real_write, real_read, real_sync = os.pwrite, os.pread, os.fsync
        def write(fd, data, offset):
            events.append(('write', offset))
            return real_write(fd, data, offset)
        def read(fd, count, offset):
            events.append(('read', offset))
            return real_read(fd, count, offset)
        def sync(fd):
            events.append(('flush',))
            return real_sync(fd)
        with tempfile.TemporaryFile() as stream, \
                patch.object(operations.os, 'pwrite', side_effect=write), \
                patch.object(operations.os, 'pread', side_effect=read), \
                patch.object(operations.os, 'fsync', side_effect=sync):
            stream.truncate(size)
            operations._badblocks(stream.fileno(), size, 2, events.append, None)
        progress = [e for e in events if isinstance(e, dict)]
        for pass_number in (1, 2):
            updates = [e for e in progress if e['pass_number'] == pass_number]
            self.assertEqual([e['phase'] for e in updates],
                             ['write', 'write', 'write', 'sync',
                              'verify', 'verify', 'verify', 'sync'])
            for phase in ('write', 'verify'):
                self.assertEqual([e['completed'] for e in updates if e['phase'] == phase],
                                 [0, 4 * operations.MiB, size])
            self.assertTrue(all(e['total'] == size for e in updates))
        for index, event in enumerate(events):
            if event in (('write', 0), ('read', 0), ('flush',)):
                expected = {'write': 'write', 'read': 'verify', 'flush': 'sync'}[event[0]]
                self.assertEqual(events[index - 1]['phase'], expected)


if __name__ == '__main__':
    unittest.main()
