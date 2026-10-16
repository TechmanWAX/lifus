"""Native XML subset of Rufus src/wue.c (GPL-3.0-or-later).

Adapted from CreateUnattendXml, Copyright © 2022–2026 Pete Batard.
See docs/WINDOWS_CONFIG.md for attribution, scope and safety caveats.
"""
import os
from pathlib import Path
import unicodedata
import xml.etree.ElementTree as ET

_RESERVED_NAMES = {name.casefold() for name in (
    'Administrator', 'Järjestelmänvalvoja', 'Administrateur', 'Rendszergazda',
    'Administrador', 'Администратор', 'Administratör', 'Guest', 'DefaultAccount',
    'WDAGUtilityAccount', 'HelpAssistant', 'KRBTGT', 'Local', 'NONE', 'SYSTEM')}


def _validate_account(name):
    # Reject rather than silently sanitize. The allowlist also protects the
    # quoted Windows net.exe argument; no user text ever reaches a host shell.
    if (not isinstance(name, str) or not name or name != name.strip()
            or name.casefold() in _RESERVED_NAMES
            or any(not (unicodedata.category(c)[0] in 'LN' or c in " '-_") for c in name)
            or len(name.encode('utf-16-le')) > 40):
        raise ValueError('local_account must be a safe, non-reserved name of 1–20 UTF-16 units')

UNATTEND = 'urn:schemas-microsoft-com:unattend'


def _component(root, pass_name, name):
    settings = ET.SubElement(root, 'settings', {'pass': pass_name})
    return ET.SubElement(settings, 'component', {
        'name': name, 'processorArchitecture': 'amd64', 'language': 'neutral',
        'publicKeyToken': '31bf3856ad364e35', 'versionScope': 'nonSxS',
        'xmlns:wcm': 'http://schemas.microsoft.com/WMIConfig/2002/State',
        'xmlns:xsi': 'http://www.w3.org/2001/XMLSchema-instance'})


def _text(parent, tag, value):
    ET.SubElement(parent, tag).text = value


def _commands(component, commands, first_logon=False):
    section = ET.SubElement(component, 'FirstLogonCommands' if first_logon else 'RunSynchronous')
    for order, text in enumerate(commands, 1):
        command = ET.SubElement(section, 'SynchronousCommand' if first_logon else 'RunSynchronousCommand', {'wcm:action': 'add'})
        _text(command, 'Order', str(order))
        _text(command, 'CommandLine' if first_logon else 'Path', text)


def generate_unattend(*, bypass_tpm=False, bypass_secure_boot=False, bypass_ram=False,
                      local_account=None, disable_telemetry=False, skip_online_account=False) -> str:
    """Generate amd64 Windows Setup XML; defaults select no customization.

    local_account explicitly creates a blank-password administrator and requests
    a password change at first logon. No passwords are accepted by this API.
    disable_telemetry selects OOBE ProtectYourPC=3, not a telemetry kill switch.
    """
    for name, value in (
        ('bypass_tpm', bypass_tpm), ('bypass_secure_boot', bypass_secure_boot),
        ('bypass_ram', bypass_ram), ('disable_telemetry', disable_telemetry),
        ('skip_online_account', skip_online_account)):
        if type(value) is not bool:
            raise ValueError(name + ' must be a bool')
    if local_account is not None:
        _validate_account(local_account)
    root = ET.Element('unattend', {'xmlns': UNATTEND})
    bypasses = [name for enabled, name in (
        (bypass_tpm, 'BypassTPMCheck'), (bypass_secure_boot, 'BypassSecureBootCheck'),
        (bypass_ram, 'BypassRAMCheck')) if enabled]
    if bypasses:
        component = _component(root, 'windowsPE', 'Microsoft-Windows-Setup')
        user_data = ET.SubElement(component, 'UserData')
        _text(user_data, 'AcceptEula', 'true')
        ET.SubElement(ET.SubElement(user_data, 'ProductKey'), 'Key')
        _commands(component, [r'reg add HKLM\SYSTEM\Setup\LabConfig /v ' + name +
                              ' /t REG_DWORD /d 1 /f' for name in bypasses])
    if skip_online_account:
        component = _component(root, 'specialize', 'Microsoft-Windows-Deployment')
        _commands(component, [r'reg add "HKLM\Software\Microsoft\Windows\CurrentVersion\OOBE" /v BypassNRO /t REG_DWORD /d 1 /f'])
    if disable_telemetry or local_account is not None:
        component = _component(root, 'oobeSystem', 'Microsoft-Windows-Shell-Setup')
        if disable_telemetry:
            oobe = ET.SubElement(component, 'OOBE')
            _text(oobe, 'HideEULAPage', 'true')
            _text(oobe, 'ProtectYourPC', '3')
        if local_account is not None:
            accounts = ET.SubElement(ET.SubElement(component, 'UserAccounts'), 'LocalAccounts')
            account = ET.SubElement(accounts, 'LocalAccount', {'wcm:action': 'add'})
            _text(account, 'Name', local_account)
            _text(account, 'DisplayName', local_account)
            _text(account, 'Group', 'Administrators;Power Users')
            password = ET.SubElement(account, 'Password')
            _text(password, 'Value', 'UABhAHMAcwB3AG8AcgBkAA==')
            _text(password, 'PlainText', 'false')
            # Intentionally do not copy upstream's machine-wide maxpwage change.
            _commands(component, [f'net user "{local_account}" /logonpasswordchg:yes'], first_logon=True)
    ET.indent(root, space='  ')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True).decode('utf-8') + '\n'


def write_unattend(destination, *, bypass_tpm=False, bypass_secure_boot=False,
                   bypass_ram=False, local_account=None, disable_telemetry=False,
                   skip_online_account=False) -> dict:
    """Write a NEW answer-file path (not a directory); never overwrite a target.

    Parent directories must already exist and be trusted by the caller. Returns
    path, bytes_written, and architecture. Validation precedes file creation.
    Errors propagate; an I/O failure can leave an incomplete new file.
    """
    content = generate_unattend(
        bypass_tpm=bypass_tpm, bypass_secure_boot=bypass_secure_boot,
        bypass_ram=bypass_ram, local_account=local_account,
        disable_telemetry=disable_telemetry, skip_online_account=skip_online_account).encode('utf-8')
    path = Path(destination)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return {'path': str(path), 'bytes_written': len(content), 'architecture': 'amd64'}
