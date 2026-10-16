"""Consent identity must survive prompts, sudo, and backend handoff."""
import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from lifus import cli, privileges


class HandoffTests(unittest.TestCase):
    def args(self, command='write'):
        argv = [command]
        if command in ('write', 'windows', 'persistence'):
            argv += ['/source']
        argv += ['/dev/test', '--unmount', '--yes', '--confirm-device=/dev/test']
        if command != 'write':
            argv += ['--execute']
        return cli.build_parser().parse_args(argv)

    def test_snapshot_roundtrip_and_worker_backend_handoff(self):
        snapshot = {'serial': 'A " unicode Ω', 'size': 123, 'topology': [{'inode': 456}]}
        for command in ('write', 'format', 'windows', 'persistence', 'badblocks'):
            with self.subTest(command=command):
                args = self.args(command)
                args.unmount_identity = snapshot
                argv = privileges.command_arguments(args)
                restored = cli.build_parser().parse_args(argv)
                self.assertEqual(restored.unmount_identity, snapshot)
                with patch.object(cli, 'load_backend') as load, patch.object(privileges.os, 'geteuid', return_value=0), contextlib.redirect_stdout(io.StringIO()):
                    load.return_value.plan_unmount.return_value = {'identity': snapshot}
                    load.return_value.write_image.return_value = {}
                    load.return_value.execute_operation.return_value = {}
                    self.assertEqual(privileges.worker_main(argv), 0)
                    method = load.return_value.write_image if command == 'write' else load.return_value.execute_operation
                    self.assertEqual(method.call_args.kwargs['expected_identity'], snapshot)

    def test_invalid_snapshot_rejected_at_parser_and_boundary(self):
        argv = ['write', '/source', '/dev/test', '--unmount', '--yes', '--confirm-device=/dev/test']
        for value in ('no-json', 'null', '[]', '42', '"string"', '{}', '{"a":NaN}', '{"a":"' + 'a' * 65536 + '"}'):
            with self.subTest(value=value[:40]), self.assertRaises(ValueError):
                cli.build_parser().parse_args(argv + ['--unmount-identity=' + value])
        with self.assertRaises(ValueError):
            cli.build_parser().parse_args(['write', '/source', '/dev/test', '--unmount-identity={"serial":"A"}'])
        with self.assertRaises(ValueError):
            privileges.command_arguments(self.args())

    def test_cli_original_snapshot_survives_root_and_sudo(self):
        snapshot = {'serial': 'A'}
        for command in ('write', 'format'):
            for uid in (0, 1000):
                with self.subTest(command=command, uid=uid), patch.object(privileges.os, 'geteuid', return_value=uid), patch.object(cli, 'load_backend') as load, patch.object(privileges, 'run_privileged') as run, contextlib.redirect_stderr(io.StringIO()) as warning:
                    load.return_value.plan_unmount.return_value = {'identity': snapshot, 'mountpoints': ['/media/usb'], 'warning': 'Close files'}
                    cli.dispatch(self.args(command))
                    self.assertIn('/media/usb', warning.getvalue())
                    if uid:
                        self.assertEqual(cli.build_parser().parse_args(run.call_args.args[0]).unmount_identity, snapshot)
                    else:
                        method = load.return_value.write_image if command == 'write' else load.return_value.execute_operation
                        self.assertEqual(method.call_args.kwargs['expected_identity'], snapshot)

    def test_cli_does_not_replace_external_original_snapshot(self):
        args = self.args()
        args.unmount_identity = {'serial': 'A'}
        with patch.object(cli, 'load_backend') as load, patch.object(privileges, 'run_privileged') as run:
            load.return_value.plan_unmount.return_value = {'identity': {'serial': 'B'}, 'mountpoints': [], 'warning': ''}
            with self.assertRaisesRegex(ValueError, 'identity'):
                cli.dispatch(args)
            run.assert_not_called()
            load.return_value.unmount_target.assert_not_called()
            load.return_value.write_image.assert_not_called()
            self.assertEqual(args.unmount_identity, {'serial': 'A'})

    def test_worker_refuses_missing_identity_without_backend(self):
        args = self.args()
        argv = ['write', '/source', '/dev/test', '--unmount', '--yes', '--confirm-device=/dev/test']
        with patch.object(cli, 'load_backend') as load, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(privileges.worker_main(argv), 1)
        load.assert_not_called()
        self.assertIn('confirmed preflight identity', output.getvalue())

    def test_sudo_boundary_does_not_reconfirm_replacement(self):
        args = self.args()
        original = {'serial': 'A'}
        def sudo(argv):
            with patch.object(privileges.os, 'geteuid', return_value=0), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(privileges.worker_main(argv), 1)
            self.assertIn('identity differs', output.getvalue())
        with patch.object(privileges.os, 'geteuid', return_value=1000), patch.object(cli, 'load_backend') as load, patch.object(privileges, 'run_privileged', side_effect=sudo), contextlib.redirect_stderr(io.StringIO()):
            load.return_value.plan_unmount.side_effect = [
                {'identity': original, 'mountpoints': ['/media/usb'], 'warning': ''},
                {'identity': {'serial': 'B'}}]
            cli.dispatch(args)
            load.return_value.unmount_target.assert_not_called()
            load.return_value.write_image.assert_not_called()
        self.assertEqual(args.unmount_identity, original)

    def test_worker_uses_its_actual_temp_directory_and_refuses(self):
        for command in ('write', 'windows'):
            args = self.args(command)
            args.unmount_identity = {'serial': 'A'}
            args.staging_dir = '/explicit'
            argv = privileges.command_arguments(args)
            def preflight(target, *, protected_paths):
                self.assertIn('/worker-temp-on-target', protected_paths)
                raise ValueError('Protected staging path is on target')
            with self.subTest(command=command), patch.object(privileges.os, 'geteuid', return_value=0), patch.object(tempfile, 'gettempdir', return_value='/worker-temp-on-target'), patch.object(cli, 'load_backend') as load, contextlib.redirect_stdout(io.StringIO()) as output:
                load.return_value.plan_unmount.side_effect = preflight
                self.assertEqual(privileges.worker_main(argv), 1)
                self.assertIn('Protected staging path', output.getvalue())
                load.return_value.unmount_target.assert_not_called()
                load.return_value.write_image.assert_not_called()
                load.return_value.execute_operation.assert_not_called()

    def test_real_preflight_refuses_implicit_temp_backed_by_target(self):
        import test_unmount
        from lifus import unmount
        fixture = test_unmount.UnmountTests()
        fixture.setUp()
        try:
            original = unmount.plan_unmount(fixture.target)['identity']
            argv = ['format', fixture.target, '--execute', '--unmount', '--yes',
                    '--confirm-device=' + fixture.target, '--staging-dir=/explicit',
                    '--unmount-identity=' + json.dumps(original)]
            with patch.object(tempfile, 'gettempdir', return_value='/media/USB/temp'), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(privileges.worker_main(argv), 1)
            self.assertIn('error', output.getvalue())
            self.assertIn('Target backs a protected source/staging path', output.getvalue())
            self.assertEqual(fixture.actions(), [])
        finally:
            fixture.doCleanups()

    def test_prepare_rejects_changed_identity_before_unmount(self):
        args = self.args()
        args.unmount_identity = {'serial': 'A'}
        with patch.object(cli, 'load_backend') as load:
            load.return_value.plan_unmount.return_value = {'identity': {'serial': 'B'}}
            with self.assertRaisesRegex(ValueError, 'identity'):
                privileges.prepare_unmount(args)
            load.return_value.unmount_target.assert_not_called()

    def test_preparation_returns_original_and_protects_implicit_temp(self):
        args = self.args()
        args.unmount_identity = {'serial': 'A'}
        args.staging_dir = '/explicit'
        with patch.object(cli, 'load_backend') as load, patch.object(tempfile, 'gettempdir', return_value='/actual-temp'):
            load.return_value.plan_unmount.return_value = {'identity': {'serial': 'A'}}
            self.assertEqual(privileges.prepare_unmount(args), args.unmount_identity)
            self.assertEqual(load.return_value.plan_unmount.call_args.kwargs['protected_paths'], ('/source', '/explicit', '/actual-temp'))
            self.assertEqual(load.return_value.unmount_target.call_args.kwargs['expected_identity'], args.unmount_identity)
