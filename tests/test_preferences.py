"""User preference tests, isolated from real user configuration."""
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.config = self.base / 'config'
        self.env = patch.dict(os.environ, {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.config)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def module(self):
        return importlib.import_module('lifus.preferences')

    def test_persist_selection_and_reload(self):
        prefs = self.module()
        self.assertEqual(prefs.get_last_image_directory(), str(self.home))
        folder = self.base / 'images with spaces'
        folder.mkdir()
        image = folder / 'installer.iso'
        image.write_bytes(b'image')
        prefs.remember_image_directory(str(image))
        prefs = importlib.reload(prefs)
        self.assertEqual(prefs.get_last_image_directory(), str(folder))
        self.assertEqual(json.loads((self.config/'lifus/settings.json').read_text())['last_image_directory'], str(folder))

    def test_missing_directory_and_invalid_json_fallback(self):
        prefs = self.module()
        path = self.config/'lifus/settings.json'
        path.parent.mkdir(parents=True)
        for text in ('invalid', '[]', json.dumps({'last_image_directory': str(self.base/'missing')}), json.dumps({'last_image_directory': 42})):
            path.write_text(text)
            self.assertEqual(prefs.get_last_image_directory(), str(self.home))

    def test_cancel_and_nonexistent_selection_do_not_change_preferences(self):
        prefs = self.module()
        selected = self.home/'a.iso'
        selected.touch()
        prefs.remember_image_directory(str(selected))
        before = (self.config/'lifus/settings.json').read_bytes()
        prefs.remember_image_directory('')
        prefs.remember_image_directory(str(self.base/'missing.iso'))
        self.assertEqual((self.config/'lifus/settings.json').read_bytes(), before)

    def test_configuration_write_failure_is_not_fatal(self):
        prefs = self.module()
        self.config.write_text('not a directory')
        selected = self.home/'a.iso'
        selected.touch()
        prefs.remember_image_directory(str(selected))
        self.assertEqual(prefs.get_last_image_directory(), str(self.home))

    def test_gui_uses_saved_folder_and_only_remembers_selection(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from lifus import gui
        prefs = self.module()
        app = SimpleNamespace(source=Mock())
        for selection in ('', str(self.home/'selected.iso')):
            with patch.object(prefs, 'get_last_image_directory', return_value=str(self.home)) as getdir, \
                    patch.object(prefs, 'remember_image_directory') as remember, \
                    patch.object(gui.filedialog, 'askopenfilename', return_value=selection) as choose:
                gui.Application.browse_source(app)
                self.assertEqual(choose.call_args.kwargs.get('initialdir'), str(self.home))
                if selection:
                    remember.assert_called_once_with(selection)
                    app.source.set.assert_called_once_with(selection)
                else:
                    remember.assert_not_called()
                    app.source.set.assert_not_called()

    def test_relative_xdg_ignored_and_other_settings_preserved(self):
        prefs = self.module()
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': 'relative-config'}):
            path=self.home/'.config/lifus/settings.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'other_setting': True}))
            selected=self.home/'a.iso';selected.touch()
            prefs.remember_image_directory(str(selected))
            self.assertTrue(json.loads(path.read_text())['other_setting'])
            self.assertEqual(prefs.get_last_image_directory(), str(self.home))


if __name__ == '__main__':
    unittest.main()
