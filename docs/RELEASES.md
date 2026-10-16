# GitHub Actions builds and releases

Workflow: `.github/workflows/release.yml` (Ubuntu 24.04).

- Push to `master`, pull requests to `master`, and manual **Run workflow**:
  run tests, build the architecture-independent Debian package and source archive,
  test extracted package modules, and verify a byte-identical rebuild from the
  source archive without Git. Download the resulting artifact from the run page.
- Pushing `vX.Y.Z` additionally creates a published GitHub release containing
  `lifus_X.Y.Z-1_all.deb`, `lifus-X.Y.Z-source.tar.gz`, and `SHA256SUMS`.
- The tag must exactly match `__version__` in `lifus/__init__.py`. No tag is
  created automatically. Update build-script defaults and version-specific tests
  when changing the application version.
- Manual runs only build artifacts; they do not create releases.

After committing and pushing the desired version:

```sh
git tag -a v0.3.0 -m 'Lifus 0.3.0'
git push origin v0.3.0
```

Before pushing a release tag, review `COPYING.md` and provide corresponding upstream
source, patches and build material for bundled third-party boot binaries where
required. The application source archive alone is not a complete source bundle
for these binaries. Automated tests are not physical USB or Windows boot tests.

The workflow uses the built-in `GITHUB_TOKEN`; no personal token or repository
secret is required. Only the release job has `contents: write`; build/test jobs
are read-only and checkout does not persist credentials. Action revisions are
pinned to commit hashes. If this fork has Actions disabled, enable them under
GitHub → Actions. Organization policies can also restrict workflow execution.

A repeated tag run fails if its release already exists instead of overwriting
release files. Do not move published version tags. Fix failures before creating
another version, or resolve the existing release explicitly before retrying.

Local equivalent:

```sh
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 build-deb.py --version 0.3.0
/usr/bin/python3 build-source.py --version 0.3.0
/usr/bin/python3 packaging/verify-release.py --version 0.3.0
```

Build from a clean checkout. GitHub Actions checks out a clean tree. The source
builder is directory-based rather than `git archive`; avoid unrelated local
files inside source directories. The verifier rejects common secret/cache paths
in the source archive, but is not a general secret scanner.
