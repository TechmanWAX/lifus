import contextlib
import io
import json
import queue
import threading
import unittest
from unittest.mock import MagicMock, patch

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from lifus import gui, privileges

QT = gui.QApplication.instance() or gui.QApplication([])


class ProgressTests(unittest.TestCase):
    def app(self):
        with patch.object(gui, 'list_devices', return_value=[]):
            app = gui.Application(gui.QWidget())
        self.addCleanup(app.root.close)
        app.busy = True
        return app

    def test_full_byte_count_is_not_completion(self):
        app = self.app()
        app.events.put(('progress', (8, 8)))
        app.poll()
        self.assertEqual(app.progress.value(), 100)
        self.assertNotEqual(app.status.text(), 'Готово')
        app.events.put(('result', {}))
        app.poll()
        self.assertEqual(app.progress.value(), 100)

    def test_worker_phase_updates_are_atomic_across_poll_ticks(self):
        app = self.app()
        values = []
        app.progress.valueChanged.connect(values.append)
        # Poll after EVERY put: models a timer firing between producer messages.
        put = app.events.put
        def poll_each(event):
            put(event)
            app.poll()
            QT.processEvents()
        def run(*args, progress, **kwargs):
            for done in (20, 40, 60, 100):
                progress({'phase': 'write', 'completed': done, 'total': 100})
            self.assertEqual(app.progress.value(), 100)
            self.assertEqual(values, [20, 40, 60, 100])
            progress({'phase': 'verify', 'completed': 25, 'total': 100})
            self.assertEqual(app.progress.value(), 100)
            return {}
        with patch.object(app.events, 'put', side_effect=poll_each), patch.object(gui.os, 'geteuid', return_value=1000), patch.object(privileges, 'run_privileged', side_effect=run):
            app.work('write', '/src', '/dev/test', '', {}, True, True)
        self.assertEqual(app.progress.value(), 100, app.log.toPlainText())
        self.assertNotIn(0, values)

    def test_batched_phase_updates_do_not_reset_progress(self):
        app = self.app()
        values = []
        app.progress.valueChanged.connect(values.append)
        for done in (20, 40, 60, 100):
            app.events.put(('phase', dict(phase='write', completed=done, total=100)))
        app.poll()
        self.assertEqual(values, [20, 40, 60, 100])

    def test_final_sync_verify_and_success_sequence(self):
        app = self.app()
        values = []
        app.progress.valueChanged.connect(values.append)
        for phase, done in [('write', 20), ('flush', 20), ('write', 60),
                            ('flush', 60), ('write', 100), ('sync', 100),
                            ('verify', 0), ('verify', 100), ('sync', 100)]:
            previous = app.status.text()
            app.events.put(('phase', dict(phase=phase, completed=done, total=100)))
            app.poll()
            if phase == 'flush':
                self.assertEqual(app.status.text(), previous)
                self.assertNotIn('Синхронизация', app.status.text())
            if phase == 'sync':
                self.assertEqual(app.progress.value(), 100)
                self.assertIn('Синхронизация', app.status.text())
            self.assertNotEqual(app.status.text(), 'Готово')
        self.assertEqual(values, [20, 60, 100])
        app.events.put(('result', {'verified': True}))
        app.poll()
        self.assertEqual(app.status.text(), 'Готово')

    def test_failure_cancel_and_preview_never_ready(self):
        for outcome in ('error', 'cancel', 'preview'):
            app = self.app()
            app.show_phase(dict(phase='sync', completed=8, total=8))
            if outcome == 'cancel':
                app.cancel_event.set()
            app.previewing = outcome == 'preview'
            app.events.put(('error', 'readback failed') if outcome == 'error' else ('result', {}))
            app.events.put(('done', None))
            app.poll()
            self.assertNotEqual(app.status.text(), 'Готово')
            if outcome == 'preview':
                self.assertIn('запись не выполнялась', app.status.text())

    def test_new_operation_resets_transfer_completion(self):
        app = self.app()
        app.show_phase(dict(phase='sync', completed=8, total=8))
        app.show_phase(dict(phase='prepare'))
        self.assertEqual(app.progress.maximum(), 0)
        app.show_phase(dict(phase='write', completed=20, total=100))
        self.assertEqual(app.progress.value(), 20)

    def test_real_direct_writer_keeps_periodic_flush_silent(self):
        import tempfile
        from lifus import direct_fat, operations
        app = self.app()
        size = 65 * operations.MiB
        layout = direct_fat.Layout([dict(path='large', size=size, directory=False)],
                                  94 * operations.MiB, 0, 'TEST')
        values, statuses = [], []
        app.progress.valueChanged.connect(values.append)
        def progress(event):
            app.events.put(('phase', event))
            app.poll()
            statuses.append(app.status.text())
            self.assertNotEqual(app.status.text(), 'Готово')
            if event['phase'] == 'flush':
                self.assertTrue(app.status.text().startswith('Запись'))
            if event['phase'] in ('sync', 'verify'):
                self.assertEqual(app.progress.value(), 100)
        def streams(node):
            for _ in range(65):
                yield bytes(operations.MiB)
        with tempfile.TemporaryFile() as target:
            target.truncate(94 * operations.MiB)
            layout.write(target.fileno(), streams, lambda: None, progress,
                         operations._sync_for_readback)
        self.assertEqual(values, sorted(values))
        self.assertEqual(sum(s.startswith('Синхронизация') for s in statuses), 1)
        app.events.put(('result', {'verified': True}))
        app.poll()
        self.assertEqual(app.status.text(), 'Готово')

    def test_worker_preserves_sync_phase(self):
        app = self.app()
        def run(*args, progress, **kwargs):
            progress({'phase': 'sync', 'completed': 0, 'total': 0}, None)
            return {}
        with patch.object(gui.os, 'geteuid', return_value=1000), patch.object(privileges, 'run_privileged', side_effect=run):
            app.work('write', '/src', '/dev/test', '', {}, True, True)
        events = list(app.events.queue)
        self.assertIn(('phase', {'phase': 'sync', 'completed': 0, 'total': 0}), events)

    def test_sync_stays_at_100_without_claiming_completion(self):
        app = self.app()
        app.events.put(('phase', {'phase': 'sync', 'completed': 8, 'total': 8}))
        app.poll()
        self.assertEqual(app.progress.maximum(), 100)
        self.assertEqual(app.progress.value(), 100)
        self.assertNotEqual(app.status.text(), 'Готово')
        self.assertIn('Синхронизация', app.status.text())
        app.events.put(('done', None))
        app.poll()
        self.assertEqual(app.progress.maximum(), 100)

    def test_privileged_protocol_preserves_phase(self):
        phase = {'phase': 'sync', 'completed': 0, 'total': 0}
        def write(*args, progress, **kwargs):
            progress(phase, None)
            return {}
        with patch.object(privileges.os, 'geteuid', return_value=0), patch.object(privileges, 'prepare_unmount', return_value=None), patch('lifus.cli.load_backend') as backend, contextlib.redirect_stdout(io.StringIO()) as output:
            backend.return_value.write_image.side_effect = write
            rc = privileges.worker_main(['write', '/src', '/dev/test', '--yes', '--confirm-device', '/dev/test'])
        self.assertEqual(rc, 0, output.getvalue())
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertIn({'type': 'progress', 'data': [phase, None]}, messages)
