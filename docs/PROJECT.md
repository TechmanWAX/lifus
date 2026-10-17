# Lifus — native Linux USB media utility

Unofficial Rufus-inspired application for Linux, using Python 3 and PySide6/Qt Widgets (no Wine). The isolated sudo askpass helper still uses Tk.
This checkout contains only the Linux host application and its required boot
resources. It can prepare **Windows installer USB media on Linux**; removing the
Windows host build does not remove Windows ISO support. It is not an official
Rufus release or a full feature-parity port.

## Run from source

```sh
# Run from the repository root
/usr/bin/python3 -m lifus doctor
/usr/bin/python3 -m lifus gui
```

The GUI stays unprivileged. Operations needing administrator access use a sudo
helper and masked askpass dialog; CLI elevation uses standard terminal sudo.
Passwords are not stored. Disk erasure still requires exact target confirmation;
mounted/protected disks are refused. Never select your system disk.

## Build and test (from repository root)

```sh
PYTHONPATH=. /usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 build-deb.py
/usr/bin/python3 build-source.py
```

Builds require Python 3 and dpkg-deb and do not install globally or write devices.
Outputs go to `dist/`. Runtime dependencies and usage:
[Linux README](../README.md). Scope and limitations:
[feature audit](FEATURES.md). Source distribution:
[SOURCE](SOURCE.md).

## Layout and licenses

- `lifus/`: native Linux application (GUI, CLI, guarded disk operations).
- `packaging/`, `build-*.py`: Debian/source packaging.
- `upstream/`: only required FreeDOS/UEFI:NTFS assets, three boot headers
  and licensing notices; no Windows host build.
- `tests/`: unit/integration tests and small attributed reference fixtures.
- `docs/`: Linux-specific feature, validation and source documentation.
- `.git/`: original history retained; Windows baseline is recoverable there.

GPL-3.0-or-later for Lifus; third-party assets retain their own licenses. See
[LICENSE.txt](../LICENSE.txt) and [COPYING.md](../COPYING.md), including source acquisition
links for prebuilt resources. The Linux source archive does not pretend to contain
the complete corresponding source of every embedded boot binary.
