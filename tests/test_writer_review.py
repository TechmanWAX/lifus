"""Security review regressions; only regular temporary files are used."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lifus import writer


class WriterReviewTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, 'posix_fadvise'), 'POSIX cache advice unavailable')
    def test_readback_drops_cache_after_sync_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            target = Path(directory) / 'target'
            source.write_bytes(b'image')
            target.write_bytes(b'T' * 32)
            events = []
            real_sync, real_advise, real_read = os.fsync, os.posix_fadvise, os.read

            def sync(fd):
                events.append('sync')
                return real_sync(fd)

            def advise(fd, offset, length, advice):
                self.assertEqual((offset, length, advice), (0, 0, os.POSIX_FADV_DONTNEED))
                events.append('drop')
                return real_advise(fd, offset, length, advice)

            def read(fd, length):
                events.append('read')
                return real_read(fd, length)

            with mock.patch.object(os, 'fsync', sync), mock.patch.object(os, 'posix_fadvise', advise), mock.patch.object(os, 'read', read):
                result = writer.write_image(source, target, allow_file=True)
            self.assertTrue(result['verified'])
            self.assertEqual(events, ['sync', 'drop', 'read'])

    def test_backup_rejects_deleted_or_replaced_destination(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'source'
                destination = Path(directory) / 'backup'
                source.write_bytes(b'original')

                def progress(done, total):
                    if done == total:
                        destination.unlink()
                        if replacement:
                            destination.write_bytes(b'replacement')

                with self.assertRaisesRegex(ValueError, 'Destination changed'):
                    writer.backup_image(source, destination, progress=progress)
                if replacement:
                    self.assertEqual(destination.read_bytes(), b'replacement')
                else:
                    self.assertFalse(destination.exists())

    def test_backup_parent_replacement_preserves_unrelated_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            parent = root / 'output'
            moved = root / 'moved'
            parent.mkdir()
            source.write_bytes(b'original')
            destination = parent / 'backup'

            def progress(done, total):
                if done == total:
                    parent.rename(moved)
                    parent.mkdir()
                    destination.write_bytes(b'replacement')

            with self.assertRaisesRegex(ValueError, 'Destination changed'):
                writer.backup_image(source, destination, progress=progress)
            self.assertEqual(destination.read_bytes(), b'replacement')
            self.assertFalse((moved / 'backup').exists())

    def test_source_mutation_during_staging_leaves_target_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            target = Path(directory) / 'target'
            source.write_bytes(b'A' * 32)
            target.write_bytes(b'T' * 64)
            original_read = __import__('io').FileIO.read
            changed = False

            def mutate_after_read(stream, size=-1):
                nonlocal changed
                block = original_read(stream, size)
                if not changed and size == 8 and os.fstat(stream.fileno()).st_ino == source.stat().st_ino:
                    changed = True
                    old = source.stat()
                    source.write_bytes(b'B' * 32)
                    os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000000))
                return block

            # FileIO is immutable: wrap the actual source stream instead.
            real_source = writer._source
            from contextlib import contextmanager

            @contextmanager
            def mutating_source(*args, **kwargs):
                with real_source(*args, **kwargs) as stream:
                    class Reader:
                        def read(self, size=-1):
                            return mutate_after_read(stream, size)
                    yield Reader()

            with mock.patch.object(writer, 'CHUNK_SIZE', 8), mock.patch.object(writer, '_source', mutating_source):
                with self.assertRaisesRegex(ValueError, 'Source changed'):
                    writer.write_image(source, target, allow_file=True)
            self.assertTrue(changed)
            self.assertEqual(target.read_bytes(), b'T' * 64)
