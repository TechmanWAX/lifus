"""Native Tk desktop application; only a separate backend child elevates."""
import json
import os
import queue

import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from .cli import ADVANCED, load_backend, unmount_warning


def list_devices():
    return load_backend('devices').list_devices()


class Application:
    def __init__(self, root):
        self.root = root
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self.busy = False
        root.title('Lifus — native Linux USB utility')
        root.geometry('820x700')
        root.minsize(700, 580)
        root.protocol('WM_DELETE_WINDOW', self.close)
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text='Lifus', font=('Sans', 22, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(frame, text='Unofficial GPL Rufus port • native Linux • no Wine').grid(row=0, column=1, columnspan=2, sticky='w')
        self.action = tk.StringVar(value='write')
        self.source = tk.StringVar()
        self.destination = tk.StringVar()
        self.device = tk.StringVar()
        self.filesystem = tk.StringVar(value='fat32')
        self.scheme = tk.StringVar(value='mbr')
        self.label = tk.StringVar(value='LIFUS')
        self.size = tk.StringVar(value='4096')
        self.passes = tk.StringVar(value='1')
        self.staging = tk.StringVar()
        self.verify = tk.BooleanVar(value=True)
        self.execute = tk.BooleanVar(value=False)
        ttk.Label(frame, text='Operation').grid(row=1, column=0, sticky='w', pady=8)
        self.action_box = ttk.Combobox(frame, textvariable=self.action, state='readonly',
            values=('write', 'backup', 'inspect', 'checksum', 'freedos', *ADVANCED))
        self.action_box.grid(row=1, column=1, columnspan=2, sticky='ew')
        for row, title, var, browse in ((2, 'Source image', self.source, self.browse_source),
                                      (3, 'Output image', self.destination, self.browse_output)):
            ttk.Label(frame, text=title).grid(row=row, column=0, sticky='w', pady=6)
            ttk.Entry(frame, textvariable=var).grid(row=row, column=1, sticky='ew')
            ttk.Button(frame, text='Browse…', command=browse).grid(row=row, column=2, padx=(8, 0))
        ttk.Label(frame, text='Target / backup disk').grid(row=4, column=0, sticky='w', pady=8)
        self.device_box = ttk.Combobox(frame, textvariable=self.device, state='readonly')
        self.device_box.grid(row=4, column=1, sticky='ew')
        ttk.Button(frame, text='Refresh', command=self.refresh).grid(row=4, column=2)
        settings = ttk.LabelFrame(frame, text='Settings', padding=8)
        settings.grid(row=5, column=0, columnspan=3, sticky='ew', pady=8)
        for col, title, var, choices in ((0, 'Filesystem', self.filesystem, ('fat32', 'ntfs', 'exfat', 'ext2', 'ext3', 'ext4')),
                                        (1, 'Partition table', self.scheme, ('mbr', 'gpt'))):
            ttk.Label(settings, text=title).grid(row=0, column=col, sticky='w')
            ttk.Combobox(settings, textvariable=var, values=choices, state='readonly', width=15).grid(row=1, column=col, padx=4)
        ttk.Label(settings, text='Volume label').grid(row=0, column=2, sticky='w')
        ttk.Entry(settings, textvariable=self.label, width=18).grid(row=1, column=2)
        ttk.Label(settings, text='Persistence MiB').grid(row=0, column=3, sticky='w')
        ttk.Entry(settings, textvariable=self.size, width=12).grid(row=1, column=3)
        ttk.Checkbutton(settings, text='Verify raw write', variable=self.verify).grid(row=2, column=0, columnspan=2, sticky='w', pady=6)
        ttk.Checkbutton(settings, text='Execute advanced operation (default: plan only)', variable=self.execute).grid(row=2, column=2, columnspan=2, sticky='w')
        ttk.Label(settings, text='Badblocks passes').grid(row=3, column=0, sticky='w')
        ttk.Combobox(settings, textvariable=self.passes, values=('1', '2', '3', '4'), state='readonly', width=8).grid(row=3, column=1)
        ttk.Label(settings, text='Staging directory').grid(row=4, column=0, sticky='w')
        ttk.Entry(settings, textvariable=self.staging).grid(row=4, column=1, columnspan=2, sticky='ew')
        ttk.Button(settings, text='Browse…', command=self.browse_staging).grid(row=4, column=3)
        ttk.Label(frame, text='FreeDOS creates a new 64 MiB output image. Backup reads the selected disk.\nDisk writes erase data. Mounted/protected disks are refused. After confirmation, sudo asks for your password.', wraplength=760).grid(row=6, column=0, columnspan=3, sticky='w', pady=5)
        self.status = tk.StringVar(value='Ready — select an image and disk')
        ttk.Label(frame, textvariable=self.status).grid(row=7, column=0, columnspan=3, sticky='w')
        self.progress = ttk.Progressbar(frame, maximum=100)
        self.progress.grid(row=8, column=0, columnspan=3, sticky='ew', pady=8)
        buttons = ttk.Frame(frame)
        buttons.grid(row=9, column=0, columnspan=3, sticky='w')
        self.start_button = ttk.Button(buttons, text='Start', command=self.start)
        self.start_button.pack(side='left')
        ttk.Button(buttons, text='Cancel', command=self.cancel).pack(side='left', padx=8)
        ttk.Button(buttons, text='Save log…', command=self.save_log).pack(side='left')
        self.log = tk.Text(frame, height=12, wrap='word', state='disabled')
        self.log.grid(row=10, column=0, columnspan=3, sticky='nsew', pady=(10, 0))
        frame.rowconfigure(10, weight=1)
        self.refresh()
        self.root.after(100, self.poll)

    def append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', str(text) + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def refresh(self):
        if self.busy:
            return
        try:
            self.devices = {}
            self.device_info = {}
            for d in list_devices():
                self.device_info[d['path']] = d
                name = f"{d['path']}  |  {d.get('model', '')}  |  {d.get('size', 0)} bytes  |  {d.get('transport', '')}"
                if d.get('mountpoints'):
                    name += '  [MOUNTED]'
                if d.get('mountpoints') and d.get('transport') == 'usb':
                    name += '  [UNMOUNT SAFETY CHECK REQUIRED]'
                elif d.get('protected'):
                    name += '  [WRITE UNAVAILABLE: ' + ', '.join(d.get('reasons') or ['protected']) + ']'
                if not os.access(d['path'], os.R_OK):
                    name += '  [BACKUP NEEDS READ PERMISSION]'
                self.devices[name] = d['path']
            self.device_box['values'] = list(self.devices)
            if self.device.get() not in self.devices:
                self.device.set(next(iter(self.devices), ''))
            self.append_log(f'{len(self.devices)} disk(s) discovered; mounted USB disks require a safety check and unmount confirmation.')
        except Exception as exc:
            self.append_log('Device discovery: ' + str(exc))

    def browse_source(self):
        from . import preferences
        path = filedialog.askopenfilename(title='Select image',
            initialdir=preferences.get_last_image_directory(),
            filetypes=[('Disk images', '*.iso *.img *.bin *.dd *.gz *.bz2 *.xz'), ('All files', '*')])
        if path:
            self.source.set(path)
            preferences.remember_image_directory(path)

    def browse_staging(self):
        path = filedialog.askdirectory(title='Staging directory (not on target disk)')
        if path:
            self.staging.set(path)

    def browse_output(self):
        path = filedialog.asksaveasfilename(title='Create new image (must not exist)', defaultextension='.img')
        if path:
            self.destination.set(path)

    def save_log(self):
        path = filedialog.asksaveasfilename(defaultextension='.log')
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as stream:
                    stream.write(self.log.get('1.0', 'end'))
            except OSError as exc:
                messagebox.showerror('Save log', str(exc))

    def start(self):
        if self.busy:
            return
        action, source = self.action.get(), self.source.get()
        target = self.devices.get(self.device.get(), '')
        destination = self.destination.get()
        execute = self.execute.get()
        destructive = action == 'write' or (action in ADVANCED and execute)
        unmount = False
        unmount_identity = None
        try:
            if action in ('write', 'inspect', 'checksum', 'windows', 'persistence') and not source:
                raise ValueError('Select a source image.')
            if action in ('write', 'backup', *ADVANCED) and not target:
                raise ValueError('Select a disk.')
            if action in ('backup', 'freedos') and not destination:
                raise ValueError('Select a new output image path.')
            options = dict(filesystem=self.filesystem.get(), partition_scheme=self.scheme.get(),
                           label=self.label.get(), persistence_mib=int(self.size.get()),
                           passes=int(self.passes.get()), staging_dir=self.staging.get() or None)
            if destructive:
                info = self.device_info.get(target, {})
                if info.get('mountpoints') and info.get('transport') == 'usb':
                    from . import privileges
                    from types import SimpleNamespace
                    paths = privileges.protected_paths(SimpleNamespace(source=source, **options))
                    plan = load_backend('unmount').plan_unmount(target, protected_paths=paths)
                    unmount_identity = plan['identity']
                    if not messagebox.askyesno('Unmount and erase disk?', unmount_warning(target, plan)):
                        self.append_log('Cancelled: unmount was not authorized.')
                        return
                    unmount = True
                elif info.get('protected') or info.get('mountpoints'):
                    raise ValueError('Disk unavailable for writing: ' + ', '.join(info.get('reasons') or ['protected or mounted']))
                typed = simpledialog.askstring('Erase disk', f'ALL DATA ON {target} WILL BE LOST.\nType the exact device path to continue:')
                if typed != target:
                    self.append_log('Cancelled: device confirmation did not match.')
                    return
        except Exception as exc:
            messagebox.showerror('Cannot start', str(exc))
            return
        verify = self.verify.get()
        self.cancel_event.clear()
        self.busy = True
        self.start_button.configure(state='disabled')
        self.progress['value'] = 0
        self.status.set('Running ' + action)
        self.append_log(f'{action}: source={source!r}, disk={target!r}, output={destination!r}')
        threading.Thread(target=self.work, args=(action, source, target, destination, options, execute, verify, unmount, unmount_identity), daemon=True).start()

    def work(self, action, source, target, destination, options, execute, verify, unmount=False, unmount_identity=None):
        def progress(done, total=None):
            if isinstance(done, dict):
                total = done.get('total', 0)
                done = done.get('completed', 0)
            self.events.put(('progress', (done, total)))
        kwargs = dict(progress=progress, cancel=self.cancel_event)
        try:
            from . import privileges
            from types import SimpleNamespace
            if os.geteuid() == 0:
                os.environ['PATH'] = privileges.SAFE_PATH
            request = SimpleNamespace(command=action, source=source, target=target,
                yes=True, confirm_device=target, allow_file=False, no_verify=not verify,
                execute=execute, unmount=unmount, unmount_identity=unmount_identity, **options)
            if os.geteuid() == 0:
                expected = privileges.prepare_unmount(request, cancel=self.cancel_event)
                if expected is not None:
                    kwargs['expected_identity'] = expected
            if privileges.needs_elevation(request):
                result = privileges.run_privileged(privileges.command_arguments(request),
                    graphical=True, **kwargs)
            elif action == 'write':
                result = load_backend('writer').write_image(source, target, verify=verify, **kwargs)
            elif action == 'backup':
                result = load_backend('writer').backup_image(target, destination, **kwargs)
            elif action == 'freedos':
                result = load_backend('freedos').create_image(destination, size_mib=64, label='FREEDOS', **kwargs)
            elif action == 'inspect':
                result = load_backend('images').inspect_image(source)
            elif action == 'checksum':
                result = {'sha256': load_backend('images').checksum(source)}
            else:
                ops = load_backend('operations')
                plan = ops.plan_operation(action, source or None, target, **options)
                result = ops.execute_operation(plan, **kwargs) if execute else plan
            self.events.put(('result', result))
        except Exception as exc:
            self.events.put(('error', str(exc)))
        finally:
            self.events.put(('done', None))

    def poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == 'progress':
                    done, total = data
                    self.progress['value'] = min(100, done * 100 / total) if total else 0
                    self.status.set(f'Processed {done:,} / {total:,}')
                elif kind == 'result':
                    self.append_log(json.dumps(data, indent=2, default=str))
                    self.status.set('Completed' if not self.cancel_event.is_set() else 'Stopped — inspect log')
                elif kind == 'error':
                    self.append_log('ERROR: ' + data)
                    self.status.set('Failed or cancelled — target may be incomplete; see log')
                elif kind == 'done':
                    self.busy = False
                    self.start_button.configure(state='normal')
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def cancel(self):
        self.cancel_event.set()
        if self.busy:
            self.status.set('Cancellation requested — waiting for safe stop')
            self.append_log('Cancel requested. Some external commands finish before cancellation is checked.')

    def close(self):
        if self.busy:
            self.cancel()
            messagebox.showinfo('Operation active', 'Cancellation requested. Wait for the operation to stop before closing.')
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    Application(root)
    root.mainloop()
