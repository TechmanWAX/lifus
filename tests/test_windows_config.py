"""Offline contract tests; not a Windows Setup execution test."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

NS = {'u': 'urn:schemas-microsoft-com:unattend'}
WCM = '{http://schemas.microsoft.com/WMIConfig/2002/State}action'


class WindowsConfigTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('lifus.windows_config'))
        from lifus import windows_config
        return windows_config

    def test_defaults_are_inert_xml(self):
        root = ET.fromstring(self.api().generate_unattend())
        self.assertEqual(root.tag, '{' + NS['u'] + '}unattend')
        self.assertEqual(list(root), [])

    def test_write_new_file_and_refuse_existing_or_symlink(self):
        api = self.api()
        self.assertTrue(callable(getattr(api, 'write_unattend', None)))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'autounattend.xml'
            result = api.write_unattend(target, local_account='Alice')
            self.assertEqual(result['path'], str(target))
            self.assertEqual(result['bytes_written'], target.stat().st_size)
            self.assertEqual(target.read_text(), api.generate_unattend(local_account='Alice'))
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            original = target.read_bytes()
            with self.assertRaises(FileExistsError):
                api.write_unattend(target)
            self.assertEqual(target.read_bytes(), original)
            for name, destination in [('link', target), ('dangling', Path(directory) / 'missing')]:
                link = Path(directory) / name
                link.symlink_to(destination)
                with self.assertRaises(FileExistsError):
                    api.write_unattend(link)
            bad = Path(directory) / 'bad.xml'
            with self.assertRaises(ValueError):
                api.write_unattend(bad, local_account='a&b')
            self.assertFalse(bad.exists())

    def test_all_option_combinations_and_serialization(self):
        import itertools
        keys = ['bypass_tpm', 'bypass_secure_boot', 'bypass_ram', 'disable_telemetry', 'skip_online_account']
        for bits in itertools.product((False, True), repeat=len(keys)):
            for account in (None, "O'Neil"):
                root = ET.fromstring(self.api().generate_unattend(**dict(zip(keys, bits)), local_account=account))
                for section in root.findall('.//u:RunSynchronous', NS):
                    orders = [int(x.text) for x in section.findall('u:RunSynchronousCommand/u:Order', NS)]
                    self.assertEqual(orders, list(range(1, len(orders) + 1)))
                self.assertIsNone(root.find('.//u:DiskConfiguration', NS))
                self.assertIsNone(root.find('.//u:PreventDeviceEncryption', NS))
        # The serializer, rather than string interpolation, owns XML escaping.
        element = ET.Element('test')
        self.api()._text(element, 'value', '<>&\"\'')
        serialized = ET.tostring(element, encoding='unicode')
        self.assertIn('&lt;&gt;&amp;', serialized)
        self.assertEqual(ET.fromstring(serialized).findtext('value'), '<>&\"\'')

    def test_oobe_matches_upstream(self):
        api = self.api()
        root = ET.fromstring(api.generate_unattend(
            local_account="Zoë O'Neil", disable_telemetry=True, skip_online_account=True))
        shell = root.find("u:settings[@pass='oobeSystem']/u:component", NS)
        self.assertEqual(shell.get('name'), 'Microsoft-Windows-Shell-Setup')
        self.assertEqual(shell.findtext('u:OOBE/u:ProtectYourPC', namespaces=NS), '3')
        self.assertEqual(shell.findtext('u:OOBE/u:HideEULAPage', namespaces=NS), 'true')
        account = shell.find('u:UserAccounts/u:LocalAccounts/u:LocalAccount', NS)
        self.assertEqual(account.get(WCM), 'add')
        self.assertEqual(account.findtext('u:Name', namespaces=NS), "Zoë O'Neil")
        self.assertEqual(account.findtext('u:Password/u:Value', namespaces=NS), 'UABhAHMAcwB3AG8AcgBkAA==')
        self.assertEqual(account.findtext('u:Password/u:PlainText', namespaces=NS), 'false')
        source = (Path(__file__).resolve().parent / 'fixtures/wue-literals.txt').read_text()
        # Compare actual upstream C string literals, not an invented schema.
        import ast
        import re
        literals = [ast.literal_eval(s) for s in re.findall(r'"(?:[^"\\]|\\.)*"', source)]
        for element in root.iter():
            tag = element.tag.split('}')[-1]
            if tag not in ('unattend', 'settings', 'component'):
                self.assertTrue(any('<' + tag in s for s in literals), tag)
            if tag in ('Path', 'CommandLine'):
                text = element.text.replace("Zoë O'Neil", '%s')
                self.assertIn(text, literals)
        deployment = root.find("u:settings[@pass='specialize']/u:component", NS)
        self.assertEqual(deployment.get('name'), 'Microsoft-Windows-Deployment')
        self.assertIn('BypassNRO', deployment.findtext('u:RunSynchronous/u:RunSynchronousCommand/u:Path', namespaces=NS))
        self.assertNotIn('maxpwage', api.generate_unattend(local_account='Alice'))
        self.assertNotIn('ProtectYourPC', api.generate_unattend(local_account='Alice'))
        self.assertNotIn('LocalAccount', api.generate_unattend(skip_online_account=True))

    def test_reject_unsafe_names_and_non_boolean_flags(self):
        for name in ['', ' ', ' Alice', 'Alice ', '.', 'a/b', 'a\\b', 'a&b',
                     'a<b', 'a"b', 'a%PATH%', 'a^b', 'a`b', 'a\nb',
                     'a\x00b', 'a\x7fb', 'a\u202eb', '\ud800', 'x' * 21,
                     'administrator', 'SYSTEM', 'Администратор', 123]:
            with self.subTest(name=repr(name)), self.assertRaises(ValueError):
                self.api().generate_unattend(local_account=name)
        for option in ['bypass_tpm', 'bypass_secure_boot', 'bypass_ram',
                       'disable_telemetry', 'skip_online_account']:
            for value in ['false', 1, None]:
                with self.subTest(option=option, value=value), self.assertRaises(ValueError):
                    self.api().generate_unattend(**{option: value})
        with self.assertRaises(TypeError):
            self.api().generate_unattend(password='secret')

    def test_bypasses_independent_and_match_upstream(self):
        source = (Path(__file__).resolve().parent / 'fixtures/wue-literals.txt').read_text()
        for option, registry in [('bypass_tpm', 'BypassTPMCheck'),
                                 ('bypass_secure_boot', 'BypassSecureBootCheck'),
                                 ('bypass_ram', 'BypassRAMCheck')]:
            with self.subTest(option=option):
                root = ET.fromstring(self.api().generate_unattend(**{option: True}))
                settings = root.find('u:settings', NS)
                self.assertEqual(settings.attrib, {'pass': 'windowsPE'})
                component = settings.find('u:component', NS)
                self.assertEqual(component.attrib, {
                    'name': 'Microsoft-Windows-Setup', 'processorArchitecture': 'amd64',
                    'language': 'neutral', 'publicKeyToken': '31bf3856ad364e35',
                    'versionScope': 'nonSxS'})
                command = component.find('u:RunSynchronous/u:RunSynchronousCommand', NS)
                self.assertEqual(command.get(WCM), 'add')
                self.assertEqual(command.findtext('u:Order', namespaces=NS), '1')
                expected = 'reg add HKLM\\SYSTEM\\Setup\\LabConfig /v ' + registry + ' /t REG_DWORD /d 1 /f'
                self.assertEqual(command.findtext('u:Path', namespaces=NS), expected)
                self.assertIn(registry, source)
                self.assertIn(expected.replace(registry, '%s').replace('\\', '\\\\'), source)
                self.assertEqual(len(root.findall('.//u:Path', NS)), 1)
                self.assertEqual(component.findtext('u:UserData/u:AcceptEula', namespaces=NS), 'true')
                self.assertIsNotNone(component.find('u:UserData/u:ProductKey/u:Key', NS))
