"""Real Qt widgets; all disk discovery and destructive workers are isolated."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QWidget
from lifus import gui

QT = QApplication.instance() or QApplication([])


class QtGuiTests(unittest.TestCase):
    def setUp(self):
        with patch.object(gui, 'list_devices', return_value=[]):
            self.app = gui.Application(QWidget())
        self.addCleanup(self.app.root.close)

    def test_rufus_windows_defaults_and_widgets(self):
        self.assertEqual(self.app.action.currentData(), 'windows')
        self.assertEqual(self.app.scheme.currentText(), 'gpt')
        self.assertIn('UEFI', self.app.target_system.text())
        self.assertEqual(self.app.start_button.text(), 'СТАРТ')
        self.assertTrue(self.app.preview_button.isEnabled())
        self.assertFalse(hasattr(self.app, 'execute'))
        self.app.root.show()
        QT.processEvents()
        self.assertLessEqual(self.app.root.width(), 650)

    def test_windows_mbr_does_not_promise_unsupported_bios_boot(self):
        self.app.scheme.setCurrentText('mbr')
        self.assertIn('UEFI', self.app.target_system.text())
        self.assertNotIn('BIOS', self.app.target_system.text())

    def test_raw_disables_ineffective_settings_and_restores_windows(self):
        self.app.action.setCurrentIndex(self.app.action.findData('write'))
        for widget in (self.app.scheme, self.app.filesystem, self.app.label):
            self.assertFalse(widget.isEnabled())
        self.assertIn('из образа', self.app.mode_hint.text())
        self.assertFalse(self.app.preview_button.isEnabled())
        self.app.action.setCurrentIndex(self.app.action.findData('windows'))
        self.assertTrue(self.app.scheme.isEnabled())
        self.assertTrue(self.app.filesystem.isEnabled())
        self.assertFalse(self.app.verify.isEnabled())

    def test_windows_start_executes_selected_settings_after_exact_confirmation(self):
        from lifus import privileges
        app = self.app
        app.source.setText('/windows.iso')
        app.device_box.addItem('Test disk', '/dev/test')
        app.device_info['/dev/test'] = {}
        app.filesystem.setCurrentText('ntfs')
        with patch.object(gui.QInputDialog, 'getText', return_value=('/dev/test', True)) as confirm, patch.object(gui.threading, 'Thread') as thread:
            app.start_button.click()
        confirm.assert_called_once()
        request = thread.call_args.kwargs['args']
        self.assertEqual(request[0], 'windows')
        self.assertTrue(request[5])
        self.assertEqual(request[4]['partition_scheme'], 'gpt')
        self.assertEqual(request[4]['filesystem'], 'ntfs')
        self.assertFalse(app.controls.isEnabled())
        self.assertTrue(app.cancel_button.isEnabled())
        with patch.object(gui.os, 'geteuid', return_value=1000), patch.object(privileges, 'run_privileged', return_value={'verified': True}) as run:
            app.work(*request)
        self.assertIn('--execute', run.call_args.args[0])
        self.assertIn('--confirm-device=/dev/test', run.call_args.args[0])
        app.poll()
        self.assertEqual(app.progress.value(), 100)
        self.assertFalse(app.busy)
        self.assertTrue(app.controls.isEnabled())

    def test_preview_never_confirms_elevates_unmounts_or_executes(self):
        from lifus import privileges
        app = self.app
        app.source.setText('/windows.iso')
        app.device_box.addItem('Test disk', '/dev/test')
        app.device_info['/dev/test'] = dict(mountpoints=['/media/test'], transport='usb')
        with patch.object(gui.QInputDialog, 'getText') as confirm, patch.object(gui.QMessageBox, 'question') as unmount, patch.object(gui.threading, 'Thread') as thread:
            app.preview_button.click()
        confirm.assert_not_called()
        unmount.assert_not_called()
        request = thread.call_args.kwargs['args']
        self.assertFalse(request[5])
        self.assertFalse(request[-2])
        with patch.object(gui.os, 'geteuid', return_value=1000), patch.object(privileges, 'run_privileged') as elevated, patch.object(gui, 'load_backend') as load:
            load.return_value.plan_operation.return_value = {'plan': 'preview'}
            app.work(*request)
        elevated.assert_not_called()
        load.return_value.execute_operation.assert_not_called()
        load.return_value.plan_operation.assert_called_once()
        app.poll()
        self.assertIn('запись не выполнялась', app.status.text())

    def test_direct_fat32_preview_reports_no_payload_staging(self):
        self.app.previewing = True
        self.app.events.put(('result', dict(action='windows', staging_bytes=0,
                                         options=dict(filesystem='fat32'))))
        self.app.poll()
        self.assertIn('Временные данные: 0 байт', self.app.log.toPlainText())
        self.assertIn('без образа раздела', self.app.log.toPlainText())

    def test_real_background_checksum_and_qt_timer(self):
        import hashlib
        import tempfile
        import time
        from pathlib import Path
        app = self.app
        app.action.setCurrentIndex(app.action.findData('checksum'))
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'image.img'
            image.write_bytes(b'Qt worker integration')
            app.source.setText(str(image))
            app.start_button.click()
            deadline = time.monotonic() + 5
            while app.busy and time.monotonic() < deadline:
                QT.processEvents()
                time.sleep(0.01)
            self.assertFalse(app.busy)
            self.assertIn(hashlib.sha256(image.read_bytes()).hexdigest(), app.log.toPlainText())
            self.assertEqual(app.progress.value(), 100)

    def test_error_and_cancel_never_report_success(self):
        for event in [('error', 'test failure'), ('result', {})]:
            with self.subTest(event=event):
                self.app.cancel_event.set()
                self.app.events.put(event)
                self.app.events.put(('done', None))
                self.app.poll()
                self.assertEqual(self.app.progress.value(), 0)
                self.assertNotEqual(self.app.status.text(), 'Завершено')
                self.assertTrue(self.app.start_button.isEnabled())

    def test_all_previous_operations_available(self):
        actions = {self.app.action.itemData(i) for i in range(self.app.action.count())}
        self.assertTrue(set(('write', 'backup', 'inspect', 'checksum', 'freedos', *gui.ADVANCED)) <= actions)

    def test_busy_close_requests_cancel_without_destroying_window(self):
        app = gui.Application()
        app.root.show()
        app.busy = True
        with patch.object(gui.QMessageBox, 'information'):
            app.root.close()
        self.assertTrue(app.root.isVisible())
        self.assertTrue(app.cancel_event.is_set())
        app.busy = False
        app.root.close()
        self.assertFalse(app.root.isVisible())
