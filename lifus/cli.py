"""Command line interface; confirmed disk writes use interactive sudo."""
import argparse
import importlib
import json
import os
import shutil
import sys

ADVANCED = ('format', 'windows', 'persistence', 'badblocks')
TOOLS = ('lsblk', 'findmnt', 'blockdev', 'sfdisk', 'wipefs', 'mkfs.vfat',
         'mkfs.fat', 'mkfs.exfat', 'mkfs.ext2', 'mkfs.ext3', 'mkfs.ntfs', 'mkfs.ext4', 'mount', 'umount', '7z', 'wimlib-imagex', 'badblocks', 'parted', 'mcopy')


def load_backend(name):
    return importlib.import_module('.' + name, __package__)


def parse_unmount_identity(value):
    """Bounded JSON data only; this snapshot never replaces live safety checks."""
    if len(value) > 65536:
        raise ValueError('Unmount identity exceeds size limit')
    try:
        def reject_constant(value):
            raise ValueError('Non-finite JSON number')
        identity = json.loads(value, parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError('Invalid unmount identity JSON') from exc
    if not isinstance(identity, dict) or not identity:
        raise ValueError('Unmount identity must be a nonempty JSON object')
    return identity


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)

    def parse_args(self, args=None, namespace=None):
        result = super().parse_args(args, namespace)
        if getattr(result, 'unmount_identity', None) is not None and not getattr(result, 'unmount', False):
            self.error('--unmount-identity requires --unmount')
        return result


def build_parser():
    p = Parser(prog='lifus', description='Lifus: unofficial native Linux Rufus port (GPL).')
    p.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    subs = p.add_subparsers(dest='command', required=True)
    for name in ('devices', 'inspect', 'checksum', 'write', 'backup', *ADVANCED, 'gui', 'doctor', 'freedos', 'raw-to-vhd', 'vhd-to-raw', 'unattend'):
        s = subs.add_parser(name)
        s.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
        if name in ('raw-to-vhd', 'vhd-to-raw'):
            s.add_argument('source')
            s.add_argument('destination')
        if name == 'unattend':
            s.add_argument('destination')
            for flag in ('bypass-tpm', 'bypass-secure-boot', 'bypass-ram', 'disable-telemetry', 'skip-online-account'):
                s.add_argument('--' + flag, action='store_true', default=False)
            s.add_argument('--local-account')
        if name == 'windows':
            s.add_argument('--unattend', help='Explicit existing unattended setup XML')
        if name == 'freedos':
            s.add_argument('destination')
            s.add_argument('--size-mib', type=int, default=64)
            s.add_argument('--label', default='FREEDOS')
        if name in ('inspect', 'checksum'):
            s.add_argument('source')
        if name == 'checksum':
            s.add_argument('--algorithm', choices=('sha256', 'sha512', 'sha1', 'md5'), default='sha256')
        if name in ('write', 'backup', 'windows', 'persistence'):
            s.add_argument('source')
        if name in ('write', 'backup', *ADVANCED):
            s.add_argument('target')
        if name in ('write', *ADVANCED):
            s.add_argument('--unmount', action='store_true', help='Authorize safe unmount of removable USB before confirmed execution')
            s.add_argument('--unmount-identity', type=parse_unmount_identity, help=argparse.SUPPRESS)
            s.add_argument('--yes', action='store_true')
            s.add_argument('--confirm-device')
        if name == 'write':
            s.add_argument('--allow-file', action='store_true', help='Permit existing regular-file target for tests')
            s.add_argument('--no-verify', action='store_true')
        if name in ADVANCED:
            s.add_argument('--execute', action='store_true', help='Execute; otherwise only print plan')
            s.add_argument('--filesystem', choices=('fat32', 'ntfs', 'exfat', 'ext2', 'ext3', 'ext4'), default='fat32')
            s.add_argument('--staging-dir', help='Staging directory on a disk other than the target')
            s.add_argument('--passes', type=int, choices=range(1, 5), default=1)
            s.add_argument('--partition-scheme', choices=('mbr', 'gpt'), default='mbr')
            s.add_argument('--label', default='LIFUS')
            s.add_argument('--persistence-mib', type=int, default=4096)
    return p


def require_confirmation(args):
    if not args.yes or args.confirm_device != args.target:
        raise ValueError('Destructive operation requires --yes AND --confirm-device exactly matching target ' + args.target)


