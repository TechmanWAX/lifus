# Validation and release status

This is an experimental native Ubuntu implementation, not full upstream Rufus parity. Upstream baseline: 714e3ad90126fa0d751d071222f3b29e11d8f435. Windows host source/build files have been removed from the worktree; the original Git history remains.

## Automatic unmount and folder preferences (0.3.0)

- 160 tests passed after integration and security review fixes, covering isolated
  device/mount safety, persistent image-folder preferences, confirmed identity
  handoff through sudo, post-unmount device replacement, open-descriptor identity
  and capacity, and implicit temporary-directory protection.
- Real read-only unmount preflight recognized /dev/sdc as removable SD Storage
  and rejected the internal system NVMe disk. No unmount command was executed
  during feature development; unmount tests mock device topology and commands.
- GUI warnings and rejection/cancellation routes are covered by integration
  tests. Existing strict writer validation remains required after unmount.
- Remembered image folders persist in XDG user configuration, with atomic
  replacement and home fallback; cancelled file dialogs do not save anything.

## Linux-only / sudo update

- 96 tests passed without skips after Linux-only migration and sudo integration.
- Real Tk askpass was displayed under Xvfb: the entry is masked; pressing Cancel
  returned failure with empty stdout. No password was entered or recorded.
- The real CLI invoked system sudo-rs, displayed its password prompt on a PTY,
  and stopped with a nonzero exit on Ctrl-C, before any backend write.
- Password-authenticated root writes were not exercised: no credentials were
  supplied and physical disks remain untouched. Worker/progress/cancellation
  paths are covered by safe tests and authorization-failure probes.
- Original Windows build/source directories are absent; required boot assets
  are under upstream. Git history is intentionally retained.

## Earlier validation (before Linux-only cleanup)

- Ubuntu x86_64: 80 unittest tests passed without skips using system Python and locally unpacked mtools.
- Real regular-file operations exercised Linux partitioning/filesystem tools, FAT32 Windows-layout extraction, NTFS population through wimlib, original UEFI:NTFS partition, answer-file injection, Debian persistence filesystem creation and readback, raw/compressed writes, backups, fixed VHD conversion and error paths.
- QEMU/SeaBIOS boot reached FreeDOS COMMAND.COM C:\\> and the generated AUTOEXEC banner using original Rufus boot resources.
- QEMU/OVMF loaded the real upstream uefi-md5sum executable from the NTFS partition through the original UEFI:NTFS loader. This fixture is NOT Windows.
- qemu-img 10.2.1 accepted fixed VHD with explicit -f vpc and converted it to byte-identical raw data. Automatic format detection selected raw; specify vpc.
- Native Tk UI ran under the desktop and an isolated Xvfb display. Screenshot: dist/validation/lifus-gui.png.
- Debian reproducibility tests compare two builds and a rebuild from the corresponding Git-free source archive.

Generated dist artifacts and old screenshots were removed during cleanup; the
observations above are historical results, not newly rerun boot tests. Rebuild
and rerun the suite for each release.

## Review fixes

Regressions cover source mutation during decoded-image staging; deleted/replaced backup outputs and parent directories; cancellation during final validation/open and between partition commands; sync and advisory cache eviction before readback; advanced GUI progress-event interoperability; frontend filesystem/privilege/options consistency; and packaged source/resource correspondence.

## Not verified / remaining limits

- No physical target disks were written. No physical USB boot or counterfeit-flash hardware tests.
- A production Windows ISO was not available for a full Windows Setup boot test. Windows Setup customizations, large production install.wim splitting, Secure Boot and multiple firmware implementations remain unverified.
- No real Debian persistence boot test; tests cover layout and persistence.conf contents only.
- Cache eviction is advisory and device/controller caches remain; readback is not a guarantee against power loss or malicious hardware.
- Cancellation cannot undo completed writes or interrupt every external tool immediately. Backup of a mounted source is not a consistent snapshot.
- Full parity gaps, including Windows To Go, generic Linux ISO extraction/bootloader repair, VHDX/FFU and other upstream functionality, are recorded in FEATURES.md.

## Reproduce

From the checkout root, with runtime dependencies installed:

    PYTHONPATH=. /usr/bin/python3 -m unittest discover -s tests -v
    /usr/bin/python3 build-deb.py
    /usr/bin/python3 build-source.py

The development host used mtools extracted under the Hermes scratch directory rather than installing it globally. The .deb declares mtools as a dependency. No global installation is performed by these build scripts.
