# Rufus → Lifus feature audit

This is a native Linux implementation, **not full Rufus parity**. The checklist
below follows the upstream Rufus README at baseline
`714e3ad90126fa0d751d071222f3b29e11d8f435` (available in Git history). “Implemented” describes code paths, not certification on physical
USB devices. No physical target disks were written during development.

| Upstream advertised feature | Linux status and limitations |
|---|---|
| Format USB, flash cards and virtual drives: FAT/FAT32/NTFS/UDF/exFAT/ReFS/ext2/ext3 | Advanced backend builds MBR or GPT with FAT32, NTFS, exFAT, ext2, ext3, or ext4. Real mkfs/file-image tests cover these filesystems. FAT16 exists in the separate FreeDOS builder, not general formatting. UDF and ReFS formatting are absent. Whole USB/removable targets only; regular files require explicit API `allow_file=True`. |
| DOS bootable FreeDOS or MS-DOS | **FreeDOS BIOS implemented and QEMU SeaBIOS boot-tested**: upstream MBR/FAT16 boot assets reach FreeDOS KERNEL, COMMAND, AUTOEXEC and `C:\>` (historical boot run; reproduce with `tests/boot_freedos.py`). Builder creates a regular disk image, subsequently written using validated writer. MS-DOS is absent; no physical USB boot test. |
| BIOS/UEFI bootable drives, including UEFI:NTFS | BIOS supported by FreeDOS builder and preserved boot sectors in raw hybrid images. UEFI supports FAT32 and NTFS installer layouts. NTFS gets the original upstream UEFI:NTFS EFI partition; QEMU/OVMF successfully chained to the real upstream uefi-md5sum executable on NTFS. This is not a Windows boot or Secure Boot certification. No generic BIOS bootloader installation. |
| Bootable ISOs: Windows, Linux, etc. | Raw hybrid-image writing; Windows UEFI FAT32 or NTFS extraction. Not every ISO is a raw-writeable disk image. No generic Linux ISO extraction/bootloader repair. Windows data-layout integration uses a synthetic ISO, not a Windows boot test. |
| Disk images, including compressed | Core writer handles raw images and gzip/bzip2/xz streams. Explicit fixed-VHD import/export is available via vhd-to-raw/raw-to-vhd. Dynamic VHD, VHDX and arbitrary ZIP/7z autodetection are not implemented. |
| Windows 11 without TPM or Secure Boot | Explicit opt-in amd64 answer-file generation ports upstream LabConfig settings; windows --unattend injects the XML. Defaults do not bypass anything. Real Windows installer behavior remains untested. |
| Windows To Go | Not implemented. Windows installer extraction is not Windows To Go. |
| Create VHD/DD, VHDX, FFU of existing drives | Core backup creates raw DD; raw-to-vhd converts that file to fixed VHD. Fixed VHD roundtrip validated by qemu-img with explicit -f vpc. No VHDX/FFU export. |
| Persistent Linux partitions | Narrow Debian Live support described below. No generic distro support, resize, Ubuntu casper conversion, or automatic boot-menu edits. |
| MD5/SHA-1/SHA-256/SHA-512 checksums | Core image module supports these algorithms. MD5/SHA-1 are compatibility checks, not authenticity guarantees. |
| Runtime UEFI bootable-media validation | Not implemented. EFI filename presence checks and written-byte readback are not signature/revocation/runtime boot validation. |
| Windows OOBE/local account/privacy customization | Optional amd64 XML generator provides local-account creation, first-logon password-change request, upstream OOBE privacy settings and BypassNRO. Not a universal telemetry disable switch or guaranteed support for every Windows build. See WINDOWS_CONFIG.md. |
| Bad blocks and fake-flash checks | Destructive full-device address-dependent pseudorandom write/read comparison, 1–4 passes; detects mismatches/wrapped capacity, stops on first error. Never restores original data. File-image tested, no counterfeit-hardware test or reliability certification. |
| Download official Windows 8/10/11 retail ISOs | Not implemented. No Fido/Microsoft downloader integration. |
| Download UEFI Shell ISOs | Not implemented. User-supplied images only. |
| Modern familiar UI, 38 languages | Native Tk interface and CLI; no claim of Rufus visual equivalence or 38-language translation coverage. |
| Small footprint, no installation | Python standard-library application plus external Linux tools. Source execution and Debian packaging; not a standalone dependency-free Windows executable. |
| Portable, Secure Boot compatible | Linux-specific dependencies and privilege model. No installed firmware/boot security bypass; image boot compatibility is inherited, not guaranteed. |
| Free software, GPL v3 | Repository remains GPL-licensed; reused upstream assets retain their notices and licenses. |

## Advanced API

```python
plan = plan_operation(action, source, target, **options)
result = execute_operation(plan, progress=callback, cancel=event_or_callable)
```

Actions: `format`, `windows`, `persistence`, `badblocks`.
`source` may be `None` for format/badblocks; Windows and persistence need a
regular ISO. Planning makes no target writes, though it reads ISO metadata and
partition tables. Missing tools are reported where possible; ISO metadata tools
are needed even for ISO previews.

Options:

* `filesystem`: `fat32` (default), `ntfs`, `exfat`, `ext2`, `ext3`, `ext4`.
* `partition_table`: `gpt` (API default) or `mbr`; alias `partition_scheme` is
  accepted for CLI compatibility. MBR is limited to targets below 2 TiB.
