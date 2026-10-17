# PySide6 GUI implementation plan

- Preserve backend interfaces and the existing confirmation, unmount identity, sudo and progress handoff; do not modify backend files.
- Replace the Tk desktop with a compact Russian Rufus-style Qt Widgets window: device/boot selection, partition scheme and target system, formatting, status, progress and log. Default to Windows GPT/UEFI; expose raw copying explicitly and disable irrelevant settings.
- Start executes; a separate advanced-operation preview never writes or authenticates. Keep all previously exposed operations and staging settings available under advanced options.
- Add offscreen real-widget tests first, migrate existing frontend safety/progress/preferences tests without dropping assertions, then run the full suite.
- Declare Debian PySide6 dependencies (retain Tk for isolated askpass), update documentation/CI, build and compare packaged Python bytes. Capture an offscreen screenshot. No host-disk writes or system package installation.
