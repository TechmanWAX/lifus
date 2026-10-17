"""Race/cancellation regressions using private files and harmless children."""
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from lifus import operations as op


class CancellationTests(unittest.TestCase):
    @contextlib.contextmanager
    def direct_fixture(self):
        import hashlib
        import json
        with tempfile.TemporaryDirectory() as d:
            source = Path(d, 'source.iso'); source.write_bytes(b'iso')
            target = Path(d, 'target'); target.write_bytes(b'untouched')
            answer = Path(d, 'answer'); answer.write_bytes(b'approved')
            identity = op._validate(str(target), True)
            records = [dict(path='file', size=1, directory=False)]
            plan = dict(action='windows', source=str(source), source_identity=op._source_identity(source),
                        target=str(target), identity=identity, size=94*op.MiB, offset=op.MiB,
                        archive_type='Iso', member_fingerprint=hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
                        options=dict(allow_file=True, filesystem='fat32', label='TEST', partition_table='gpt', unattend=str(answer)),
                        unattend_identity=op._source_identity(answer), missing_tools=[], staging_bytes=0, efi_offset=None)
            with patch('os.geteuid', return_value=0), patch.object(op, 'plan_operation', return_value=plan), \
                    patch.object(op, '_members', return_value=records):
                yield plan, target, answer

    def test_stalled_partition_commands_are_cancelled_and_reaped(self):
        import subprocess
        popen = subprocess.Popen
        for stop in (1, 2, 3):
            with self.subTest(stop=stop), self.direct_fixture() as (plan, target, answer):
                pid = answer.with_name('pid')
                calls = []
                started = None
                def child(args, **kwargs):
                    nonlocal started
                    calls.append(args)
                    if len(calls) == stop:
                        started = time.monotonic()
                        body = f'open({str(pid)!r}, "w").write(str(os.getpid())); time.sleep(2)'
                    else:
                        body = 'pass'
                    return popen([sys.executable, '-c', 'import os,time; '+body], **kwargs)
                with patch('subprocess.Popen', side_effect=child), self.assertRaises(op.OperationCancelled):
                    op.execute_operation(plan, cancel=lambda: started is not None and time.monotonic()-started > .3)
                self.assertLess(time.monotonic()-started, 1.5)
                self.assertEqual(len(calls), stop)
                with self.assertRaises(ProcessLookupError): os.kill(int(pid.read_text()), 0)
                self.assertEqual(target.read_bytes(), b'untouched')

    def test_unattend_race_fails_before_any_partition_command(self):
        for kind in ('file', 'fifo'):
            with self.subTest(kind=kind), self.direct_fixture() as (plan, target, answer):
                prepare = op._prepare_direct
                def race(*args):
                    answer.unlink()
                    if kind == 'fifo': os.mkfifo(answer)
                    else: answer.write_bytes(b'unapproved')
                    return prepare(*args)
                with patch.object(op, '_prepare_direct', side_effect=race), patch.object(op, '_run') as run:
                    with self.assertRaises(ValueError): op.execute_operation(plan)
                run.assert_not_called()
                self.assertEqual(target.read_bytes(), b'untouched')

    def test_unattend_snapshot_survives_replacement_after_destruction(self):
        from lifus.direct_fat import Layout
        for kind in ('fifo', 'file'):
            with self.subTest(kind=kind), self.direct_fixture() as (plan, target, answer):
                def write(layout, fd, streams, *args):
                    answer.rename(answer.with_name('approved'))
                    if kind == 'fifo': os.mkfifo(answer)
                    else: answer.write_bytes(b'unapproved')
                    self.assertEqual(b''.join(streams(layout.nodes['autounattend.xml'])), b'approved')
                    return 8
                with patch.object(Layout, 'write', write), patch.object(op, '_run'):
                    with self.assertRaisesRegex(ValueError, 'incomplete'): op.execute_operation(plan)

    def test_execution_preflight_listing_is_cancellable_and_reaped(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d, 'source.iso'); source.write_bytes(b'iso')
            target = Path(d, 'disk'); target.write_bytes(b'safe')
            pid = Path(d, 'pid')
            import subprocess
            popen = subprocess.Popen
            def child(args, **kwargs):
                return popen([sys.executable, '-c',
                    f'import os,time; open({str(pid)!r}, "w").write(str(os.getpid())); time.sleep(2)'], **kwargs)
            started = time.monotonic()
            plan = dict(action='windows', source=str(source), target=str(target), size=96*op.MiB,
                        options={'allow_file': True})
            with patch('os.geteuid', return_value=0), patch.object(op, '_validate', return_value={
                    'path': str(target), 'size': 96*op.MiB}), patch('subprocess.Popen', side_effect=child):
                with self.assertRaises(op.OperationCancelled):
                    op.execute_operation(plan, cancel=lambda: time.monotonic()-started > .3)
            self.assertLess(time.monotonic()-started, 1.5)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid.read_text()), 0)
            self.assertEqual(target.read_bytes(), b'safe')


class PinnedInputTests(unittest.TestCase):
    def test_unattend_replaced_before_open_is_rejected_without_blocking(self):
        for kind in ('fifo', 'file', 'symlink', 'ancestor'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                parent = Path(d, 'parent'); parent.mkdir()
                path = parent/'answer.xml'; path.write_bytes(b'approved')
                identity = op._source_identity(path)
                if kind == 'ancestor':
                    parent.rename(Path(d, 'old')); parent.mkdir()
                    path.write_bytes(b'unapproved')
                else:
                    path.unlink()
                    if kind == 'fifo': os.mkfifo(path)
                    elif kind == 'symlink': path.symlink_to('/dev/null')
                    else: path.write_bytes(b'unapproved')
                # Guard old implementations from blocking on a FIFO.
                original = os.open
                def checked(name, flags, *args, **kwargs):
                    if str(name) == str(path):
                        self.assertTrue(flags & os.O_NONBLOCK)
                        self.assertTrue(flags & os.O_NOFOLLOW)
                    return original(name, flags, *args, **kwargs)
                with patch('os.open', side_effect=checked), self.assertRaises((ValueError, OSError)):
                    op._read_approved(path, identity)

    def test_replacement_after_open_never_reads_replacement(self):
        for kind in ('file', 'fifo', 'ancestor'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                parent = Path(d, 'parent'); parent.mkdir()
                path = parent/'answer'; path.write_bytes(b'approved')
                identity = op._source_identity(path)
                original = os.open
                def swapped(name, flags, *args, **kwargs):
                    fd = original(name, flags, *args, **kwargs)
                    if str(name) == str(path):
                        if kind == 'ancestor':
                            parent.rename(Path(d, 'old')); parent.mkdir()
                            path.write_bytes(b'unapproved')
                        else:
                            path.unlink()
                            if kind == 'fifo': os.mkfifo(path)
                            else: path.write_bytes(b'unapproved')
                    return fd
                with patch('os.open', side_effect=swapped):
                    try:
                        data = op._read_approved(path, identity)
                    except ValueError:
                        pass  # unlink changes ctime: fail closed
                    else:
                        self.assertEqual(data, b'approved')

    def test_postread_ctime_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 'answer'); path.write_bytes(b'approved')
            identity = op._source_identity(path)
            original = os.read
            def mutate(fd, size):
                data = original(fd, size)
                path.write_bytes(b'evil!!!!')
                os.utime(path, ns=(path.stat().st_atime_ns, identity[3]))
                return data
            with patch('os.read', side_effect=mutate), self.assertRaises(ValueError):
                op._read_approved(path, identity)
