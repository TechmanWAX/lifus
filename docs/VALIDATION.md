# Validation and release status

This is an experimental native Ubuntu implementation, not full upstream Rufus parity. Upstream baseline: 714e3ad90126fa0d751d071222f3b29e11d8f435. Windows host source/build files have been removed from the worktree; the original Git history remains.

## Direct FAT32 Windows writing

- 196 tests passed with Qt offscreen after the direct-write change.
- A real Windows10Pro_22H2.iso was prepared on a dirty, nonzero-prefilled private
  8 GiB file target: **0 temporary payload bytes**, **0 allocated staging files**,
  no partition image. All **908 source files** independently matched SHA-256
  hashes read back through mtools. Filesystem writes totalled 4,907,312,128 bytes.
- The measured run took 30.973 s for direct write/internal verification, 58.584 s
  including independent all-file verification, with maximum RSS 55,028 KiB.
  These are file-target measurements, not USB throughput or boot claims.
- The real ISO exposed a 7z parser trap: extensionless pinned descriptors selected
  the boot-only ISO view instead of UDF. Execution now pins the parser and checks
  the complete namespace fingerprint before destruction; a UDF regression covers it.
- GPT/MBR dirty-target overwrite tests prove zero files replace old bytes. Tests
  also cover bounded streams, silent/stderr-blocked child cancellation/reaping,
  source mutation/replacement, target replacement, errors/incomplete-media reporting,
  metadata/hash corruption, read-only fsck.fat and the GUI's zero-staging plan.
- Real small-WIM split/apply reconstruction and existing NTFS UEFI tests passed.
  Production >4 GiB WIM splitting, physical Windows boot and Secure Boot remain
  unverified. Free clusters deliberately retain old bytes (not secure erasure).
- Details, measurements and limitations: [WINDOWS_STAGING.md](WINDOWS_STAGING.md).

## PySide6 / Rufus-style GUI

- 179 tests passed without skips using PySide6 6.12.0 in a scratch virtual
  environment, including real Qt Widgets under `QT_QPA_PLATFORM=offscreen`.
- Real Qt buttons exercised Windows Start versus non-writing Preview, the
  exact-device and unmount/identity/sudo handoff, RAW disabled settings,
  cancellation, close-while-busy, and phase-aware progress. A real checksum ran
  through the background worker and Qt event loop on a temporary regular file.
- GUI safety/progress/preferences tests were migrated from Tk mocks to Qt;
  existing backend changes and security assertions were retained. Debian tests
  check Qt dependencies, all packaged module bytes, reproducibility, and a
  Git-free source rebuild.
- Development interpreter:
  `/home/techman/.hermes/cache/scratch/lifus-qt-venv/bin/python`.
  System Python had no PySide6; no system packages were installed.
- Offscreen screenshot: `/home/techman/.hermes/cache/scratch/lifus-pyside6.png`.
  Qt's offscreen plugin prints a benign `propagateSizeHints` warning when shown.
- No host disk writes, authenticated sudo writes, Windows Setup boot or Secure
  Boot tests were performed for this GUI migration. Tk remains only in askpass.

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

## Progress regression and bursty-write investigation (0.4.0)

- Real offscreen Qt regression reproduced `20 → 0 → 40 → 0 → 60 → 0 → 99`.
  The worker split each dictionary into phase and numeric queue messages, and
  `show_phase()` reset the widget on every update. Phase/count now travel and
  render atomically. Tests poll between every producer message and also drain a
  batch. With the final-only synchronization UX, transfer completion reaches 100;
  final sync and readback retain 100 without claiming success.
- Direct FAT32 emitted progress on every pipe read. A 128 MiB regular-file probe
  with 8 KiB chunks emitted 16,385 write events before the fix; the same probe
  emitted 5 afterward (plus 3 sync and 3 verification events). Same-phase updates
  are limited to about 10/second; phase transitions and final byte counts are
  never suppressed. Bytes are actual measured work, not interpolated progress.
- The existing 64 MiB periodic fsync now emits a distinct `flush` event, which
  the GUI ignores to preserve the write status. Only end-stage `sync` events
  display synchronization at 100%. Cadence, final fsync,
  advisory cache eviction, independent readback and cancellation remain enabled.
  Local regular-file fsync timings were roughly 21–27 ms, not two seconds.
- Read-only probes of the supplied Windows10Pro_22H2.iso found listing around
  28 ms and most sampled small-member 7z startups around 6–8 ms (one cold sample
  275 ms). install.esd produced 63,488 pipe chunks. Cold/warm stream times varied
  substantially (16.37/3.36 s); these are not a before/after speedup benchmark.
  One process per member remains unchanged: it has overhead, but does not prove
  the reported periodic USB stalls and avoids a speculative extraction rewrite.
- After the fixes, rootless real-ISO construction used a private 8 GiB file:
  908 independent mtools file hashes matched, 4,888,071,077 payload bytes,
  zero staging payload files/allocation, write+verify 30.657 s; with independent
  validation 59.560 s. Hash-manifest SHA256:
  `ebd4365ba0e712b6a6837119dc9d888c750bd72d72f5258d42f48f660c4d48da`.
- Final-only synchronization regression suite: 210 offscreen Qt unittests pass.
  Real temporary-file direct FAT32 callbacks exercise periodic flush, final sync
  and readback through real Qt widgets; atomic updates stay monotonic. Errors,
  cancellation and previews never show **Готово**; only backend success does.
  No physical USB write was attempted. USB controller/cache/flash pauses and
  power-loss durability cannot be inferred from these regular-file timings.

## Not verified / remaining limits

- No physical target disks were written. No physical USB boot or counterfeit-flash hardware tests.
- A production Windows ISO was tested for regular-file media construction and content hashes, but not Windows Setup boot. Windows Setup customizations, large production install.wim splitting, Secure Boot and multiple firmware implementations remain unverified.
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
