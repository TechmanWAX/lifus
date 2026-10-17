"""Identity races use synthetic plans and private regular files, never disks."""
import copy
from contextlib import ExitStack
import os
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lifus import unmount, writer, operations


A = dict(dev=5, inode=10, rdev=2048, model='USB', serial='A', size=4096,
         topology=[dict(path='/dev/fake', device='8:0', size=4096,
                        dev=5, inode=10, rdev=2048)])


def plan(identity):
    return dict(target='/dev/fake', identity=identity, mounts=[], mountpoints=[])


class UnmountIdentityTests(unittest.TestCase):
    def test_backends_bind_original_confirmation(self):
        for backend in ('raw', 'advanced'):
            for race in ('after_unmount', 'before_open', 'after_open',
                         'final_validate', 'fd_substitution', None):
                with self.subTest(backend=backend, race=race):
                    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                        self._backend_race(Path(directory), stack, backend, race)

    def _backend_race(self, directory, stack, backend, race):
        target = directory / 'target'
        target.write_bytes(b'x' * 4096)
        source = directory / 'source'
        source.write_bytes(b'image')
        substitute = directory / 'substitute'
        substitute.write_bytes(b'y' * 4096)
        original_stat, original_fstat, original_open = os.stat, os.fstat, os.open
        target_stat = original_stat(target)
        expected = dict(A, dev=target_stat.st_dev, inode=target_stat.st_ino, rdev=0)
        state = copy.deepcopy(expected)
        opens, writes, commands, validations = [], [], [], []

        def replace(obj, name, value):
            stack.enter_context(patch.object(obj, name, value))

        def as_block(st):
            values = {key: getattr(st, key) for key in dir(st) if key.startswith('st_')}
            values['st_mode'] = stat.S_IFBLK | 0o600
            return SimpleNamespace(**values)

        def fake_stat(path, *a, **kw):
            st = original_stat(path, *a, **kw)
            return as_block(st) if os.fspath(path) == str(target) else st

        def fake_fstat(fd):
            st = original_fstat(fd)
            return as_block(st) if st.st_ino == target_stat.st_ino else st

        info = dict(path=str(target), size=4096, model='USB', serial='A',
                    stat_identity=[target_stat.st_dev, target_stat.st_ino, 0])
        def validate(*a, **kw):
            validations.append(1)
            if ((race == 'before_open' and len(validations) == 2) or
                    (race == 'final_validate' and len(validations) == 3)):
                state['serial'] = 'B'
            return dict(info)

        def target_open(path, flags, *a, **kw):
            if os.fspath(path) == str(target):
                opens.append(path)
                if race == 'after_open':
                    state['serial'] = 'B'
                if race == 'fd_substitution':
                    path = str(substitute)
            return original_open(path, flags, *a, **kw)

        replace(os, 'stat', fake_stat)
        checked_stat = writer.devices._checked_stat
        def checked(path):
            name, st = checked_stat(path)
            return name, as_block(st) if name == str(target) else st
        replace(writer.devices, '_checked_stat', checked)
        replace(os, 'fstat', fake_fstat)
        replace(os, 'open', target_open)
        replace(os, 'geteuid', lambda: 0)
        replace(unmount, 'plan_unmount', lambda *a, **kw: plan(copy.deepcopy(state)))
        replace(writer.devices, 'validate_target', validate)
        replace(writer, '_write_all', lambda *a: writes.append(a))
        replace(writer.fcntl, 'ioctl', lambda *a: struct.pack('Q', 4096))
        replace(operations, '_validate', validate)
        replace(operations, '_source_safe', lambda *a: None)
        replace(operations, '_format_image', lambda path, *a, **kw: Path(path).write_bytes(b''))
        replace(operations, '_run', lambda args, **kw: commands.append(args))
        replace(operations, '_pwrite_all', lambda *a: writes.append(a))
        preview = dict(action='format', source=None, target=str(target), identity=info,
                       options=dict(filesystem='fat32', label='TEST', partition_table='gpt'),
                       missing_tools=[], staging_bytes=0, size=0, offset=0, efi_offset=None)
        def regenerate(*a, **kw):
            validate()
            return copy.deepcopy(preview)
        replace(operations, 'plan_operation', regenerate)
        if race == 'after_unmount':
            state['serial'] = 'B'
        def execute():
            if backend == 'raw':
                return writer.write_image(source, target, verify=False, expected_identity=expected)
            return operations.execute_operation(preview, expected_identity=expected)
        if race:
            with self.assertRaisesRegex(ValueError, 'identity|changed'):
                execute()
            self.assertFalse(writes)
            self.assertFalse(commands)
            if race in ('after_unmount', 'before_open'):
                self.assertFalse(opens)
        else:
            execute()
            self.assertTrue(writes if backend == 'raw' else commands)

    def test_assert_identity_checks_every_field(self):
        for field in ('dev', 'inode', 'rdev', 'model', 'serial', 'size', 'topology'):
            with self.subTest(field=field):
                other = copy.deepcopy(A)
                other[field] = 'replacement'
                with patch.object(unmount, 'plan_unmount', return_value=plan(other)):
                    with self.assertRaisesRegex(ValueError, 'identity'):
                        unmount.assert_target_identity('/dev/fake', A)

    def test_assert_identity_checks_open_handle(self):
        for field in ('dev', 'inode', 'rdev', 'size', 'mode'):
            with self.subTest(field=field):
                values = dict(st_dev=5, st_ino=10, st_rdev=2048, st_mode=stat.S_IFBLK)
                key = {'dev': 'st_dev', 'inode': 'st_ino', 'rdev': 'st_rdev', 'mode': 'st_mode'}.get(field)
                if key:
                    values[key] += 1 if field != 'mode' else -stat.S_IFBLK
                with patch.object(unmount, 'plan_unmount', return_value=plan(A)), \
                     patch.object(unmount.os, 'fstat', return_value=SimpleNamespace(**values)), \
                     patch('fcntl.ioctl', return_value=struct.pack('Q', 4096 + (field == 'size'))):
                    with self.assertRaisesRegex(ValueError, 'identity|capacity'):
                        unmount.assert_target_identity('/dev/fake', A, fd=999)

    def test_assert_identity_returns_current_plan(self):
        with patch.object(unmount, 'plan_unmount', return_value=plan(A)):
            self.assertEqual(unmount.assert_target_identity('/dev/fake', A), plan(A))

    def test_unmount_replacement_during_final_validation(self):
        state = copy.deepcopy(A)
        def validate(target):
            state['serial'] = 'B'
            return {'path': target}
        with patch.object(unmount, 'plan_unmount', side_effect=lambda *a, **k: plan(copy.deepcopy(state))), \
             patch.object(unmount.os, 'geteuid', return_value=0), \
             patch.object(unmount.devices, 'validate_target', side_effect=validate):
            with self.assertRaisesRegex(ValueError, 'identity'):
                unmount.unmount_target('/dev/fake', expected_identity=A)

    def test_expected_identity_never_overrides_strict_validation(self):
        with patch.object(unmount, 'assert_target_identity', return_value=plan(A)), \
             patch.object(writer.devices, 'validate_target', side_effect=ValueError('unsafe')), \
             patch.object(os, 'open') as opened, \
             patch.object(os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(ValueError, 'unsafe'):
                writer.write_image('/source', '/dev/fake', expected_identity=A)
            with self.assertRaisesRegex(ValueError, 'unsafe'):
                operations.execute_operation(dict(action='format', source=None, target='/dev/fake',
                                                  options={}), expected_identity=A)
            opened.assert_not_called()
