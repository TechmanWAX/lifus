import os
import threading
import tempfile
import unittest
from unittest.mock import patch

from lifus import cli


class PrivilegeTests(unittest.TestCase):
    def test_explicit_unmount_worker_orders_preflight_unmount_write(self):
        import contextlib
        import io
        from unittest.mock import MagicMock
        from lifus import privileges
        backend = MagicMock()
        backend.plan_unmount.return_value = {'identity': {'serial': 'usb'}, 'mountpoints': ['/media/usb'], 'warning': 'Unmount first'}
        backend.write_image.return_value = {'verified': True}
        argv = ['write', '/source', '/dev/test', '--unmount', '--yes', '--confirm-device', '/dev/test']
        argv += ['--unmount-identity={"serial":"usb"}']
        with patch.object(privileges.os, 'geteuid', return_value=0), patch.object(cli, 'load_backend', return_value=backend), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(privileges.worker_main(argv), 0)
        self.assertEqual([call[0] for call in backend.mock_calls], ['plan_unmount', 'unmount_target', 'write_image'])
        backend.plan_unmount.assert_called_once_with('/dev/test', protected_paths=('/source', tempfile.gettempdir()))
        self.assertEqual(backend.unmount_target.call_args.kwargs['expected_identity'], {'serial': 'usb'})
        self.assertIsInstance(backend.unmount_target.call_args.kwargs['cancel'], threading.Event)

    def test_unmount_failure_blocks_advanced_plan_and_protects_inputs(self):
        import contextlib
        import io
        from lifus import privileges
        argv = ['windows', '/source', '/dev/test', '--execute', '--unmount', '--yes', '--confirm-device', '/dev/test', '--unattend', '/answer.xml', '--staging-dir', '/stage']
        argv += ['--unmount-identity={"serial":"usb"}']
        for failure in ('plan_unmount', 'unmount_target'):
            with self.subTest(failure=failure), patch.object(privileges.os, 'geteuid', return_value=0), patch.object(cli, 'load_backend') as load, contextlib.redirect_stdout(io.StringIO()):
                load.return_value.plan_unmount.return_value = {'identity': {'serial': 'usb'}}
                getattr(load.return_value, failure).side_effect = ValueError('unsafe or busy')
                self.assertEqual(privileges.worker_main(argv), 1)
                load.return_value.plan_unmount.assert_called_once_with('/dev/test', protected_paths=('/source', '/answer.xml', '/stage', tempfile.gettempdir()))
                load.return_value.plan_operation.assert_not_called()
                load.return_value.execute_operation.assert_not_called()
                if failure == 'plan_unmount':
                    load.return_value.unmount_target.assert_not_called()

    def test_unmount_cancelled_before_plan(self):
        from lifus import privileges
        cancel = threading.Event()
        cancel.set()
        args = cli.build_parser().parse_args(['write', '/source', '/dev/test', '--unmount', '--yes', '--confirm-device', '/dev/test'])
        with patch.object(cli, 'load_backend') as load, self.assertRaises(InterruptedError):
            privileges.prepare_unmount(args, cancel=cancel)
        load.assert_not_called()

    def test_extracted_install_resolves_own_askpass(self):
        import tempfile
        from pathlib import Path
        from lifus import privileges
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            module = root / 'usr/share/lifus/lifus/privileges.py'
            module.parent.mkdir(parents=True)
            module.touch()
            helper = root / 'usr/libexec/lifus-askpass'
            helper.parent.mkdir(parents=True)
            helper.touch()
            with patch.object(privileges, '__file__', str(module)):
                self.assertEqual(privileges.helper_path('lifus-askpass'), str(helper))

    def test_confirm_before_elevation_and_forward_validated_arguments(self):
        from lifus import privileges
        args = cli.build_parser().parse_args(['write', '/source', '/dev/test', '--yes', '--confirm-device', '/dev/test', '--no-verify'])
        with patch.object(privileges.os, 'geteuid', return_value=1000), patch.object(privileges, 'run_privileged', return_value={'verified': False}) as run:
            self.assertEqual(cli.dispatch(args), {'verified': False})
            forwarded = run.call_args.args[0]
            self.assertEqual(vars(cli.build_parser().parse_args(forwarded)), vars(args))
            args.confirm_device = '/dev/other'
            with self.assertRaises(ValueError):
                cli.dispatch(args)
            self.assertEqual(run.call_count, 1)

    def test_worker_revalidates_allowlist_and_confirmation(self):
        import contextlib
        import io
        from lifus import privileges
        for argv in (['gui'], ['format', '/dev/test'], ['write', '/source', '/dev/test', '--yes', '--confirm-device', '/wrong']):
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()) as output, patch.object(cli, 'load_backend') as load:
                self.assertNotEqual(privileges.worker_main(argv), 0)
                load.assert_not_called()
                self.assertIn('"type": "error"', output.getvalue())

    def test_plan_file_write_and_root_do_not_elevate(self):
        import tempfile
        from lifus import privileges
        with tempfile.NamedTemporaryFile() as target, patch.object(privileges.os, 'geteuid', return_value=1000), patch.object(privileges, 'run_privileged') as elevated, patch.object(cli, 'load_backend') as load:
            cli.dispatch(cli.build_parser().parse_args(['format', '/dev/test']))
            cli.dispatch(cli.build_parser().parse_args(['write', '/source', target.name, '--allow-file', '--yes', '--confirm-device', target.name]))
            elevated.assert_not_called()
            load.return_value.write_image.assert_called_once()
        with patch.object(privileges.os, 'geteuid', return_value=0), patch.object(privileges, 'run_privileged') as elevated, patch.object(cli, 'load_backend'):
            cli.dispatch(cli.build_parser().parse_args(['write', '/source', '/dev/test', '--yes', '--confirm-device', '/dev/test']))
            elevated.assert_not_called()

    def test_worker_callbacks_safe_path_and_cooperative_cancel(self):
        import contextlib
        import io
        import json
        import signal
        from lifus import privileges
        argv = ['windows', '/source.iso', '/dev/test', '--yes', '--confirm-device', '/dev/test', '--execute', '--filesystem', 'ntfs', '--unattend', '/file.xml', '--staging-dir', '/space dir', '--passes', '3']
        for stop in (False, True):
            with self.subTest(stop=stop), patch.object(privileges.os, 'geteuid', return_value=0), patch.dict(os.environ, {'PATH': '/untrusted'}), patch.object(cli, 'load_backend') as load, contextlib.redirect_stdout(io.StringIO()) as output:
                def execute(plan, *, progress, cancel):
                    self.assertEqual(os.environ['PATH'], privileges.SAFE_PATH)
                    progress({'completed': 1, 'total': 2})
                    if stop:
                        os.kill(os.getpid(), signal.SIGTERM)
                        self.assertTrue(cancel.is_set())
                    return {'verified': True}
                load.return_value.execute_operation.side_effect = execute
                self.assertEqual(privileges.worker_main(argv), int(stop))
                options = load.return_value.plan_operation.call_args.kwargs
                self.assertEqual(options['unattend'], '/file.xml')
                self.assertEqual(options['filesystem'], 'ntfs')
                self.assertEqual(options['passes'], 3)
                records = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(records[0], {'type': 'progress', 'data': [1, 2]})
                self.assertEqual(records[-1]['type'], 'error' if stop else 'result')

    def test_source_entry_ignores_pythonpath_and_rejects_gui(self):
        import subprocess
        import tempfile
        from pathlib import Path
        from lifus import privileges
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'lifus.py').write_text("raise RuntimeError('injected module')")
            result = subprocess.run(['/usr/bin/python3', '-I', privileges.helper_path('lifus-privileged.py'), 'gui'], cwd=tmp, env=dict(os.environ, PYTHONPATH=tmp), capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Unsupported privileged command', result.stdout)
        self.assertNotIn('injected module', result.stderr)

    def test_option_values_beginning_with_dash_roundtrip(self):
        from lifus import privileges
        args = cli.build_parser().parse_args(['format', '/dev/test', '--execute', '--yes', '--confirm-device', '/dev/test', '--label=-LIFUS'])
        restored = cli.build_parser().parse_args(privileges.command_arguments(args))
        self.assertEqual(restored.label, '-LIFUS')
        args = cli.build_parser().parse_args(['write', '--yes', '--confirm-device=-target', '--', '-source', '-target'])
        restored = cli.build_parser().parse_args(privileges.command_arguments(args))
        self.assertEqual(vars(restored), vars(args))

    def test_cancel_before_auth_never_starts_sudo(self):
        from lifus import privileges
        cancel = threading.Event()
        cancel.set()
        with patch.object(privileges.subprocess, 'Popen') as popen, self.assertRaises(InterruptedError):
            privileges.run_privileged(['write', '/source', '/dev/test', '--yes', '--confirm-device', '/dev/test'], graphical=True, cancel=cancel)
        popen.assert_not_called()

    def test_askpass_cancel_never_outputs_a_secret(self):
        import contextlib
        import io
        import runpy
        from pathlib import Path
        helper = Path(__file__).resolve().parents[1] / 'packaging' / 'lifus-askpass'
        with patch('tkinter.Tk'), patch('tkinter.simpledialog.askstring', return_value=None) as prompt, contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(helper), run_name='__main__')
        self.assertNotEqual(stopped.exception.code, 0)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(prompt.call_args.kwargs['show'], '*')
        self.assertEqual(helper.read_text().splitlines()[0], '#!/usr/bin/python3 -I')

    def transport(self, code, *, graphical=True, cancel=None, progress=None):
        import subprocess
        import sys
        from lifus import privileges
        real_popen = subprocess.Popen
        calls = []
        def fake_sudo(command, **kwargs):
            calls.append((command, kwargs))
            return real_popen([sys.executable, '-u', '-c', code], **kwargs)
        argv = ['write', '/source', '/dev/test', '--yes', '--confirm-device', '/dev/test']
        with patch.object(privileges.subprocess, 'Popen', side_effect=fake_sudo):
            result = privileges.run_privileged(argv, graphical=graphical, cancel=cancel, progress=progress)
        return result, calls

    def test_isolated_sudo_and_progress_protocol(self):
        from lifus import privileges
        seen = []
        result, calls = self.transport("import json; print(json.dumps({'type':'progress','data':[4,8]})); print(json.dumps({'type':'result','data':{'verified':True}}))", progress=lambda *v: seen.append(v))
        self.assertEqual(result, {'verified': True})
        self.assertEqual(seen, [(4, 8)])
        command, kwargs = calls[0]
        self.assertEqual(command[:6], ['/usr/bin/sudo', '-k', '-A', '--', '/usr/bin/python3', '-I'])
        self.assertTrue(os.path.isabs(command[6]))
        self.assertTrue(os.path.isabs(kwargs['env']['SUDO_ASKPASS']))
        self.assertNotIn('-S', command)
        _, calls = self.transport("print('{\"type\":\"result\",\"data\":{}}')", graphical=False)
        self.assertEqual(calls[0][0][:5], ['/usr/bin/sudo', '-k', '--', '/usr/bin/python3', '-I'])

    def test_auth_failure_drains_large_stderr_without_success(self):
        with self.assertRaisesRegex(RuntimeError, 'sudo.*failed'):
            self.transport("import sys; sys.stderr.write('denied ' * 50000); sys.exit(1)")

    def test_missing_or_error_result_is_failure(self):
        for code in ("pass", "print('{\"type\":\"error\",\"data\":\"write refused\"}')", "print('{\"type\":\"result\",\"data\":{}}'); raise SystemExit(1)"):
            with self.subTest(code=code), self.assertRaises(RuntimeError):
                self.transport(code)

    def test_cancel_is_forwarded_to_worker(self):
        cancel = threading.Event()
        def progress(*args):
            cancel.set()
        code = "import signal,time; signal.signal(signal.SIGTERM, lambda *a: exit(2)); print('{\"type\":\"progress\",\"data\":[0,1]}',flush=True); time.sleep(10)"
        with self.assertRaisesRegex(InterruptedError, 'cancelled'):
            self.transport(code, cancel=cancel, progress=progress)


if __name__ == '__main__':
    unittest.main()
