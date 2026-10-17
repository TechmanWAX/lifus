# Direct Windows FAT32 writing

## Ordinary ISO: zero temporary payloads

The Windows FAT32 path creates GPT/MBR and a FAT32 filesystem **directly on the
confirmed target**, then streams each ISO member through 7z stdout into its final
clusters. It creates **no `partition.img` and no extracted ordinary files**.
`staging_bytes` is **0** for an ISO whose files all fit FAT32, including the tested
Windows10Pro_22H2.iso with its 4,160,744,150-byte `sources/install.esd`.
An empty private working directory is still created (directory metadata only).
The GUI's non-writing plan displays the temporary byte estimate and direct-write
warning. NTFS is explicitly outside this zero-payload-staging scope.

This replaces the earlier sparse-image design: reducing a partition image's
allocated size was not sufficient, because the ISO payload and largest extracted
file still consumed gigabytes of temporary disk space.

## Implementation and safety

`lifus/direct_fat.py` computes the complete FAT32 geometry and namespace before
mutation. It emits reserved sectors, both FATs, FSInfo/backup boot sectors,
directories (LFNs and collision-free short aliases), and sequential file extents
through bounded `pwrite` calls. Every filesystem write checks partition bounds.
The partition uses its full planned size; unallocated clusters retain old bytes.
This is **quick formatting, not secure erasure**. It installs no BIOS boot code.

The direct formatter avoids depending on external formatters' offset/length
semantics or giving mtools an unbounded writable whole-device descriptor. Only
partition-table commands receive the pinned `/proc/<pid>/fd/<fd>` target handle.
The existing no-follow/exclusive target open, topology/identity checks, typed
confirmation and root boundary remain. No kernel mounts, loop devices, sudo calls
or physical disk writes are used by the integration tests.

The source ISO is also pinned by an open descriptor. **Pin its archive parser as
well:** 7z auto-detection on an extensionless `/proc` path can choose a hybrid
ISO's boot-only ISO9660 view instead of its UDF installation tree. The plan records
the detected parser and a namespace fingerprint; execution explicitly selects
that parser and compares the entire pinned namespace before destruction. A real
Windows ISO test found this trap; ISO9660-only synthetic fixtures did not.

Payload reads/writes are at most 1 MiB; stderr retains only a 4 KiB tail. Both
pipes are drained using selectors, with cancellation checks even when a child
produces no stdout. Children are killed/reaped and descriptors closed on errors
or cancellation. FAT generation uses bounded chunks rather than a full in-memory
FAT. Namespace metadata is retained, capped at 100,000 files/directories, 4096-byte
paths, 255 UTF-16 units per component and a 64 MiB archive listing. Direct FAT32
partitions are sector-aligned, at least 64 MiB and less than 2 TiB. Unsupported
names, traversal, links, case collisions, file/directory conflicts, oversized
non-WIM files and capacity failures are rejected before changing the target.

Input bytes are SHA-256 hashed during streaming, without retaining a second copy.
Writes are periodically flushed at 64 MiB. Final fsync and advisory cache eviction
precede byte comparison of generated filesystem metadata and SHA-256 readback of
every file extent. File cluster padding is initialized; free space is not erased
or verified. Source size/mtime/ctime/inode are checked before and after streaming;
restoring mtime after rewriting does not evade the ctime check. Readback is not a
power-loss or hardware-cache durability guarantee.

Cancellation, extraction errors, changed sources, failed writes or failed
readback **can leave incomplete installation media**. There is no rollback;
errors after partitioning explicitly say that the media is incomplete. Success
is returned only after all verification and final synchronization/cancel checks.
Progress carries prepare/write/flush/sync/verify phases through the existing GUI
and privileged-worker channel. Periodic flush events do not change the GUI write
status. 100% means data transfer is complete, not that the operation succeeded:
it stays at 100 during final sync and readback. Only a successful backend result
(after verification and cancellation checks) displays **Готово**. Plans, errors
and cancellation never report a completed write.

## Exceptional oversized WIM and NTFS

An oversized `sources/install.wim` is extracted and split with wimlib **before
changing the target**. Only this WIM and its `.swm` outputs use temporary payload
files; ordinary ISO files still stream directly. The estimate is twice the
oversized WIM size plus 64 MiB split overhead. Existing `install*.swm` collisions,
empty split results and output parts larger than FAT32's real 4 GiB-minus-one-byte
limit are rejected. A single oversized WIM resource may not be splittable.
Unusual header growth or concurrent disk usage can exhaust the conservative
estimate, but splitting still fails before destruction. Split files are removed
with the private working directory on completion/cancellation/failure.

NTFS retains full extraction, uncompressed capture/apply, the partition image,
and the upstream UEFI:NTFS partition. Its estimate remains partition size plus
twice the payload. Generic formatting and persistence also retain their existing
private-image staging paths. No sparse-filesystem requirement remains for direct
Windows FAT32.

## Verified results

Only private regular-file targets were written; the source ISO was read-only.
The real integration script independently re-extracted and SHA-256 compared
**all 908 files**, not just the EFI loader or large ESD, against mtools readback.

| Real Windows10Pro_22H2.iso, GPT | Measured value |
| --- | ---: |
| ISO bytes | 4,893,900,800 |
| Payload bytes | 4,888,071,077 |
| Dirty, nonzero-prefilled file target | 8,589,934,592 bytes (8 GiB) |
| Planned temporary payload space | **0 bytes** |
| Peak allocated staging files, sampled every 5 ms | **0 bytes; no files** |
| Filesystem bytes written | 4,907,312,128 |
| Direct write + internal readback | 30.973 s |
| Including independent all-file hashes | 58.584 s |
| Process maximum resident set | 55,028 KiB |

The path/hash manifest SHA-256 was
`ebd4365ba0e712b6a6837119dc9d888c750bd72d72f5258d42f48f660c4d48da`.
These timings are one local file-target run, **not a physical USB speed claim**.

Synthetic GPT and MBR regressions use 256 MiB targets prefilled with nonzero
bytes, independently read nested long names and a 2 MiB zero file through mtools,
and verify partition geometry, unchanged target length, zero staging and all
progress phases. Additional regressions cover read-only fsck.fat acceptance,
UDF parser pinning, bounded pipe memory, blocked-child cancellation/reaping,
short/oversized streams, metadata and file-hash corruption, source mutation and
replacement, target replacement, namespace limits and I/O error reporting.

A real wimlib capture/split/apply round trip uses deliberately reduced test
thresholds (2 MiB FAT test limit, 1 MiB requested split size); every reconstructed
file matches. This is **not** a production >4 GiB WIM test. Existing NTFS, raw
writer, formatting, persistence, unmount/identity, sudo boundary and Qt regressions
remain part of the full suite.

## Reproduce

```sh
QT_QPA_PLATFORM=offscreen /home/techman/.hermes/cache/scratch/lifus-qt-venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=. /home/techman/.hermes/cache/scratch/lifus-qt-venv/bin/python tests/real_windows_direct.py /path/to/windows.iso
python build-deb.py
python build-source.py
```

The opt-in real-ISO test accepts **only a source** and creates its own scratch file
target; it never accepts a disk-device target. It retains that workspace for
inspection and reports its location. Remove it manually when no longer needed.
Returned `bytes_written` counts filesystem writes, not partition-table writes.
Physical USB, Windows Setup boot, Secure Boot, production >4 GiB WIM splitting
and power-loss behavior remain untested.
