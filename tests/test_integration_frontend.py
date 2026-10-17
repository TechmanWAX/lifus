import queue
import threading
import tempfile
import unittest
from unittest.mock import patch

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from lifus import cli, gui

QT = gui.QApplication.instance() or gui.QApplication([])


class FrontendIntegrationTests(unittest.TestCase):
    def test_advanced_callback_reaches_gui_without_crashing(self):
        app = gui.Application.__new__(gui.Application)
        app.cancel_event = threading.Event()
        app.events = queue.Queue()
        with patch.object(gui, 'load_backend') as load, patch.object(gui.os, 'geteuid', return_value=0):
            def execute(plan, *, progress, cancel):
                progress({'phase': 'write', 'completed': 4, 'total': 8})
                return {'verified': True}
            load.return_value.execute_operation.side_effect = execute
            app.work('format', '', '/dev/test', '', {}, True, True)
        events = list(app.events.queue)
        self.assertFalse(any(kind == 'error' for kind, _ in events), events)
        self.assertIn(('phase', {'phase': 'write', 'completed': 4, 'total': 8}), events)
        self.assertFalse(any(kind == 'progress' for kind, _ in events))

    def test_all_backend_filesystems_accessible(self):
        for fs in ('fat32','ntfs','exfat','ext2','ext3','ext4'):
            args = cli.build_parser().parse_args(['format','/dev/test','--filesystem',fs])
            self.assertEqual(args.filesystem, fs)

    def make_app(self, action='format'):
        with patch.object(gui, 'list_devices', return_value=[]):
            app = gui.Application(gui.QWidget())
        self.addCleanup(app.root.close)
        app.action.setCurrentIndex(app.action.findData(action))
        for name, value in dict(source='/source.iso', destination='/out.img',
                                label='LIFUS', size='4096', staging='/space dir').items():
            getattr(app, name).setText(value)
        app.scheme.setCurrentText('mbr')
        app.passes.setCurrentText('3')
        app.device_box.addItem('disk', '/dev/test')
        app.device_info = {'/dev/test': {}}
        return app

    def test_mounted_usb_requires_warning_and_exact_confirmation(self):
        for approve, typed in ((False, '/dev/test'), (True, None), (True, '/dev/test')):
            with self.subTest(approve=approve, typed=typed):
                app = self.make_app('write')
                app.device_info['/dev/test'] = dict(protected=True, transport='usb', removable=True, mountpoints=['/media/usb'])
                with patch.object(gui, 'load_backend') as load, patch.object(gui.QMessageBox, 'question', return_value=gui.QMessageBox.Yes if approve else gui.QMessageBox.No) as warning, patch.object(gui.QInputDialog, 'getText', return_value=(typed or '', typed is not None)) as confirm, patch.object(gui.QMessageBox, 'critical') as error, patch.object(gui.threading, 'Thread') as thread:
                    load.return_value.plan_unmount.return_value = {'identity': {'serial': 'usb'}, 'mountpoints': ['/media/usb'], 'warning': 'Close files'}
                    app.start()
                error.assert_not_called()
                warning.assert_called_once()
                self.assertIn('/media/usb', warning.call_args.args[2])
                self.assertIn('ALL DATA', warning.call_args.args[2])
                self.assertEqual(confirm.call_count, int(approve))
                load.return_value.plan_unmount.assert_called_once_with('/dev/test', protected_paths=('/source.iso', '/space dir', tempfile.gettempdir()))
                load.return_value.unmount_target.assert_not_called()
                self.assertEqual(thread.call_count, int(approve and typed == '/dev/test'))
                if thread.called:
                    self.assertEqual(thread.call_args.kwargs['args'][-1], {'serial': 'usb'})
                    self.assertTrue(thread.call_args.kwargs['args'][-2])

    def test_gui_unmount_worker_authorization_and_root_order(self):
        from lifus import privileges
        for uid in (0, 1000):
            app = self.make_app('write')
            app.events = queue.Queue()
            with self.subTest(uid=uid), patch.object(gui.os, 'geteuid', return_value=uid), patch.object(privileges, 'run_privileged', return_value={}) as run, patch.object(cli, 'load_backend') as privileged_load, patch.object(gui, 'load_backend') as load:
                privileged_load.return_value.plan_unmount.return_value = {'identity': {'serial': 'usb'}}
                load.return_value.write_image.return_value = {}
                app.work('write', '/source', '/dev/test', '', {'staging_dir': '/stage'}, True, True, True, {'serial': 'usb'})
                if uid:
                    self.assertTrue(cli.build_parser().parse_args(run.call_args.args[0]).unmount)
                    self.assertEqual(cli.build_parser().parse_args(run.call_args.args[0]).unmount_identity, {'serial': 'usb'})
                    privileged_load.assert_not_called()
                    load.assert_not_called()
                else:
                    privileged_load.return_value.unmount_target.assert_called_once()
                    load.return_value.write_image.assert_called_once()
                    self.assertEqual(load.return_value.write_image.call_args.kwargs['expected_identity'], {'serial': 'usb'})
                    run.assert_not_called()
                self.assertFalse(any(kind == 'error' for kind, _ in app.events.queue))

    def test_identity_change_during_gui_confirmation_never_unmounts(self):
        from lifus import privileges
        for uid in (0, 1000):
            for action in ('write', 'format'):
                app = self.make_app(action)
                app.events = queue.Queue()
                app.device_info['/dev/test'] = dict(transport='usb', mountpoints=['/media/usb'])
                original = {'serial': 'A'}
                current = {'identity': original, 'mountpoints': ['/media/usb'], 'warning': 'Close files'}
                def typed(*args):
                    current['identity'] = {'serial': 'B'}
                    return '/dev/test', True
                def elevated(argv, **kwargs):
                    import contextlib
                    import io
                    self.assertEqual(cli.build_parser().parse_args(argv).unmount_identity, original)
                    with patch.object(privileges.os, 'geteuid', return_value=0), contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(privileges.worker_main(argv), 1)
                    raise ValueError(output.getvalue())
                with self.subTest(uid=uid, action=action), patch.object(gui.os, 'geteuid', return_value=uid), patch.object(gui, 'load_backend') as preview, patch.object(cli, 'load_backend') as backend, patch.object(gui.QMessageBox, 'question', return_value=gui.QMessageBox.Yes), patch.object(gui.QInputDialog, 'getText', side_effect=typed), patch.object(gui.QMessageBox, 'critical') as error, patch.object(gui.threading, 'Thread') as thread, patch.object(privileges, 'run_privileged', side_effect=elevated):
                    preview.return_value.plan_unmount.return_value = current
                    backend.return_value.plan_unmount.return_value = current
                    app.start()
                    error.assert_not_called()
                    request = thread.call_args.kwargs['args']
                    self.assertEqual(request[-1], original)
                    app.work(*request)
                    backend.return_value.unmount_target.assert_not_called()
                    preview.return_value.write_image.assert_not_called()
                    preview.return_value.execute_operation.assert_not_called()
                    self.assertTrue(any(kind == 'error' and 'identity' in text for kind, text in app.events.queue))

    def test_preflight_refusal_never_prompts_or_starts_worker(self):
        app = self.make_app('write')
        app.device_info['/dev/test'] = dict(protected=True, transport='usb', mountpoints=['/media/usb'])
        with patch.object(gui, 'load_backend') as load, patch.object(gui.QMessageBox, 'question') as warning, patch.object(gui.QInputDialog, 'getText') as confirm, patch.object(gui.QMessageBox, 'critical') as error, patch.object(gui.threading, 'Thread') as thread:
            load.return_value.plan_unmount.side_effect = ValueError('source is on target')
            app.start()
        error.assert_called_once()
        warning.assert_not_called()
        confirm.assert_not_called()
        thread.assert_not_called()
        load.return_value.unmount_target.assert_not_called()

    def test_nonroot_advanced_starts_only_after_confirmation(self):
        app = self.make_app()
        with patch.object(gui.os, 'access', return_value=True), patch.object(gui.os, 'geteuid', return_value=1000), \
             patch.object(gui.QInputDialog, 'getText', return_value=('/dev/test', True)), \
             patch.object(gui.QMessageBox, 'critical') as error, patch.object(gui.threading, 'Thread') as thread:
            app.start()
        thread.assert_called_once()
        error.assert_not_called()
        app.busy = False
        with patch.object(gui.QInputDialog, 'getText', return_value=('', False)), patch.object(gui.threading, 'Thread') as thread:
            app.start()
        thread.assert_not_called()

    def test_gui_elevated_worker_forwards_options_and_progress(self):
        from lifus import privileges
        app = self.make_app('windows')
        app.events = queue.Queue()
        options = dict(filesystem='ntfs', partition_scheme='gpt', label='Windows disk', persistence_mib=4096,
                       passes=3, staging_dir='/space dir', unattend='/answer file.xml')
        def elevated(argv, **kwargs):
            args = cli.build_parser().parse_args(argv)
            for key, value in options.items():
                self.assertEqual(getattr(args, key), value)
            self.assertTrue(kwargs['graphical'])
            self.assertIs(kwargs['cancel'], app.cancel_event)
            kwargs['progress'](4, 8)
            return {'verified': True}
        with patch.object(gui.os, 'geteuid', return_value=1000), patch.object(privileges, 'run_privileged', side_effect=elevated) as run, patch.object(gui, 'load_backend') as load:
            app.work('windows', '/source.iso', '/dev/test', '', options, True, True)
        run.assert_called_once()
        load.assert_not_called()
        self.assertIn(('progress', (4, 8)), list(app.events.queue))
        self.assertIn(('result', {'verified': True}), list(app.events.queue))

    def test_gui_auth_failure_never_reports_success(self):
        from lifus import privileges
        app = self.make_app('write')
        app.events = queue.Queue()
        with patch.object(gui.os, 'geteuid', return_value=1000), patch.object(privileges, 'run_privileged', side_effect=RuntimeError('sudo authentication cancelled')), patch.object(gui, 'load_backend') as load:
            app.work('write', '/source', '/dev/test', '', {}, False, False)
        load.assert_not_called()
        events = list(app.events.queue)
        self.assertFalse(any(kind == 'result' for kind, _ in events))
        self.assertIn(('error', 'sudo authentication cancelled'), events)
        self.assertEqual(events[-1], ('done', None))

    def test_protected_visible_for_backup_but_write_blocked(self):
        app = self.make_app('backup')
        with patch.object(gui, 'list_devices', return_value=[dict(path='/dev/test', protected=True, mountpoints=['/'])]):
            app.refresh()
        self.assertEqual(list(app.devices.values()), ['/dev/test'])
        name = next(iter(app.devices))
        self.assertIn('запись запрещена', name)
        app.device_box.setCurrentText(name)
        with patch.object(gui.threading, 'Thread') as thread:
            app.start()
            thread.assert_called_once()
        app.busy = False
        app.action.setCurrentIndex(app.action.findData('write'))
        with patch.object(gui.QMessageBox, 'critical') as error, patch.object(gui.threading, 'Thread') as thread:
            app.start()
            thread.assert_not_called()
            error.assert_called_once()

    def test_doctor_all_filesystems(self):
        tools = cli.doctor()['tools']
        for name in ('mkfs.fat', 'mkfs.exfat', 'mkfs.ext2', 'mkfs.ext3'):
            self.assertIn(name, tools)

    def test_optional_commands_and_unattend(self):
        for command, method in [('raw-to-vhd', 'raw_to_vhd'), ('vhd-to-raw', 'vhd_to_raw')]:
            with patch.object(cli, 'load_backend') as load:
                cli.dispatch(cli.build_parser().parse_args([command, '/src', '/dst']))
                getattr(load.return_value, method).assert_called_once_with('/src', '/dst')
        with patch.object(cli, 'load_backend') as load:
            cli.dispatch(cli.build_parser().parse_args(['unattend', '/out.xml']))
            kwargs = load.return_value.write_unattend.call_args.kwargs
            self.assertFalse(kwargs['bypass_tpm'])
            self.assertFalse(kwargs['bypass_secure_boot'])
        with patch.object(cli, 'load_backend') as load:
            cli.dispatch(cli.build_parser().parse_args(['windows', '/src', '/dev/test', '--unattend', '/out.xml']))
            self.assertEqual(load.return_value.plan_operation.call_args.kwargs['unattend'], '/out.xml')

    def test_staging_dir_forwarded(self):
        args = cli.build_parser().parse_args(['format', '/dev/test', '--staging-dir', '/space'])
        with patch.object(cli, 'load_backend') as load:
            cli.dispatch(args)
            self.assertEqual(load.return_value.plan_operation.call_args.kwargs['staging_dir'], '/space')

    def test_badblocks_passes_accessible(self):
        args = cli.build_parser().parse_args(['badblocks','/dev/test','--passes','2'])
        with patch.object(cli,'load_backend') as load:
            cli.dispatch(args)
            self.assertEqual(load.return_value.plan_operation.call_args.kwargs['passes'], 2)