* `label`: 1–11 ASCII letters/digits/underscore/hyphen, default `LIFUS`.
* `allow_file`: default false; explicit isolated regular-image test mode.
* `passes`: 1–4, default 1; meaningful for destructive badblocks.
* `persistence_size`: bytes; default remaining aligned unused space.
  Alias `persistence_mib` converts MiB to bytes. Persistence overrides filesystem
  and label to ext4 / `persistence`.
* `staging_dir`: directory with sufficient free space, not located on the target.
* `unattend`: explicit Windows answer XML path; validated namespace and size,
  copied as autounattend.xml. Identity is rechecked before writes. Explicitly
  replaces an existing ISO answer file; never enables bypass options by default.

Plans include `action`, `source`, `target`, identity snapshots, normalized
`options`, `destructive`, `warning`, `steps`, partition `offset`/`size` in bytes,
`required_tools`, `missing_tools`, and conservative `staging_bytes`.
Results include `status='completed'`, action, target, bytes_written, verified,
and a bootability/hardware caveat. Progress receives dictionaries with `phase`,
`completed`, `total`, and (for badblocks) `pass_number`. Errors raise exceptions;
cancellation raises `OperationCancelled`. Cancellation is checked between
chunks/stages, not inside every external mkfs/parted/wimlib invocation.

### Windows UEFI path

Requires `parted`, `mkfs.vfat`, `7z`, **mtools (`mcopy`)**, and `wimlib-imagex`.
The development host lacked mtools; integration used a locally unpacked package,
without installing it system-wide. Production must install this dependency.
No Windows/EFI executable is synthesized. ISO must contain a supported removable
EFI loader (`bootx64.efi`, `bootaa64.efi`, or `bootia32.efi`) and `sources/boot.wim`.
Files are extracted individually through 7z stdout to exclusive ordinary files;
archive paths cannot control root extraction destinations. Absolute paths,
traversal, links reported by the archive, duplicate case-folded paths, and
oversize non-WIM files are refused. No ISO kernel mounts or loop devices are used.
An `install.wim` larger than FAT32's 4 GiB minus one byte limit is split using
wimlib into 3800 MiB `.swm` pieces. That large-WIM branch exists but has not been
validated against a >4 GiB production Windows ISO. Windows boot is not tested.

### Windows NTFS path

Select `--filesystem ntfs` for a main NTFS partition plus the original 1 MiB
Rufus `upstream/res/uefi/uefi-ntfs.img` EFI partition. The ISO payload is extracted in
userspace, captured to a temporary WIM and applied directly into an NTFS image
using wimlib's NTFS support. No mounts are needed. Files above 4 GiB are not
split on NTFS. Both partition payloads are read back after writing.

Real integration tests use ntfscat to verify the copied payload and answer XML,
and compare the EFI partition byte-for-byte against the upstream resource.
QEMU with OVMF loaded the upstream uefi-md5sum executable from the NTFS partition
through UEFI:NTFS in earlier validation (generated screenshots were removed). This fixture
is not Windows; production Windows installation and Secure Boot remain untested.

### Persistence scope

Requires a source ISO whose `.disk/info` identifies Debian, whose layout contains
`live/filesystem.squashfs`, and whose existing boot configuration includes a
`persistence` kernel parameter. It does **not** add that parameter automatically.
The target must have an MBR layout with a free primary slot, no extended or
protective entry, and enough unallocated trailing space. Execution verifies the
entire target prefix against the original ISO before appending anything.
Existing partitions are not resized. A new ext4 partition labelled `persistence`
contains `/ union` in `persistence.conf`. The selected persistence-enabled boot
entry must be used. GPT and stock ISOs without such a boot entry are refused.
A synthetic Debian-layout file image exercises append, filesystem construction,
and reading back persistence.conf; no real Debian persistence boot was tested.

### Destructive scope and safety

* Advanced backend execution requires root, independently of target confirmation.
  The unprivileged GUI uses a sudo helper with masked askpass authentication;
  the CLI uses terminal sudo. Passwords are not stored.
* Target device validation runs again at execution and immediately before writes:
  mounted/protected targets are refused; source/staging on target is refused.
  Identity and source metadata changes invalidate the plan.
* Format and Windows destroy the whole disk's prior partition layout. Badblocks
  overwrites **every byte**, including all partition tables and filesystem data.
  Persistence changes the partition table and unused tail; interruption can still
  damage media. No action provides rollback.
* Filesystems and Windows payloads are staged before the target partition table
  is changed. Staging can require several times the ISO size plus partition size.
* Exclusive block-device opens, no-follow opens, private staging, argv-only
  subprocesses, bounded chunk copying, fsync, and byte-for-byte filesystem
  readback provide defense in depth. Hostile physical device replacement and
  other privileged writers cannot be ruled out by application software.
* No mounts or loops are acquired, so failures/cancellation cannot leak them.
  Staging and descriptors are closed through context managers/finally blocks.
* No physical disk operations or physical USB boot tests were performed. Safe
  integration tests use disposable regular files; root predicate is mocked only
  for those tests, while real partitioning/filesystem tools perform the work.

## Upstream asset reuse

FreeDOS uses the upstream FreeDOS payload and `mbr_win7.h` /
`br_fat16fd_0x3e.h` boot code, with evidence above. Advanced Windows creation
preserves loader assets from the user's ISO instead of embedding alternative
EFI binaries. Only FreeDOS KERNEL/COMMAND, UEFI:NTFS, three ms-sys boot headers
and their notices are retained as runtime assets. Unused GRUB/Syslinux,
localization and Fido resources are not shipped.
No parity claim is inferred from an asset existing in the source tree.
