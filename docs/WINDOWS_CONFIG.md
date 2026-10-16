# Native Windows answer-file subset

`lifus.windows_config` generates Windows Setup XML on Linux using only Python's
standard library. This is additional partial Rufus parity, **not a full port**.
It does not mount media, edit an ISO/WIM, partition disks, invoke commands, or
integrate itself into the CLI/GUI. The caller supplies a new output file path.

## API

```python
generate_unattend(*, bypass_tpm=False, bypass_secure_boot=False,
                  bypass_ram=False, local_account=None,
                  disable_telemetry=False, skip_online_account=False) -> str

write_unattend(destination, *, bypass_tpm=False, bypass_secure_boot=False,
               bypass_ram=False, local_account=None,
               disable_telemetry=False, skip_online_account=False) -> dict
```

`generate_unattend` returns UTF-8-declared XML text. `write_unattend` writes that
text as UTF-8 and returns `{'path': str(destination), 'bytes_written': <int>,
'architecture': 'amd64'}`. Destination is a **file**, not a directory; parents
must already exist and be trusted. Typical filename is `autounattend.xml`, to
be placed at installer-media root by a separate integration step. No options
produces an inert `<unattend>` root; no security settings are changed by default.

Writes validate before opening, use exclusive creation (including refusing
existing or dangling destination symlinks), request mode 0600, flush and fsync.
Existing contents are never overwritten. Filesystem errors propagate, and an
I/O error can leave an incomplete new file which must not be used. Parent
symlinks are not prohibited; use a caller-controlled directory. FAT filesystems
may not honor POSIX permissions. No host shell or subprocess is used.

## Exact semantics and limitations

| Option | Pass/component | Actual configuration |
|---|---|---|
| `bypass_tpm` | windowsPE / Microsoft-Windows-Setup | `reg add HKLM\SYSTEM\Setup\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f` |
| `bypass_secure_boot` | same | `BypassSecureBootCheck` only |
| `bypass_ram` | same | `BypassRAMCheck` only |
| `skip_online_account` | specialize / Microsoft-Windows-Deployment | Registry `HKLM\Software\Microsoft\Windows\CurrentVersion\OOBE`, `BypassNRO=1` |
| `disable_telemetry` | oobeSystem / Microsoft-Windows-Shell-Setup | `OOBE/HideEULAPage=true`, `OOBE/ProtectYourPC=3` |
| `local_account` | same | Local administrator in `Administrators;Power Users`, initially empty password; first-logon `net user "NAME" /logonpasswordchg:yes` |

Hardware flags are independent and explicitly opt-in; bypassing setup checks
does not switch off firmware Secure Boot or TPM. The windowsPE component also
contains upstream's `UserData/AcceptEula=true` and empty `ProductKey/Key`.
Selecting a hardware bypass therefore also accepts the installation EULA.

**Local-account warning:** the upstream encoded empty-password value
`UABhAHMAcwB3AG8AcgBkAA==` with `PlainText=false` is not encryption and is not a
user-provided secret. This explicitly creates an administrator with a blank
initial password and requests a change via FirstLogonCommands. Actual prompt
behavior is not verified here; set a strong password promptly. No password
argument exists. Unlike upstream, this subset does **not** issue
`net accounts /maxpwage:unlimited`, which would weaken machine-wide policy.

Names are rejected, never silently rewritten: 1–20 UTF-16 code units, Unicode
letters/numbers plus ASCII spaces, apostrophes, hyphens and underscores, no
leading/trailing whitespace. Controls, formatting characters, shell/XML
metacharacters and upstream's reserved account names are refused. XML is
serialized with ElementTree, not interpolated templates. Flags must be actual
booleans; strings such as `"false"` are rejected.

`disable_telemetry` is an API convenience name for Rufus's OOBE privacy choice,
**not** a guarantee of zero Windows telemetry. No Defender, updates, BitLocker,
password-expiry policy or disk configuration changes are emitted. No implicit
online-account bypass is added when requesting only privacy or a local account.

## Versions and attribution

Only **amd64 (x86-64)** answer-file components are emitted. Intended for booted
Windows 11 installation media; Windows 10 uses the same OOBE components but
has not been tested here. ARM64, x86, S Mode, in-place upgrades, Windows To Go,
and every individual Windows release are not claimed supported. Upstream notes
local-account creation can bypass MSA on Windows 11 22H2 even with networking;
BypassNRO behavior depends on installer build and connectivity. Newer builds
may ignore it. No claim is made that it bypasses current/future requirements.

Semantics adapted from upstream `src/wue.c`, `CreateUnattendXml`,
Copyright © 2022–2026 Pete Batard, GPL-3.0-or-later; see repository license.
Inspected checkout HEAD: `714e3ad90126fa0d751d071222f3b29e11d8f435`.
Relevant source: namespace/components and UserData around lines 136–150;
LabConfig around 240–251; BypassNRO around 266–275; privacy/account and
first-logon sections around 313–382 and 455–465. Reserved names are at 111–115;
`src/rufus.h` also defines upstream username-invalid characters. The resource
tree was searched; this subset's authoritative templates are the C-generated
XML in `wue.c`, not a separate `res/` XML file. Upstream credits AveYo's
MediaCreationTool.bat `bypass11/AutoUnattend.xml` for the specialize bypass.

## Verification performed

From repository root:

```sh
PYTHONPATH=. python -m unittest discover -s tests -p test_windows_config.py -v
```

Six offline tests pass, covering all boolean/account-presence combinations,
parsed namespaces/components/passes and command ordering, comparisons against
attributed upstream C literal excerpts in `tests/fixtures/wue-literals.txt`, Unicode round-trip and XML escaping,
unsafe/reserved names, strict boolean validation, no password parameter,
new-file output/readback and no-clobber/symlink behavior. Tests were run failing
before the corresponding implementation slices. These are structural/source
contract checks, **not Microsoft XSD/catalog validation or real Setup tests**.

The full suite was also run: 72 tests, one unrelated packaging failure
(`test_reproducible_deb_and_corresponding_source`: missing packaged
`usr/share/doc/lifus/FEATURES.md`) and two dependency skips. All six tests for
this module passed. No actual Windows installer or physical disk was used.
Real installation and first-logon behavior remain untested.
