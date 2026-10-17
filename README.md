# Lifus — native Linux USB utility

Lifus is an **unofficial GPL Rufus-inspired Linux port**, not an official Rufus release and not full Windows Rufus feature parity. The Windows host build has been removed; Git history is retained. Windows ISO media creation on Linux remains supported. The Linux application uses Python 3 and native PySide6 (Qt Widgets); it does not use Wine.

## Run from checkout

Use an interpreter with PySide6 installed. On distributions providing the PySide6 Debian packages, these supply it for the system interpreter; a development virtual environment can instead use `python -m pip install PySide6`. Ubuntu 24.04 does not provide these packages: use source execution in a virtual environment there, not the `.deb` installation instructions below. Run the examples with that environment's `python` rather than `/usr/bin/python3`:

```sh
# Run from the repository root
/usr/bin/python3 -m lifus doctor
/usr/bin/python3 -m lifus gui
QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m unittest discover -s tests -v
```

Debian/Ubuntu runtime packages: `python3 python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets python3-tk sudo util-linux fdisk parted dosfstools e2fsprogs exfatprogs ntfs-3g mtools p7zip-full wimtools`. Tk is retained only for the isolated sudo askpass helper. Individual advanced operations report missing tools. `doctor` reports available executables, Tk askpass and privilege/display status without changing disks; it does not check PySide6 availability.

## CLI

From the checkout use `/usr/bin/python3 -m lifus` in place of installed `lifus`:

```sh
lifus devices --json
lifus inspect installer.iso --json
lifus checksum installer.iso --algorithm sha256
lifus backup /dev/sdX new-backup.img
lifus freedos new-freedos.img --size-mib 64
# DESTRUCTIVE: inspect the exact disk carefully before executing!
lifus write installer.iso /dev/sdX --yes --confirm-device /dev/sdX
# Opt in to ordinary unmounting of safe removable volumes before writing:
lifus windows windows.iso /dev/sdX --execute --unmount --yes --confirm-device /dev/sdX
# Advanced operations only show a plan unless --execute is specified:
lifus format /dev/sdX --filesystem fat32 --partition-scheme mbr
lifus windows windows.iso /dev/sdX
lifus persistence linux.iso /dev/sdX --persistence-mib 4096
lifus badblocks /dev/sdX --passes 2
# Temporary payload space is needed only for oversized WIM splitting or NTFS:
lifus windows windows.iso /dev/sdX --staging-dir /path/to/large/disk
# Optional file-only conversion and explicit unattended configuration:
lifus raw-to-vhd disk.img new-disk.vhd
lifus vhd-to-raw disk.vhd new-disk.img
lifus unattend new-autounattend.xml --local-account user
lifus windows windows.iso /dev/sdX --unattend new-autounattend.xml
```

Every destructive execution requires **both** `--yes` and `--confirm-device` matching the target argument exactly, including advanced `--execute`. Mounted targets remain blocked unless explicit `--unmount` authorization permits safe ordinary unmounting before the write. System/swap/internal/stacked disks remain forbidden. CLI operations needing privilege use standard terminal sudo authentication. Do not run commands against your system disk. Backup and FreeDOS creation require a new output file. `--allow-file` is an explicit raw-write testing escape hatch for existing regular-file targets, not a way to bypass confirmation. Raw writes verify by default; `--no-verify` opts out. JSON output is available for every command, including errors; failures exit nonzero. Ctrl-C may leave a partially written target.

Raw writing works for disk images and bootable hybrid ISOs; copying an arbitrary ISO byte-for-byte does not guarantee bootability. Windows media preparation, persistence, formatting and bad-block checks depend on backend support and external Linux tools; inspect their plans before executing. Persistence is distribution-specific, not universal. No Windows To Go or firmware boot guarantee is claimed. Optional unattended XML generation exposes explicit `--bypass-tpm`, `--bypass-secure-boot`, `--bypass-ram`, `--local-account`, `--disable-telemetry` and `--skip-online-account` settings; all boolean switches default off. XML injection requires `windows --unattend PATH`. These settings are not a boot/security guarantee; see the feature audit for tested scope.

## Windows temporary space

Windows FAT32 is written **directly to the target** with bounded streaming buffers:
no partition image and no extracted ordinary payload files. The plan reports
**0 temporary bytes** for ISOs whose files fit FAT32 (including `install.esd`).
Only an oversized `install.wim` requires exceptional extraction/splitting before
destruction; its estimate is twice that WIM's size plus 64 MiB. Input hashes are
compared against every target file after fsync, and filesystem metadata is read
back. Cancellation or read/write errors can leave **incomplete media**.
This is **quick formatting, not secure erasure**: old bytes remain in free clusters.
NTFS retains the larger capture/apply staging path and original UEFI:NTFS loader.
See [WINDOWS_STAGING.md](docs/WINDOWS_STAGING.md) for measurements and limits.

## Native GUI

The compact Russian PySide6 interface follows Rufus's device / boot selection / partition scheme / formatting / status layout. **Windows ISO with GPT / UEFI is the default.** Select a disk and ISO, then **СТАРТ** to execute after confirmation. There is no execution checkbox. **Просмотр плана** is a separate, non-writing action for Windows, format, persistence and badblocks; it never authenticates or unmounts. A mounted disk may fail backend preview validation: preview never bypasses safety checks.

