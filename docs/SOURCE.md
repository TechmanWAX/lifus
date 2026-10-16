# Corresponding source and reproducible builds

This package is built from the **LOCAL modified Rufus checkout**, not an upstream
Rufus release. https://github.com/pbatard/rufus is the Windows upstream; that URL
does **not** contain this Linux port. The local repository root contains the Linux
implementation, tests, packaging scripts and documentation. Do not substitute a
fresh upstream clone for the corresponding source of this package.

Distribute `lifus-0.3.0-source.tar.gz` alongside `lifus_0.3.0-1_all.deb`.
From the local checkout root, produce both with:

```sh
/usr/bin/python3 build-source.py
/usr/bin/python3 build-deb.py
```

The source builder includes only the Linux tree (including local modifications,
tests, required boot resources and notices), root README, COPYING.md, LICENSE.txt
and Git configuration files. It omits the Windows host source/build tree, Git
metadata, dist artifacts, Python caches and virtual environments. Review the
archive before distribution: this is a local-tree archive, not `git archive`.

This archive is the source of the Linux application, not complete corresponding
source for every prebuilt boot binary. COPYING.md identifies upstream versions,
source acquisition links, the inherited FreeDOS kernel patch and redistribution
obligations. Distributors must supply matching upstream sources/build material
where required by the applicable licenses; links alone are not a universal
substitute for that obligation.

Rebuild from the supplied source archive (no Git checkout required):

```sh
tar -xzf lifus-0.3.0-source.tar.gz
cd lifus-0.3.0
/usr/bin/python3 build-deb.py
# Run from the repository root
/usr/bin/python3 -m unittest discover -s tests -v
```

Build dependency: Python 3 and Debian `dpkg-deb`. Tests additionally require
runtime dependencies listed in README; some integration checks need Xvfb or
QEMU. Package builds never install globally or write a device.

Both builders use `SOURCE_DATE_EPOCH` if provided, otherwise the archived
`SOURCE_DATE_EPOCH`, otherwise the upstream checkout's latest Git commit
timestamp. The source archive records this epoch for rebuilding outside Git.
All package member timestamps, ownership, and modes are normalized; source tar
ownership and timestamps and gzip header are normalized. Reproducibility assumes
identical input contents and toolchain (notably dpkg compression version).

`FEATURES.md` separates implemented paths from remaining full-port gaps. Shipping
an upstream asset is not evidence that its Linux integration or boot behavior
has been implemented or certified.