class PackagingTests(unittest.TestCase):
    def test_reproducible_deb_and_corresponding_source(self):
        import hashlib
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tarfile
        import tempfile
        base = Path(__file__).resolve().parents[1]
        env = dict(os.environ, SOURCE_DATE_EPOCH='1700000000')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hashes = []
            for name in ('first', 'second'):
                out = root / name
                subprocess.run([sys.executable, str(base / 'build-deb.py'), '--output-dir', str(out)], env=env, check=True, capture_output=True)
                deb = next(out.glob('*.deb'))
                hashes.append(hashlib.sha256(deb.read_bytes()).hexdigest())
            self.assertEqual(*hashes)
            extracted = root / 'unpacked'
            subprocess.run(['dpkg-deb', '-x', str(deb), str(extracted)], check=True)
            self.assertTrue((extracted / 'usr/share/doc/lifus/FEATURES.md').is_file())
            self.assertTrue((extracted / 'usr/share/doc/lifus/SOURCE.md').is_file())
            self.assertTrue((extracted / 'usr/share/lifus/upstream/res/uefi/uefi-ntfs.img').is_file())
            for source in (base / 'lifus').glob('*.py'):
                self.assertEqual(source.read_bytes(), (extracted / 'usr/share/lifus/lifus' / source.name).read_bytes())
            archives = []
            for name in ('first', 'second'):
                subprocess.run([sys.executable, str(base / 'build-source.py'), '--output-dir', str(root / name)], env=env, check=True, capture_output=True)
                archives.append(next((root / name).glob('*.tar.gz')))
            self.assertEqual(hashlib.sha256(archives[0].read_bytes()).digest(), hashlib.sha256(archives[1].read_bytes()).digest())
            with tarfile.open(archives[0]) as archive:
                names = archive.getnames()
                self.assertIn('lifus-0.4.0/lifus/cli.py', names)
                self.assertIn('lifus-0.4.0/upstream/res/freedos/KERNEL.SYS', names)
                self.assertIn('lifus-0.4.0/COPYING.md', names)
                self.assertFalse(any(n.startswith(('lifus-0.4.0/src/', 'lifus-0.4.0/res/',
                                                  'lifus-0.4.0/.mingw/', 'lifus-0.4.0/.vs/',
                                                  'lifus-0.4.0/.github/')) for n in names))
                self.assertNotIn('lifus-0.4.0/configure', names)
                self.assertFalse(any('/.git/' in n or '/dist/' in n or '/__pycache__/' in n for n in names))
                self.assertFalse(any(n.startswith('lifus-0.4.0/linux/') for n in names))
                self.assertIn('lifus-0.4.0/build-deb.py', names)
                self.assertIn('lifus-0.4.0/SOURCE_DATE_EPOCH', names)
                archive.extractall(root / 'source', filter='data')
            source_root = root / 'source/lifus-0.4.0'
            clean_env = dict(os.environ)
            clean_env.pop('SOURCE_DATE_EPOCH', None)
            subprocess.run([sys.executable, str(source_root / 'build-deb.py')],
                           cwd=source_root, env=clean_env, check=True, capture_output=True)
            rebuilt = source_root / 'dist/lifus_0.4.0-1_all.deb'
            self.assertEqual(hashlib.sha256(rebuilt.read_bytes()).hexdigest(), hashes[0])


if __name__ == '__main__':
    unittest.main()
