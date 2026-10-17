"""Progress contracts exercised only against temporary regular files."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lifus import writer


class WriterProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / 'source'
        self.target = Path(self.directory.name) / 'target'
        self.source.write_bytes(b'abcdefgh')
        self.target.write_bytes(b'T' * 16)
        self.events = []

    def progress(self, done, total):
        # Both arguments are mandatory, including for structured phase events.
        if isinstance(done, dict):
            self.assertIsNone(total)
            self.assertEqual(set(done), {'phase', 'completed', 'total'})
            self.events.append(('phase', done['phase'], done['completed'], done['total']))
        else:
            self.events.append(('bytes', done, total))

    def test_only_block_targets_sync_each_16_mib_and_resume_write(self):
        mib = 1024 * 1024
        size = 33 * mib
        self.source.write_bytes(b'A' * size)
        self.target.write_bytes(b'T' * size)
        real_sync = os.fsync
        for block_device in (False, True):
            with self.subTest(block_device=block_device):
                self.events.clear()
                sync_positions = []

                def syncing(fd):
                    self.assertEqual(self.events[-1][0:2], ('phase', 'flush' if os.lseek(fd, 0, os.SEEK_CUR) < size else 'sync'))
                    sync_positions.append(os.lseek(fd, 0, os.SEEK_CUR))
                    return real_sync(fd)

                # Exercise the block-only policy on a regular temporary file;
                # keep all actual identity, capacity and validation checks intact.
                stat_proxy = mock.Mock(wraps=writer.stat)
                stat_proxy.S_ISBLK.return_value = block_device
                with mock.patch.object(writer, 'stat', stat_proxy), \
                        mock.patch.object(writer, 'CHUNK_SIZE', 4 * mib), \
                        mock.patch.object(os, 'fsync', syncing):
                    result = writer.write_image(self.source, self.target, allow_file=True,
                                                verify=False, progress=self.progress)
                self.assertFalse(result['verified'])
                self.assertEqual(sync_positions, [16 * mib, 32 * mib, size]
                                 if block_device else [size])
                phases = [e for e in self.events if e[0] == 'phase']
                expected = [('phase', 'prepare', 0, 0), ('phase', 'write', 0, size)]
                if block_device:
                    for completed in (16 * mib, 32 * mib):
                        expected.extend([('phase', 'flush', completed, size),
                                         ('phase', 'write', completed, size)])
                expected.append(('phase', 'sync', size, size))
                self.assertEqual(phases, expected)
                self.assertEqual(self.target.read_bytes(), self.source.read_bytes())

    def test_backup_phases_precede_source_open_write_and_both_syncs(self):
        source, write, sync = writer._source, writer._write_all, os.fsync
        syncs = []

        def opening(*args, **kwargs):
            self.assertEqual(self.events, [('phase', 'prepare', 0, 0)])
            return source(*args, **kwargs)

        def writing(fd, block):
            self.assertEqual([e[1] for e in self.events if e[0] == 'phase'][-1], 'write')
            return write(fd, block)

        def syncing(fd):
            self.assertEqual(self.events[-1], ('phase', 'sync', 8, 8))
            syncs.append(fd)
            return sync(fd)

        destination = self.target.with_name('backup')
        with mock.patch.object(writer, '_source', opening), \
                mock.patch.object(writer, '_write_all', writing), \
                mock.patch.object(os, 'fsync', syncing):
            result = writer.backup_image(self.source, destination, progress=self.progress)
        self.assertEqual(result['bytes_written'], 8)
        self.assertEqual(destination.read_bytes(), self.source.read_bytes())
        self.assertEqual(len(syncs), 2)
        self.assertEqual(self.events, [
            ('phase', 'prepare', 0, 0), ('phase', 'write', 0, 8),
            ('bytes', 0, 8), ('bytes', 8, 8), ('phase', 'sync', 8, 8)])

    def test_write_phases_precede_work_and_track_readback(self):
        validate, write, sync, read = (writer.devices.validate_target,
                                      writer._write_all, os.fsync, os.read)

        def validating(*args, **kwargs):
            self.assertEqual(self.events[:1], [('phase', 'prepare', 0, 0)])
            return validate(*args, **kwargs)

        def writing(fd, block):
            self.assertEqual([e[1] for e in self.events if e[0] == 'phase'][-1], 'write')
            return write(fd, block)

        def syncing(fd):
            self.assertEqual(self.events[-1], ('phase', 'sync', 8, 8))
            return sync(fd)

        def reading(fd, length):
            self.assertEqual(self.events[-1][0:2], ('phase', 'verify'))
            return read(fd, length)

        with mock.patch.object(writer, 'CHUNK_SIZE', 4), \
                mock.patch.object(writer.devices, 'validate_target', validating), \
                mock.patch.object(writer, '_write_all', writing), \
                mock.patch.object(os, 'fsync', syncing), \
                mock.patch.object(os, 'read', reading):
            result = writer.write_image(self.source, self.target, allow_file=True,
                                        progress=self.progress)
        self.assertTrue(result['verified'])
        self.assertEqual(self.target.read_bytes(), b'abcdefgh' + b'T' * 8)
        self.assertEqual(self.events, [
            ('phase', 'prepare', 0, 0), ('phase', 'write', 0, 8),
            ('bytes', 0, 8), ('bytes', 4, 8), ('bytes', 8, 8),
            ('phase', 'sync', 8, 8), ('phase', 'verify', 0, 8),
            ('phase', 'verify', 4, 8), ('phase', 'verify', 8, 8)])