def unmount_warning(target, plan):
    mounts = '\n'.join('  ' + str(path) for path in plan['mountpoints'])
    return (f"Unmount filesystems on {target}:\n{mounts}\n"
            f"{plan['warning']}\nClose open files before continuing.\n"
            f"ALL DATA ON {target} WILL BE LOST.")


def doctor():
    try:
        import tkinter
        tk = str(tkinter.TkVersion)
    except ImportError:
        tk = None
    return {'python': sys.executable, 'root': os.geteuid() == 0, 'tk': tk,
            'display': bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')),
            'tools': {name: shutil.which(name) for name in TOOLS},
            'notice': 'Confirmed disk writes use sudo with a password prompt. Advanced operations default to plan-only.'}


def dispatch(args):
    command = args.command
    backend_kwargs = {}
    if getattr(args, 'unmount_identity', None) is not None and not getattr(args, 'unmount', False):
        raise ValueError('--unmount-identity requires --unmount')
    if getattr(args, 'unmount', False) and not (command == 'write' or (command in ADVANCED and args.execute)):
        raise ValueError('--unmount requires write or an advanced --execute operation')
    if command == 'write' or (command in ADVANCED and args.execute):
        require_confirmation(args)
        from . import privileges
        if args.unmount:
            plan = load_backend('unmount').plan_unmount(args.target, protected_paths=privileges.protected_paths(args))
            original = getattr(args, 'unmount_identity', None)
            if original is not None and original != plan['identity']:
                raise ValueError('Target identity differs from confirmed preflight')
            print(unmount_warning(args.target, plan), file=sys.stderr, flush=True)
            if original is None:
                args.unmount_identity = plan['identity']
        if privileges.needs_elevation(args):
            return privileges.run_privileged(privileges.command_arguments(args))
        if os.geteuid() == 0:
            os.environ['PATH'] = privileges.SAFE_PATH
        expected = privileges.prepare_unmount(args)
        if expected is not None:
            backend_kwargs['expected_identity'] = expected
    if command in ('raw-to-vhd', 'vhd-to-raw'):
        return getattr(load_backend('containers'), command.replace('-', '_'))(args.source, args.destination)
    if command == 'unattend':
        return load_backend('windows_config').write_unattend(args.destination,
            bypass_tpm=args.bypass_tpm, bypass_secure_boot=args.bypass_secure_boot,
            bypass_ram=args.bypass_ram, local_account=args.local_account,
            disable_telemetry=args.disable_telemetry, skip_online_account=args.skip_online_account)
    if command == 'freedos':
        return load_backend('freedos').create_image(args.destination, size_mib=args.size_mib, label=args.label)
    if command == 'devices':
        return load_backend('devices').list_devices()
    if command == 'inspect':
        return load_backend('images').inspect_image(args.source)
    if command == 'checksum':
        return {'algorithm': args.algorithm, 'checksum': load_backend('images').checksum(args.source, algorithm=args.algorithm)}
    if command == 'write':
        require_confirmation(args)
        return load_backend('writer').write_image(args.source, args.target, allow_file=args.allow_file, verify=not args.no_verify, **backend_kwargs)
    if command == 'backup':
        return load_backend('writer').backup_image(args.source, args.target)
    if command in ADVANCED:
        if args.execute:
            require_confirmation(args)
        operations = load_backend('operations')
        plan = operations.plan_operation(command, getattr(args, 'source', None), args.target,
            filesystem=args.filesystem, partition_scheme=args.partition_scheme,
            label=args.label, persistence_mib=args.persistence_mib, passes=args.passes, staging_dir=args.staging_dir,
            **({'unattend': args.unattend} if getattr(args, 'unattend', None) else {}))
        return operations.execute_operation(plan, **backend_kwargs) if args.execute else plan
    if command == 'doctor':
        return doctor()
    if command == 'gui':
        load_backend('gui').main()
        return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    structured = '--json' in argv
    try:
        args = build_parser().parse_args(argv)
        result = dispatch(args)
        if result is not None:
            print(json.dumps(result, indent=2, default=str))
        return 0
    except KeyboardInterrupt:
        error, code = 'Interrupted; target may be incomplete.', 130
    except Exception as exc:
        error, code = str(exc), 1
    if structured:
        print(json.dumps({'error': error}))
    else:
        print('lifus: ' + error, file=sys.stderr)
    return code
