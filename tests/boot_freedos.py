"""Optional real BIOS boot check; requires QEMU and SeaBIOS, no root.

Usage: python3 tests/boot_freedos.py IMAGE OUTPUT_DIR QEMU_RESOURCE_ROOT
QEMU_RESOURCE_ROOT is /usr for installed QEMU, or unpacked-root/usr.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def main():
    image, output, root = map(lambda p: Path(p).resolve(), sys.argv[1:])
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = str(root/'lib/x86_64-linux-gnu')
    with tempfile.TemporaryDirectory(prefix='lifus-qemu-') as td:
        address = str(Path(td)/'qmp')
        proc = subprocess.Popen([str(root/'bin/qemu-system-x86_64'), '-machine', 'pc',
            '-accel', 'tcg', '-m', '64', '-L', str(root/'share/qemu'),
            '-bios', str(root/'share/seabios/bios.bin'), '-nodefaults',
            '-device', 'VGA,romfile='+str(root/'share/seabios/vgabios-stdvga.bin'),
            '-display', 'none', '-nic', 'none', '-drive', f'file={image},format=raw,snapshot=on',
            '-qmp', f'unix:{address},server=on,wait=off'], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            deadline = time.monotonic()+20
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(proc.communicate()[1].decode())
                if time.monotonic() > deadline:
                    raise TimeoutError('QMP startup timeout')
                try:
                    conn.connect(address)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(.1)
            conn.settimeout(10)
            stream = conn.makefile('rwb', buffering=0)
            print(stream.readline().decode().strip())
            def command(name, arguments=None):
                stream.write((json.dumps({'execute':name,'arguments':arguments or {}})+'\n').encode())
                while True:
                    response = json.loads(stream.readline())
                    if 'error' in response:
                        raise RuntimeError(response)
                    if 'return' in response:
                        return response['return']
            command('qmp_capabilities')
            # Bounded boot wait inside this foreground validation, not a daemon.
            time.sleep(8)
            screenshot = output/'freedos-boot.ppm'
            command('screendump', {'filename':str(screenshot)})
            vga = output/'freedos-vga.bin'
            command('human-monitor-command', {'command-line': f'pmemsave 0xb8000 4000 "{vga}"'})
            raw = vga.read_bytes()
            text = '\n'.join(bytes(raw[row*160:row*160+160:2]).decode('cp437') for row in range(25))
            (output/'freedos-boot.txt').write_text(text)
            print(text)
            if 'FreeDOS - Lifus native Linux build' not in text or 'C:\\>' not in text:
                raise AssertionError('FreeDOS shell and AUTOEXEC banner not reached')
            print('PASS: actual SeaBIOS -> Rufus MBR -> FreeDOS PBR -> KERNEL.SYS -> COMMAND.COM')
            command('quit')
        finally:
            conn.close()
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)


if __name__ == '__main__':
    main()
