"""Unprivileged PySide6 desktop; only the isolated backend child elevates."""
import json
import os
import queue
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout, QWidget)

from .cli import ADVANCED, load_backend, unmount_warning


def list_devices():
    return load_backend('devices').list_devices()


class Window(QWidget):
    def closeEvent(self, event):
        if self.controller.busy:
            self.controller.cancel()
            QMessageBox.information(self, 'Операция выполняется',
                'Запрошена отмена. Дождитесь безопасного завершения операции.')
            event.ignore()
        else:
            event.accept()


class Application:
    def __init__(self, root=None):
        self.root = root if root is not None else Window()
        self.root.controller = self
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self.busy = False
        self.previewing = False
        self.root.setWindowTitle('Lifus — создание загрузочного USB')
        self.root.resize(560, 650)
        outer = QVBoxLayout(self.root)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(10)
        title = QLabel('Lifus')
        title.setStyleSheet('font-size: 24px; font-weight: bold;')
        outer.addWidget(title)
        subtitle = QLabel('Загрузочные USB-носители • Linux • неофициальный порт Rufus')
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)
        self.controls = QWidget()
        layout = QVBoxLayout(self.controls)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.controls)

        drive = QGroupBox('Свойства диска')
        grid = QGridLayout(drive)
        layout.addWidget(drive)
        grid.addWidget(QLabel('Устройство'), 0, 0, 1, 2)
        self.device_box = QComboBox()
        self.device_box.setMinimumWidth(240)
        self.device_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        grid.addWidget(self.device_box, 1, 0)
        refresh = QPushButton('Обновить')
        refresh.clicked.connect(self.refresh)
        grid.addWidget(refresh, 1, 1)
        grid.addWidget(QLabel('Метод загрузки / операция'), 2, 0, 1, 2)
        self.action = QComboBox()
        self.action_box = self.action
        actions = [('Установочный носитель Windows (ISO)', 'windows'),
                   ('Побайтовая запись образа (RAW / DD)', 'write'),
                   ('Форматирование (без загрузки)', 'format'),
                   ('FreeDOS — создать файл образа', 'freedos'),
                   ('Резервная копия диска в файл', 'backup'),
                   ('Информация об образе', 'inspect'),
                   ('Контрольная сумма SHA-256', 'checksum'),
                   ('Linux persistence (расширенные)', 'persistence'),
                   ('Проверка блоков — стирает диск', 'badblocks')]
        for text, action in actions:
            self.action.addItem(text, action)
        for action in ADVANCED:
            if self.action.findData(action) < 0:
                self.action.addItem(action, action)
        grid.addWidget(self.action, 3, 0, 1, 2)
        self.source = QLineEdit()
        self.source.setPlaceholderText('Выберите ISO или образ диска')
        grid.addWidget(self.source, 4, 0)
        self.source_button = QPushButton('ВЫБРАТЬ…')
        self.source_button.clicked.connect(self.browse_source)
        grid.addWidget(self.source_button, 4, 1)
        grid.addWidget(QLabel('Схема раздела'), 5, 0)
        grid.addWidget(QLabel('Целевая система'), 5, 1)
        self.scheme = QComboBox()
        self.scheme.addItems(['gpt', 'mbr'])
        grid.addWidget(self.scheme, 6, 0)
        self.target_system = QLabel()
        self.target_system.setWordWrap(True)
        grid.addWidget(self.target_system, 6, 1)
        self.mode_hint = QLabel()
        self.mode_hint.setWordWrap(True)
        grid.addWidget(self.mode_hint, 7, 0, 1, 2)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)

        formatting = QGroupBox('Параметры форматирования')
        form = QFormLayout(formatting)
        layout.addWidget(formatting)
        self.label = QLineEdit('LIFUS')
        form.addRow('Метка тома', self.label)
        self.filesystem = QComboBox()
        self.filesystem.addItems(['fat32', 'ntfs', 'exfat', 'ext2', 'ext3', 'ext4'])
        form.addRow('Файловая система', self.filesystem)
        self.verify = QCheckBox('Проверять запись RAW чтением с носителя')
        self.verify.setChecked(True)
        form.addRow(self.verify)

        self.advanced_toggle = QCheckBox('Расширенные параметры и файл результата')
        layout.addWidget(self.advanced_toggle)
        self.advanced = QGroupBox('Расширенные параметры')
        advanced = QFormLayout(self.advanced)
        self.destination = QLineEdit()
        output = QHBoxLayout()
        output.addWidget(self.destination)
        button = QPushButton('Сохранить…')
        button.clicked.connect(self.browse_output)
        output.addWidget(button)
        advanced.addRow('Файл результата', output)
        self.size = QLineEdit('4096')
        advanced.addRow('Persistence, МиБ', self.size)
        self.passes = QComboBox()
        self.passes.addItems(['1', '2', '3', '4'])
        advanced.addRow('Проходы проверки', self.passes)
        self.staging = QLineEdit()
        staging = QHBoxLayout()
        staging.addWidget(self.staging)
        button = QPushButton('Каталог…')
        button.clicked.connect(self.browse_staging)
        staging.addWidget(button)
        advanced.addRow('Временные файлы', staging)
        layout.addWidget(self.advanced)
        self.advanced.hide()
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)

        status_group = QGroupBox('Состояние')
        status_layout = QVBoxLayout(status_group)
        outer.addWidget(status_group)
        self.status = QLabel('Готово — выберите образ и устройство')
        self.status.setWordWrap(True)
        status_layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        status_layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        outer.addLayout(buttons)
        self.preview_button = QPushButton('Просмотр плана')
        self.preview_button.clicked.connect(self.preview)
        buttons.addWidget(self.preview_button)
        buttons.addStretch()
        self.start_button = QPushButton('СТАРТ')
        self.start_button.clicked.connect(self.start)
        buttons.addWidget(self.start_button)
        self.cancel_button = QPushButton('ОТМЕНА')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        buttons.addWidget(self.cancel_button)
        log_buttons = QHBoxLayout()
        outer.addLayout(log_buttons)
        log_toggle = QCheckBox('Журнал')
        log_buttons.addWidget(log_toggle)
        log_buttons.addStretch()
        save = QPushButton('Сохранить журнал…')
        save.clicked.connect(self.save_log)
        log_buttons.addWidget(save)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(110)
        outer.addWidget(self.log)
        self.log.hide()
        log_toggle.toggled.connect(self.log.setVisible)
        self.action.currentIndexChanged.connect(self.update_mode)
        self.scheme.currentTextChanged.connect(self.update_mode)
        self.update_mode()
        self.refresh()
        self.timer = QTimer(self.root)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)

    def update_mode(self):
        action = self.action.currentData()
        layout = action in ('windows', 'format')
        self.scheme.setEnabled(layout)
        self.filesystem.setEnabled(layout)
        self.label.setEnabled(layout)
        self.verify.setEnabled(action == 'write')
        self.source.setEnabled(action in ('write', 'windows', 'inspect', 'checksum', 'persistence'))
        self.source_button.setEnabled(self.source.isEnabled())
        self.preview_button.setEnabled(action in ADVANCED and not self.busy)
        self.size.setEnabled(action == 'persistence')
        self.passes.setEnabled(action == 'badblocks')
        self.destination.setEnabled(action in ('backup', 'freedos'))
        self.staging.setEnabled(action in ADVANCED)
        if action in ('backup', 'freedos'):
            self.advanced_toggle.setChecked(True)
        # Only FAT32 and NTFS are supported for Windows preparation.
        for i in range(self.filesystem.count()):
            self.filesystem.model().item(i).setEnabled(action != 'windows' or i < 2)
        if action == 'windows' and self.filesystem.currentText() not in ('fat32', 'ntfs'):
            self.filesystem.setCurrentText('fat32')
        if action == 'write':
            self.target_system.setText('Из исходного образа')
            self.mode_hint.setText('RAW / DD: схема разделов, файловая система и метка копируются из образа. Настройки форматирования не применяются. Для Windows ISO выберите установочный носитель Windows.')
        elif layout:
            self.target_system.setText(('UEFI (без CSM)' if self.scheme.currentText() == 'gpt'
                else 'UEFI (поддержка MBR прошивкой)') if action == 'windows' else 'Без загрузчика')
            self.mode_hint.setText('СТАРТ стирает данные после подтверждения. Просмотр плана ничего не записывает.')
        else:
            self.target_system.setText('Не применяется')
            self.mode_hint.setText('FreeDOS создаёт новый образ 64 МиБ. Backup читает диск. Проверка блоков и persistence изменяют диск.')

    def append_log(self, text):
        self.log.appendPlainText(str(text))

    def refresh(self):
        if self.busy:
            return
        previous = self.device_box.currentData()
        try:
            found = list_devices()
            self.devices = {}
            self.device_info = {}
            self.device_box.clear()
            for d in found:
                self.device_info[d['path']] = d
                name = f"{d['path']} | {d.get('model', '')} | {d.get('size', 0) / (1024**3):.1f} ГиБ | {d.get('transport', '')}"
                if d.get('mountpoints') and d.get('transport') == 'usb':
                    name += ' [требуется размонтирование]'
                elif d.get('protected') or d.get('mountpoints'):
                    name += ' [запись запрещена]'
                self.devices[name] = d['path']
                self.device_box.addItem(name, d['path'])
            index = self.device_box.findData(previous)
            if index >= 0:
                self.device_box.setCurrentIndex(index)
            self.append_log(f'Найдено дисков: {len(self.devices)}. Перед записью USB потребуется проверка безопасности.')
        except Exception as exc:
            self.append_log('Поиск устройств: ' + str(exc))

    def browse_source(self):
        from . import preferences
        path, _ = QFileDialog.getOpenFileName(self.root, 'Выберите образ',
            preferences.get_last_image_directory(),
            'Образы (*.iso *.img *.bin *.dd *.gz *.bz2 *.xz);;Все файлы (*)')
        if path:
            self.source.setText(path)
            preferences.remember_image_directory(path)

    def browse_staging(self):
        path = QFileDialog.getExistingDirectory(self.root, 'Временный каталог (не на целевом диске)')
        if path:
            self.staging.setText(path)

    def browse_output(self):
        path, _ = QFileDialog.getSaveFileName(self.root, 'Новый файл образа (не должен существовать)', '', 'Образы (*.img)')
        if path:
            self.destination.setText(path)

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(self.root, 'Сохранить журнал', '', 'Журнал (*.log)')
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as stream:
                    stream.write(self.log.toPlainText())
            except OSError as exc:
                QMessageBox.critical(self.root, 'Журнал', str(exc))

    def preview(self):
        self.start(preview=True)

    def start(self, checked=False, *, preview=False):
        if self.busy:
            return
        action, source = self.action.currentData(), self.source.text()
        target = self.device_box.currentData() or ''
        destination = self.destination.text()
        if preview and action not in ADVANCED:
            return
        execute = not preview
        destructive = not preview and (action == 'write' or action in ADVANCED)
        unmount = False
        unmount_identity = None
        try:
            if action in ('write', 'inspect', 'checksum', 'windows', 'persistence') and not source:
                raise ValueError('Выберите исходный образ.')
            if action in ('write', 'backup', *ADVANCED) and not target:
                raise ValueError('Выберите диск.')
            if action in ('backup', 'freedos') and not destination:
                raise ValueError('Выберите путь нового файла образа.')
            options = dict(filesystem=self.filesystem.currentText(), partition_scheme=self.scheme.currentText(),
                           label=self.label.text(), persistence_mib=int(self.size.text()) if action == 'persistence' else 4096,
                           passes=int(self.passes.currentText()), staging_dir=self.staging.text() or None)
            if destructive:
                info = self.device_info.get(target, {})
                if info.get('mountpoints') and info.get('transport') == 'usb':
                    from . import privileges
                    from types import SimpleNamespace
                    paths = privileges.protected_paths(SimpleNamespace(source=source, **options))
                    plan = load_backend('unmount').plan_unmount(target, protected_paths=paths)
                    unmount_identity = plan['identity']
                    warning = 'Размонтировать тома и стереть диск?\n\n' + unmount_warning(target, plan)
                    if QMessageBox.question(self.root, 'Размонтирование и удаление данных', warning,
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                        self.append_log('Отменено: размонтирование не разрешено.')
                        return
                    unmount = True
                elif info.get('protected') or info.get('mountpoints'):
                    raise ValueError('Запись запрещена: ' + ', '.join(info.get('reasons') or ['защищённый или смонтированный диск']))
                typed, accepted = QInputDialog.getText(self.root, 'Удаление данных',
                    f'ВСЕ ДАННЫЕ НА {target} БУДУТ УДАЛЕНЫ.\nВведите точный путь устройства:')
                if not accepted or typed != target:
                    self.append_log('Отменено: путь устройства не подтверждён.')
                    return
        except Exception as exc:
            QMessageBox.critical(self.root, 'Невозможно начать', str(exc))
            return
        verify = self.verify.isChecked()
        self.cancel_event.clear()
        self.busy = True
        self.previewing = preview
        self.controls.setEnabled(False)
        self.start_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.show_phase({'phase': 'prepare'})
        self.append_log(f'{"ПЛАН" if preview else "ЗАПУСК"} {action}: source={source!r}, disk={target!r}, output={destination!r}')
        threading.Thread(target=self.work, args=(action, source, target, destination, options, execute, verify, unmount, unmount_identity), daemon=True).start()

    def work(self, action, source, target, destination, options, execute, verify, unmount=False, unmount_identity=None):
        def progress(done, total=None):
            if isinstance(done, dict):
                self.events.put(('phase', done.copy()))
                # One queue message owns both phase and count; a timer may run
                # between puts, so never split an update into reset + value.
                return
            if total:
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

    def show_phase(self, data):
        phase = str(data.get('phase', 'write'))
        # Periodic durability waits are not the final synchronization stage.
        if phase == 'flush':
            return
        if phase == 'prepare':
            self.transfer_complete = False
        elif phase == 'sync':
            self.transfer_complete = True
        self.progress_phase = {
            'prepare': 'Подготовка образа / устройства',
            'write': 'Запись',
            'sync': 'Синхронизация — не извлекайте устройство',
            'verify': 'Проверка записанных данных',
            'read': 'Чтение',
            'finalizing': 'Завершение операции',
        }.get(phase, phase)
        measurable = data.get('total') and phase not in ('prepare', 'sync', 'finalizing')
        complete = getattr(self, 'transfer_complete', False)
        self.progress.setRange(0, 100 if measurable or complete else 0)
        if complete:
            self.progress.setValue(100)
        if measurable:
            done, total = data.get('completed', 0), data['total']
            value = min(100, int(done * 100 / total))
            self.progress.setValue(100 if complete else value)
            if phase in ('write', 'read') and done >= total:
                self.transfer_complete = True
            self.status.setText(f'{self.progress_phase}: {done:,} / {total:,} байт')
        else:
            self.status.setText(self.progress_phase)

    def poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == 'phase':
                    self.show_phase(data)
                elif kind == 'progress':
                    done, total = data
                    self.progress.setRange(0, 100)
                    # 100% means transfer complete, not successful verification.
                    complete = getattr(self, 'transfer_complete', False)
                    self.progress.setValue(100 if complete else min(100, int(done * 100 / total)) if total else 0)
                    if total and done >= total:
                        self.transfer_complete = True
                    phase = getattr(self, 'progress_phase', 'Обработано')
                    self.status.setText(f'{phase}: {done:,} / {total:,} байт')
                elif kind == 'result':
                    self.progress.setRange(0, 100)
                    self.progress.setValue(100 if not self.cancel_event.is_set() else 0)
                    if self.previewing and 'staging_bytes' in data:
                        self.append_log(f"Временные данные: {data['staging_bytes']} байт.")
                        if data.get('action') == 'windows' and data.get('options', {}).get('filesystem') == 'fat32':
                            self.append_log('Прямая запись FAT32 без образа раздела. Временные файлы нужны только для разделения большого install.wim. При отмене или ошибке носитель останется неполным.')
                    self.append_log(json.dumps(data, indent=2, ensure_ascii=False, default=str))
                    self.status.setText('Остановлено — проверьте журнал' if self.cancel_event.is_set() else
                        ('План готов — запись не выполнялась' if self.previewing else 'Готово'))
                    if self.previewing:
                        self.log.show()
                elif kind == 'error':
                    self.progress.setRange(0, 100)
                    self.progress.setValue(0)
                    self.append_log('ОШИБКА: ' + data)
                    self.log.show()
                    self.status.setText('Ошибка или отмена — носитель может быть записан не полностью; см. журнал')
                elif kind == 'done':
                    self.progress.setRange(0, 100)
                    self.busy = False
                    self.controls.setEnabled(True)
                    self.start_button.setEnabled(True)
                    self.cancel_button.setEnabled(False)
                    self.update_mode()
        except queue.Empty:
            pass

    def cancel(self):
        self.cancel_event.set()
        if self.busy:
            self.status.setText('Запрошена отмена — ожидайте безопасной остановки')
            self.append_log('Отмена запрошена. Некоторые внешние команды должны завершить текущий шаг.')

    def close(self):
        self.root.close()


def main():
    qt = QApplication.instance() or QApplication(sys.argv[:1])
    qt.setApplicationName('Lifus')
    app = Application()
    app.root.show()
    return qt.exec()
