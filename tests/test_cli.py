import contextlib
import importlib
import io
import json
import unittest
from unittest.mock import patch, MagicMock


class CLITests(unittest.TestCase):
    def cli(self):
        try:
            return importlib.import_module('lifus.cli')
        except ImportError as exc:
            self.fail('CLI not implemented: ' + str(exc))

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.cli().main(args)
        return code, out.getvalue(), err.getvalue()

    def test_unmount_warning_before_sudo_and_root_revalidation(self):
        from lifus import privileges
        cli = self.cli()
        for uid in (1000, 0):
            with self.subTest(uid=uid), patch.object(cli.os, 'geteuid', return_value=uid), patch.object(cli, 'load_backend') as load, patch.object(privileges, 'run_privileged') as sudo:
                load.return_value.plan_unmount.return_value = {'identity': {'serial': 'usb'}, 'mountpoints': ['/media/usb'], 'warning': 'Close files first'}
                load.return_value.write_image.return_value = {}
                def elevated(argv):
                    self.assertTrue(cli.build_parser().parse_args(argv).unmount)
                    self.assertIn('/media/usb', captured.getvalue())
                    return {}
                sudo.side_effect = elevated
                captured = io.StringIO()
                with contextlib.redirect_stderr(captured):
                    result = cli.dispatch(cli.build_parser().parse_args(['write', '/source', '/dev/test', '--unmount', '--yes', '--confirm-device', '/dev/test']))
                self.assertIn('ALL DATA', captured.getvalue())
                self.assertEqual(load.return_value.unmount_target.call_count, int(uid == 0))
                self.assertEqual(load.return_value.plan_unmount.call_count, 2 if uid == 0 else 1)

    def test_unmount_plan_and_unconfirmed_refuse_without_backend(self):
        cli = self.cli()
        for argv in (['format', '/dev/test', '--unmount'], ['write', '/src', '/dev/test', '--unmount'], ['devices', '--unmount']):
            with patch.object(cli, 'load_backend') as load:
                code, _, _ = self.run_cli(argv)
                self.assertNotEqual(code, 0)
                load.assert_not_called()

    def test_devices_json(self):
        cli = self.cli()
        with patch.object(cli, 'load_backend') as load:
            load.return_value.list_devices.return_value = [{'path': '/dev/test'}]
            code, out, _ = self.run_cli(['devices', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [{'path': '/dev/test'}])

    def test_write_requires_both_exact_confirmations(self):
        cli = self.cli()
        for flags in ([], ['--yes'], ['--yes', '--confirm-device', '/dev/wrong']):
            with patch.object(cli, 'load_backend') as load:
                code, out, err = self.run_cli(['write', 'a.iso', '/dev/test', '--json'] + flags)
            self.assertNotEqual(code, 0)
            self.assertIn('error', json.loads(out))
            load.assert_not_called()

    def test_confirmed_write(self):
        cli = self.cli()
        with patch.object(cli.os, 'geteuid', return_value=0), patch.object(cli, 'load_backend') as load:
            load.return_value.write_image.return_value = {'verified': True}
            code, out, _ = self.run_cli(['write', 'a.iso', '/dev/test', '--yes', '--confirm-device', '/dev/test', '--json'])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)['verified'])
            load.return_value.write_image.assert_called_once_with('a.iso', '/dev/test', allow_file=False, verify=True)

    def test_advanced_default_is_dry_run(self):
        cli = self.cli()
        with patch.object(cli, 'load_backend') as load:
            load.return_value.plan_operation.return_value = {'action': 'format'}
            code, out, _ = self.run_cli(['format', '/dev/test', '--json'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)['action'], 'format')
            load.return_value.execute_operation.assert_not_called()

    def test_backend_error_is_json(self):
        cli = self.cli()
        with patch.object(cli, 'load_backend', side_effect=PermissionError('denied')):
            code, out, _ = self.run_cli(['devices', '--json'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['error'], 'denied')

    def test_freedos_creates_image(self):
        cli = self.cli()
        with patch.object(cli, 'load_backend') as load:
            load.return_value.create_image.return_value = {'path': 'dos.img'}
            code, out, _ = self.run_cli(['freedos', 'dos.img', '--size-mib', '64', '--json'])
            self.assertEqual(code, 0)
            load.return_value.create_image.assert_called_once_with('dos.img', size_mib=64, label='FREEDOS')

    def test_native_gui_controls(self):
        import os
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication, QWidget
        qt = QApplication.instance() or QApplication([])
        root = QWidget()
        try:
            try:
                gui = importlib.import_module('lifus.gui')
            except ImportError as exc:
                self.fail('GUI not implemented: ' + str(exc))
            with patch.object(gui, 'list_devices', return_value=[]):
                app = gui.Application(root)
                qt.processEvents()
                self.assertGreaterEqual(app.action_box.findData('write'), 0)
                self.assertGreaterEqual(app.action_box.findData('freedos'), 0)
                self.assertEqual(app.start_button.text(), 'СТАРТ')
                self.assertFalse(app.cancel_event.is_set())
                app.cancel()
                self.assertTrue(app.cancel_event.is_set())
        finally:
            root.close()

    def test_debian_build_contains_launcher_and_assets(self):
        import pathlib
        import subprocess
        import tempfile
        base = pathlib.Path(__file__).resolve().parents[1]
        self.assertTrue((base / 'build-deb.py').is_file(), 'Debian builder missing')
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(['/usr/bin/python3', str(base / 'build-deb.py'), '--output-dir', temp], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            packages = list(pathlib.Path(temp).glob('*.deb'))
            self.assertEqual(len(packages), 1)
            depends = subprocess.check_output(['dpkg-deb', '-f', str(packages[0]), 'Depends'], text=True)
            for dependency in ('python3-pyside6.qtwidgets', 'python3-pyside6.qtcore', 'python3-pyside6.qtgui', 'python3-tk'):
                self.assertIn(dependency, depends)
            contents = subprocess.check_output(['dpkg-deb', '--contents', str(packages[0])], text=True)
            for name in ('usr/bin/lifus', 'usr/share/lifus/lifus-privileged.py', 'usr/libexec/lifus-askpass', 'usr/share/lifus/lifus/cli.py', 'usr/share/applications/lifus.desktop', 'usr/share/lifus/upstream/res/freedos/', 'mbr_win7.h', 'usr/share/doc/lifus/copyright'):
                self.assertIn(name, contents)

if __name__ == '__main__':
    unittest.main()
