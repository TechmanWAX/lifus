"""Explicit sudo boundary. No passwords are handled by this process.

The installed worker lives in /usr/share/lifus and askpass in /usr/libexec;
source checkouts use their packaging directory. The user authorizes that code
with sudo; installed root-owned code is recommended for normal use.
"""
import os
from pathlib import Path
import stat
import json
import selectors
import signal
import subprocess
import sys
import threading
import tempfile

SAFE_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
COMMANDS = ('write', 'format', 'windows', 'persistence', 'badblocks')


def needs_elevation(args):
    if os.geteuid() == 0:
        return False
    if args.command == 'write':
        if args.allow_file:
            try:
                if stat.S_ISREG(os.lstat(args.target).st_mode):
                    return False
            except OSError:
                pass
        return True
    return args.command in COMMANDS and args.execute


def command_arguments(args):
    """Reconstruct only known backend actions; never forward arbitrary argv."""
    from .cli import require_confirmation, parse_unmount_identity
    if args.command not in COMMANDS:
        raise ValueError('Unsupported privileged command')
    require_confirmation(args)
    result = [args.command, '--yes', '--confirm-device=' + args.confirm_device]
    identity = getattr(args, 'unmount_identity', None)
    if getattr(args, 'unmount', False):
        if not isinstance(identity, dict) or not identity:
            raise ValueError('--unmount requires the confirmed preflight identity')
        encoded = json.dumps(identity, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        parse_unmount_identity(encoded)
        result += ['--unmount', '--unmount-identity=' + encoded]
    elif identity is not None:
        raise ValueError('--unmount-identity requires --unmount')
    positional = []
    if args.command in ('write', 'windows', 'persistence'):
        positional.append(args.source)
    positional.append(args.target)
    if args.command == 'write':
        if args.allow_file:
            result.append('--allow-file')
        if args.no_verify:
            result.append('--no-verify')
    else:
        if not args.execute:
            raise ValueError('Plan-only operations must not elevate')
        result.append('--execute')
        for name in ('filesystem', 'partition_scheme', 'label', 'persistence_mib', 'passes', 'staging_dir', 'unattend'):
            value = getattr(args, name, None)
            if value is not None:
                result.append('--' + name.replace('_', '-') + '=' + str(value))
    if getattr(args, 'json', False):
        result.append('--json')
    return result + ['--'] + positional


def protected_paths(args):
    # Both writer decompression and operation TemporaryFile use this directory,
    # even when an explicit operation staging directory was supplied.
    return tuple(value for name in ('source', 'unattend', 'staging_dir')
                 if (value := getattr(args, name, None))) + (tempfile.gettempdir(),)


def prepare_unmount(args, *, cancel=None):
    """Revalidate independently against the ORIGINAL confirmed preflight."""
    from .cli import load_backend, require_confirmation
    if not getattr(args, 'unmount', False):
        return
    require_confirmation(args)
    if args.command not in COMMANDS or (args.command != 'write' and not args.execute):
        raise ValueError('--unmount requires a destructive execution')
    if cancel is not None and cancel.is_set():
        raise InterruptedError('Operation cancelled')
    expected = getattr(args, 'unmount_identity', None)
    if not isinstance(expected, dict) or not expected:
        raise ValueError('--unmount requires the confirmed preflight identity')
    backend = load_backend('unmount')
    paths = protected_paths(args)
    plan = backend.plan_unmount(args.target, protected_paths=paths)
    if plan['identity'] != expected:
        raise ValueError('Target identity differs from confirmed preflight')
    backend.unmount_target(args.target, protected_paths=paths,
                           expected_identity=expected, cancel=cancel)
    return expected


def helper_path(name):
    base = Path(__file__).resolve().parent.parent
    if (base / 'packaging' / name).is_file():
        path = base / 'packaging' / name
    elif name == 'lifus-askpass':
        # Derive the trusted install prefix from this module, not from PATH or
        # caller-controlled environment. Also permits extracted-package tests.
        if base.name != 'lifus' or base.parent.name != 'share':
            raise RuntimeError('Unrecognized Lifus install layout')
        path = base.parent.parent / 'libexec' / name
    else:
        path = base / name
    if not path.is_file():
        raise RuntimeError('Missing Lifus privilege helper: ' + str(path))
    return str(path)


def run_privileged(argv, *, graphical=False, progress=None, cancel=None):
    from .cli import build_parser
    argv = command_arguments(build_parser().parse_args(argv))
    if cancel is not None and cancel.is_set():
        raise InterruptedError('Operation cancelled')
    command = ['/usr/bin/sudo', '-k']
    env = dict(os.environ, PATH=SAFE_PATH)
    if graphical:
        command.append('-A')
        env['SUDO_ASKPASS'] = helper_path('lifus-askpass')
    command += ['--', '/usr/bin/python3', '-I', helper_path('lifus-privileged.py'), *argv]
    # sudo authenticates through /dev/tty or its own askpass pipe, never ours.
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    pending = b''
    errors = b''
    result = None
    received = False
    failure = None
    interrupted = False
    signalled = False

    def message(line):
        nonlocal result, received, failure
        try:
            record = json.loads(line)
            kind, data = record['type'], record['data']
            if kind == 'progress':
                if progress:
                    progress(*data)
            elif kind == 'result' and not received:
                result, received = data, True
            elif kind == 'error':
                failure = str(data)
            else:
                failure = 'Invalid privileged worker response'
        except Exception:
            failure = 'Invalid privileged worker response'

    with selectors.DefaultSelector() as selector:
        for stream in (proc.stdout, proc.stderr):
            selector.register(stream, selectors.EVENT_READ)
        try:
            while selector.get_map() or proc.poll() is None:
                try:
                    if (interrupted or (cancel is not None and cancel.is_set())) and not signalled:
                        # sudo forwards SIGTERM to its command; the worker sets an
                        # Event rather than exiting in the middle of a write.
                        if proc.poll() is None:
                            proc.send_signal(signal.SIGTERM)
                        signalled = True
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if key.fileobj is proc.stderr:
                            errors = (errors + chunk)[-8192:]
                        else:
                            pending += chunk
                            while b'\n' in pending:
                                line, pending = pending.split(b'\n', 1)
                                message(line)
                            if len(pending) > 1048576:
                                failure = 'Oversized privileged worker response'
                                pending = b''
                except KeyboardInterrupt:
                    interrupted = True
            code = proc.wait()
        finally:
            proc.stdout.close()
            proc.stderr.close()
    if interrupted or signalled or (cancel is not None and cancel.is_set()):
        raise InterruptedError('Operation cancelled; target may be incomplete')
    if pending:
        message(pending)
    if failure:
        raise RuntimeError(failure)
    if code != 0 or not received:
        detail = errors.decode('utf-8', errors='replace').strip()
        raise RuntimeError(f'sudo privileged command failed (exit {code}): ' + (detail or 'No successful worker result; authentication may have been cancelled.'))
    return result


def worker_main(argv=None):
    """JSON-lines protocol: progress [done,total], result object, error string."""
    from .cli import build_parser, load_backend
    cancel = threading.Event()
    previous = {}

    def emit(kind, data):
        print(json.dumps({'type': kind, 'data': data}, default=str), flush=True)

    def progress(done, total=None):
        if isinstance(done, dict):
            done, total = done.get('completed', 0), done.get('total', 0)
        emit('progress', [done, total])

    try:
        # Parse and reconstruct again at the privilege boundary. GUI, arbitrary
        # commands and plan-only execution are never valid worker requests.
        args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
        command_arguments(args)
        if os.geteuid() != 0:
            raise PermissionError('Privileged worker requires sudo/root')
        os.environ['PATH'] = SAFE_PATH
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: cancel.set())
        expected = prepare_unmount(args, cancel=cancel)
        kwargs = dict(progress=progress, cancel=cancel)
        if expected is not None:
            kwargs['expected_identity'] = expected
        if args.command == 'write':
            result = load_backend('writer').write_image(args.source, args.target,
                allow_file=args.allow_file, verify=not args.no_verify, **kwargs)
        else:
            ops = load_backend('operations')
            options = {name: getattr(args, name) for name in ('filesystem', 'partition_scheme', 'label', 'persistence_mib', 'passes', 'staging_dir')}
            if getattr(args, 'unattend', None):
                options['unattend'] = args.unattend
            plan = ops.plan_operation(args.command, getattr(args, 'source', None), args.target, **options)
            if cancel.is_set():
                raise InterruptedError('Operation cancelled')
            result = ops.execute_operation(plan, **kwargs)
        if cancel.is_set():
            raise InterruptedError('Operation cancelled; target may be incomplete')
        emit('result', result)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        emit('error', str(exc) or 'Operation interrupted')
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
