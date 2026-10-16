# Copyright, licenses and third-party source

Lifus is an unofficial native Linux application inspired by Rufus. It is not an
upstream Rufus release. Lifus code is GPL-3.0-or-later; see LICENSE.txt (preserved
from Rufus). Rufus copyright belongs to Pete Batard and its contributors. Original
Lifus contributions belong to their contributors. No warranty is provided.

## Retained upstream material

Resources were copied without further modification from Rufus commit
`714e3ad90126fa0d751d071222f3b29e11d8f435`:
https://github.com/pbatard/rufus/tree/714e3ad90126fa0d751d071222f3b29e11d8f435

- `upstream/res/freedos/`: FreeDOS 1.4 binaries and original provenance
  readme. FreeDOS programs retain their individual authors' copyrights and
  licenses, rather than being relicensed as Lifus. The Linux builder uses
  only KERNEL.SYS and COMMAND.COM; unused utilities, fonts and keyboard layouts
  are omitted. The unmodified readme identifies the original distribution and
  package names. KERNEL.SYS already has Rufus' documented FORCELBA patch (byte
  offset 0x0D = 0x01); migration makes no further binary changes.
- `upstream/res/uefi/`: original UEFI:NTFS FAT image and readme. The image
  contains UEFI:NTFS 2.8 bootloaders, ntfs-3g-derived 1.9 NTFS drivers, and EfiFs
  1.12 drivers. Preserve the projects' own licenses and copyright notices;
  UEFI:NTFS is GPLv2-or-later and EfiFs is GPLv3-or-later. Signing does not change
  those licenses. A signed binary is not a guarantee of firmware acceptance.
- `upstream/src/ms-sys/inc/`: only `mbr_win7.h`, `br_fat16_0x0.h`, and
  `br_fat16fd_0x3e.h`, used by the FreeDOS image builder. These are the original
  boot-byte headers from Rufus' ms-sys integration (GPLv2-or-later); retain any
  embedded notices. ms-sys credits Henrik Carlqvist and its contributors.
- `tests/fixtures/wue-literals.txt`: test-only string excerpts, not a
  Windows implementation. Copyright © 2022–2026 Pete Batard, GPL-3.0-or-later.
  The complete original license notice is retained. The native answer-file
  implementation adapts Rufus' CreateUnattendXml semantics; Rufus credits AveYo
  for the online-account bypass technique.
- `tests/fixtures/uefi-md5sum/`: original x64 EFI test payload and readme,
  with its upstream GPLv2 license. Used only for optional emulator validation;
  not installed by the Debian package.

Additional license texts are in `upstream/licenses/` (GPLv2, LGPLv2.1,
UEFI:NTFS and EfiFs). GPLv3 is in the root LICENSE.txt. The original Lifus SVG
icon is not the upstream Rufus icon.

## Obtaining upstream source for binary resources

The Lifus source archive contains the Linux application, tests, build scripts,
notices and retained resources. **It is not the complete corresponding source
of every prebuilt boot binary.** Removing the Windows application does not remove
the obligation to provide corresponding source when redistributing GPL binaries.
Distributors must provide matching upstream source, patches and build material
under each applicable license; links here are provenance/acquisition guidance,
not a claim that links alone discharge every distribution obligation.

- FreeDOS 1.4 distribution, with package/source provenance:
  https://www.ibiblio.org/pub/micro/pc-stuff/freedos/files/distributions/1.4/
  (original binary bundle: FD14-FullUSB.zip; see res/freedos/readme.txt).
  Kernel source: https://github.com/FDOS/kernel
  Command interpreter source: https://github.com/FDOS/freecom
  Other package sources: https://www.ibiblio.org/pub/micro/pc-stuff/freedos/files/
  Obtain versions matching the 1.4 package metadata and apply the documented
  FORCELBA change when reproducing the Rufus kernel payload.
- UEFI:NTFS 2.8: https://github.com/pbatard/uefi-ntfs/tree/v2.8
- EfiFs 1.12: https://github.com/pbatard/efifs/tree/v1.12
- NTFS UEFI driver 1.9: https://github.com/pbatard/ntfs-3g/releases
  and https://github.com/pbatard/ntfs-3g (see the original UEFI resource readme).
- ms-sys: https://ms-sys.sourceforge.net/ and the pinned Rufus tree above.
- uefi-md5sum: https://github.com/pbatard/uefi-md5sum ; the exact test binary is
  preserved from the pinned Rufus commit above.
- Original wue.c reference:
  https://github.com/pbatard/rufus/blob/714e3ad90126fa0d751d071222f3b29e11d8f435/src/wue.c

The pinned Windows baseline remains available in Git history, but is not shipped
in the Linux source archive. See docs/SOURCE.md for Linux rebuild steps.
