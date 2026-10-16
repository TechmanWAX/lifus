# Native fixed VHD conversion

```python
from lifus.containers import raw_to_vhd, vhd_to_raw, CancelledError

result = raw_to_vhd('disk.raw', 'disk.vhd', progress=None, cancel=None)
result = vhd_to_raw('disk.vhd', 'restored.raw', progress=None, cancel=None)
```

Both functions accept path-like source/destination arguments and keyword-only
`progress` and `cancel`. No root, mount, network, qemu-img, or third-party Python
package is required. This API converts **regular files only**, not disks. Use the
existing raw backup facility first to capture a disk; conversion does not make a
live filesystem backup consistent.

- `progress(done, total)` reports payload bytes, initially zero and then after
  each bounded chunk. VHD footer bytes are excluded. A final payload callback is
  not a success indication: cancellation, footer write, and fsync can still fail.
- `cancel` is a threading.Event-compatible object with `is_set()`. Cancellation
  raises `lifus.writer.CancelledError` (also exported here).
- The result contains absolute `source` and `destination`, payload `size`, output
  `bytes_written`, output `format` (`vhd` or `raw`), and `sha256` of **all output
  bytes**. Export hashes include the footer, so export/import hashes differ.
- Output data is fsynced before return. SHA256 is computed during writing, not
  by a separate readback. Directory-entry crash durability is not guaranteed.

## Format semantics

Upstream `src/vhd.c` uses Windows CreateVirtualDisk and explicitly requests
`CREATE_VIRTUAL_DISK_FLAG_FULL_PHYSICAL_ALLOCATION` for VHD so the data can be
used as a DD image. This implementation preserves that fixed-disk layout:
unchanged raw payload followed by a single 512-byte, big-endian VHD footer.
No CHS rounding, padding, partition changes, or filesystem changes are made.

The footer uses `conectix`, features 2, version 0x00010000, all-ones data offset,
seconds since 2000-01-01 UTC, creator `Lifu` version 0x00010000, compatible host
marker `Wi2k`, original/current payload sizes, VHD-spec CHS geometry, disk type 2,
a one's-complement byte-sum checksum (checksum field zeroed), and a random UUID.
The saved-state byte and reserved bytes are zero. Repeated exports therefore
need not have identical footer bytes; extracted raw bytes are identical.

Import validates the trailing footer, checksum, feature/version/data-offset
fields, type, aligned positive sizes, and exact current-size/file-length match.
Original and current sizes may differ for resized fixed disks. Geometry is
advisory and is not used to truncate the current payload. Features 2 and 3 are
accepted. Dynamic/differencing VHD, VHDX, FFU, legacy 511-byte footers, empty
images, unaligned raw input, and sizes above 2040 GiB are unsupported. VHDX and
standard SignedImage FFU signatures are explicitly rejected in either direction.
Compression is not decoded by these APIs; supply a decoded raw file.

## File safety and limitations

Every ancestor directory is opened with no-follow semantics and held by a file
descriptor. Source must be a regular non-symlink file. Destination must not exist:
`O_EXCL | O_NOFOLLOW` creation with mode 0600 prevents overwriting existing files,
including same-path inputs and hard links. Existing destinations are never
truncated. FIFOs/devices/directories are refused as sources.

Failures (including progress callback exceptions) remove a partial output only
if its directory entry still identifies the inode created by the operation.
Replacement files are retained, and replacement detected before return fails.
Size/mtime/ctime changes to the open input are detected before success. This is
not a snapshot or protection against malicious simultaneous writes. Use a
private destination directory: inode-check-then-unlink is not atomic against
an attacker concurrently replacing entries in that same directory.

## Validation

Run from the repository root:

```
PYTHONPATH=. python -m unittest discover -s tests -p test_containers.py -v
```

Tests exercise byte-exact 2 MiB roundtrip, footer fields/checksum/UUID, CHS branch
boundaries, SHA256, bounded progress chunks, fsync, malformed/truncated/trailing
bytes, unsupported formats, no-clobber/hardlinks, symlinks including ancestors,
FIFO/directory refusal, cancellation, and replacement-output preservation.
Large-file limits are policy, not a multi-terabyte integration-test claim.
External validation used qemu-img 10.2.1 with explicit `-f vpc`: it accepted the
fixed footer and converted a 2 MiB sample back to byte-identical raw data.
Autodetection selected raw, so specify `-f vpc` for fixed VHD. Evidence is in
`dist/validation/vhd-qemu.json`. Windows interoperability and multi-terabyte
limits remain untested. CLI commands are `raw-to-vhd` and `vhd-to-raw`.
