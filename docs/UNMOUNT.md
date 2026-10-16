# Confirmed automatic unmount

Lifus can prepare mounted removable USB media for a destructive operation.
This is not a general-purpose force-unmount facility and does not relax the
writer's requirement that its target be unmounted and safe.

## GUI flow

1. Select the image and target disk, then start an actual write/format operation.
2. If removable target volumes are mounted, read-only preflight determines
   whether they may safely be unmounted.
3. A warning lists the mountpoints to disconnect and warns about the subsequent
   destructive operation. Rejecting it changes nothing.
4. Type the exact target path to confirm the operation; sudo authentication is
   requested separately if needed. Cancelling either confirmation or
   authentication prevents unmount and write.
5. The privileged worker repeats safety checks and performs ordinary unmounts
   before invoking the existing writer. It checks again that the disk is safe.

## CLI

The opt-in flag is `--unmount`. It must accompany an actual destructive command
and the existing `--yes --confirm-device /dev/DEVICE` consent. Advanced commands
also need `--execute`. Read-only previews do not unmount anything.

Example (DESTRUCTIVE: replace paths only after checking the intended disk):

    lifus windows /path/to/windows.iso /dev/sdX --filesystem ntfs --execute --unmount --yes --confirm-device /dev/sdX

The application prints the unmount warning before privilege escalation. Without
`--unmount`, mounted targets remain blocked.

## Refusals and failure handling

- Non-removable/internal, read-only, stacked or unknown device topology.
- System filesystems or active swap, including swap files backed by the disk.
- Source ISO, answer file or explicit staging directory located on the target.
- Foreign nested mounts whose removal would affect another filesystem.
- Device/mount identity changes between preflight and unmount.
- Busy filesystems or any ordinary unmount failure.

There is no `swapoff`, force/lazy unmount, process killing, recursive unmount or
USB power-off. A failure stops the operation rather than bypassing protection.
Unmounts already completed are not automatically remounted if a later stage
fails; disk writes already completed are not rolled back. Close programs using
the media if the OS reports it busy, then retry.

## Verification boundary

Automated tests use mocked mount/device topology and ordinary temporary files.
They must never unmount host disks or perform a real USB write. Actual media
operations require the user's deliberate confirmations in the application.