**RAW / DD** is an explicit alternative for disk images and hybrid ISOs: it copies the source's partition layout and filesystem. Scheme, filesystem and volume-label settings are disabled because they cannot change a raw copy. Windows preparation uses the selected GPT/MBR scheme and FAT32/NTFS filesystem, but supports UEFI, not legacy BIOS boot. MBR requires firmware that supports UEFI boot from MBR media; firmware compatibility still depends on the source, not just the displayed target system.

**СТАРТ** runs work in a background thread with phase-aware progress; **ОТМЕНА** requests cooperative cancellation. Preparation is indeterminate. Write progress reaches 100% when data transfer finishes and stays there during final synchronization and readback; only a successful backend result shows **Готово**. Periodic durability flushes remain enabled without interrupting the write status. 100% alone is not permission to remove the device. Use **Журнал** or **Сохранить журнал…** for details. Inspect/checksum use the source image. Backup reads the disk into a new file. FreeDOS creates a new 64 MiB image for subsequent raw writing. All previously available operations remain in the operation selector; **Расширенные параметры** exposes the output file, persistence size, badblocks passes (1–4) and staging directory. Staging must not be on the target and needs sufficient free space.

All discovered disks remain visible, including read-only backup sources. For mounted removable media, the GUI performs a safety preflight and asks permission to unmount listed volumes before writing. System/swap/internal/stacked disks remain refused. Busy filesystems are never forcibly unmounted. See [UNMOUNT.md](docs/UNMOUNT.md). Live backups of mounted filesystems may be inconsistent; quiesce applications or use an offline source.

Disk erasure requires typing the exact device path in a confirmation dialog. The main GUI stays unprivileged. Operations requiring root use a separate sudo helper, with a masked askpass dialog when authentication is needed. CLI commands use standard terminal sudo authentication. Passwords are not stored; cancelling authentication prevents the operation. For advanced execution root is required even if the disk is writable by the user. Cancellation is not a rollback and some tools finish their current step before stopping. Checksum/inspection do not offer mid-operation cancellation. Keep the GUI open until its worker finishes.

## Remembered image folder

The source-image picker remembers the directory of the last selected file across
application restarts. Settings are stored in `$XDG_CONFIG_HOME/lifus/settings.json`
or `~/.config/lifus/settings.json`. Cancelling selection preserves the previous
value. Missing/unreadable directories fall back to the home directory; config
write failures never block image selection. Preferences are not used by the
privileged worker.

## Automated GitHub builds

GitHub Actions builds and tests pushes to `master`, pull requests and manual runs.
Version tags `vX.Y.Z` publish a release with the Debian package, application
source archive and checksums. See [release workflow](docs/RELEASES.md) for usage
and required third-party source checks before publication.

## Build a Debian package (does not install)

```sh
/usr/bin/python3 build-deb.py
/usr/bin/python3 build-source.py
# Result: dist/lifus_0.4.0-1_all.deb
# Optional inspection:
dpkg-deb --info dist/lifus_0.4.0-1_all.deb
dpkg-deb --contents dist/lifus_0.4.0-1_all.deb
```

The builder requires `dpkg-deb`, uses a temporary staging directory, and does not need root. It installs no files globally. Optional installation, only when you decide to do so: `sudo apt install ./dist/lifus_0.4.0-1_all.deb`.

Package paths:

- `/usr/bin/lifus`: launcher pinned to `/usr/bin/python3`, independent of active virtual environments.
- `/usr/share/lifus/lifus/`: Python modules.
- `/usr/share/lifus/lifus-privileged.py`: isolated sudo worker (0644; launched with system Python `-I`).
- `/usr/libexec/lifus-askpass`: masked Tk authentication prompt (0755).
- `/usr/share/lifus/upstream/res/freedos/`: preserved upstream FreeDOS resources.
- `/usr/share/lifus/upstream/src/ms-sys/inc/`: `mbr_win7.h`, `br_fat16_0x0.h`, `br_fat16fd_0x3e.h`.
- `/usr/share/applications/lifus.desktop` and `/usr/share/icons/hicolor/scalable/apps/lifus.svg`: native desktop entry and original Lifus icon.
- `/usr/share/doc/lifus/`: README, FEATURES audit, SOURCE instructions and upstream GPL license.

Distribute the matching `dist/lifus-0.4.0-source.tar.gz` with the binary. It contains this **local Linux application tree and required resources**,  not a claim that upstream GitHub contains the port. See [SOURCE.md](docs/SOURCE.md) for source exclusions, rebuild instructions and `SOURCE_DATE_EPOCH` normalization. See [FEATURES.md](docs/FEATURES.md) for implemented scope and full-port gaps.

The code and resources retain their upstream licensing; see `LICENSE.txt` and notices in upstream resources. See `COPYING.md` for resource attribution, matching-source acquisition and redistribution obligations. The Linux archive does not contain the full original Windows source tree or full sources for prebuilt boot binaries.
