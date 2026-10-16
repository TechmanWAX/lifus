"""Best-effort per-user preferences; never used by privileged disk operations."""
import json
import os
from pathlib import Path
import tempfile


def _settings_file():
    configured = os.environ.get('XDG_CONFIG_HOME', '')
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / '.config'
    return base / 'lifus/settings.json'


def _read(path):
    try:
        if path.is_symlink():
            return {}
        with path.open('r', encoding='utf-8') as stream:
            text = stream.read(65537)
        if len(text) > 65536:
            return {}
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def get_last_image_directory():
    """Return a usable remembered folder, or the user's home directory."""
    value = _read(_settings_file()).get('last_image_directory')
    if isinstance(value, str) and value and os.path.isabs(value):
        try:
            if Path(value).is_dir() and os.access(value, os.R_OK | os.X_OK):
                return value
        except (OSError, ValueError):
            pass
    return str(Path.home())


def remember_image_directory(selected_file):
    """Persist only successful selection; configuration failure never blocks UI."""
    if not selected_file:
        return
    temporary = None
    try:
        selected = Path(os.path.abspath(os.path.expanduser(os.fspath(selected_file))))
        if not selected.is_file():
            return
        directory = selected.parent
        if not os.access(directory, os.R_OK | os.X_OK):
            return
        path = _settings_file()
        if path.is_symlink():
            return
        settings = _read(path)
        settings['last_image_directory'] = str(directory)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.settings-',
                                         suffix='.json', dir=path.parent, delete=False) as out:
            temporary = Path(out.name)
            json.dump(settings, out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, ValueError, TypeError):
        # The application is still usable with a read-only/broken config folder.
        return
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
