---
name: cbdb-dist
description: Build, verify and publish the CBDB-Desktop distributions (macOS arm64, macOS Intel, Windows x64) from an upstream release archive. Use when asked to cut a release, rebuild the packages, take a new upstream drop, or when build-dist.py fails.
---

# Building the CBDB-Desktop distributions

Read `AGENTS.md` first — it holds the traps and the reasoning. This skill is
the procedure.

Every shell block below is **Git Bash**, and `<repo>` is a placeholder for the
repository path in Git Bash form, e.g. `/c/Users/me/Desktop/CBDB DIST`. The
`python` invocations work in either shell.

## Taking a new upstream drop

1. Point `CBDB_ARCHIVE` in `.env` at the new archive.
   If you are not using `.env`, put it in the repository root and **remove
   the old one** -- `build-dist.py` refuses to guess when there are two.
   Beware: when `CBDB_ARCHIVE` is set, the repository root is never looked
   at, so dropping a new archive there changes nothing.

2. `python crossbuild/build-dist.py --audit-only`

   Read the audit. It should report the five known defects. Otherwise:

   - **`already fixed upstream`** (a patch) or **`already handled upstream`**
     (the added file) — upstream adopted it. Good; it is skipped automatically.
   - **`cannot classify ... the anchor matched N time(s)`** — upstream edited
     those exact lines. Do **not** loosen the anchor to make it match. Read the
     new upstream code, decide whether the defect is still there, and rewrite
     the patch. A patch that applies in the wrong place is worse than one that
     fails loudly.
   - **`Code/ holds entries this kit does not know how to stage`** — upstream
     added a vendor directory, a subpackage, embedded assets or C sources.
     `stage()` copies a flat package of `.go` files plus `go.mod`/`go.sum`,
     skipping `*_test.go`; its gate tolerates but does not copy `.bat`, `.ps1`,
     `.md` and `.txt`. Extend it deliberately rather than widening the gate.

   Then read the **`outside the payload (not shipped)`** block: whatever sits
   at the archive root or beside `Code/`/`Data/`. None of it is packed, since
   `assemble()` copies by allow-list. Before shipping, compare it with the
   previous drop, and treat every entry marked **`NEW, check`** as unverified:

   - grep `Code/` and `Templates/` for its name (file-open sites, `flag`
     defaults in `main.go`, `../../static/...` links);
   - not read by the program → add a pattern to `KNOWN_UNSHIPPED` in
     `build-dist.py` and leave it out;
   - read by the program → add it to `PAYLOAD_DIRS`/`PAYLOAD_FILES`.

   Word documents (`*.docx`) are already classified as not shipped. They are
   upstream's label-revision and translation notes; the label text is
   hardcoded in `Templates/*/index.html`, which only cite them in comments.
   See AGENTS.md, "Root-level files are not shipped unless the program reads
   them".

3. `python crossbuild/build-dist.py`

   About ten minutes. Each target is built, assembled, packed and verified
   before it is given a release name.

4. Check `packages/.built-from`. Any line marked `EARLIER` is an archive left
   over from a previous upstream release — delete it before shipping a set.

## Only when the logo changes

    python crossbuild/make-favicon.py --preview

Then look at `crossbuild/assets/favicon-preview.png`. The badge is detailed, so
16x16 is inherently muddy; 32x32 is the size that matters for a browser tab.
`favicon.ico` is committed, so this is not part of a normal build.

## Checking a delivered archive by hand

The pipeline already verifies against a manifest, but to inspect one — Git
Bash, for `awk`, `sort` and `uniq`:

    tar tvf packages/CBDB-Desktop-mac-arm64.tar.gz | awk '{print $1}' | sort | uniq -c

Expect exactly one `-rwxr-xr-x` (the cbdb binary), everything else
`-rw-r--r--`, directories `drwxr-xr-x`. For the Windows zip, `cbdb.exe` should
be mode 755 with `create_system=3`.

## Smoke-testing for real

Windows is the only target runnable here, and it exercises the same patched
source as the macOS binaries. Run it from an **unrelated** working directory —
that is the whole point of the asset-path patch.

    cd /c/Windows/Temp
    "<repo>/dist/CBDB-Desktop-win-x64/cbdb.exe" -port 18080 &
    curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://localhost:18080/static/cbdb_styles.css
    curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://localhost:18080/favicon.ico
    curl -s http://localhost:18080/api/browser/person/1762 | head -c 60

Expect `200 text/css; charset=utf-8`, `200 image/x-icon`, and a person record.

The program opens a browser tab ~300 ms after start and there is no flag to
stop it. Trimming `PATH` does not help: `openBrowser` uses `rundll32`, which
lives in `C:\Windows\System32`. Just close the tab.

Kill the process afterwards, then delete `Data/cbdb.db-wal`,
`Data/cbdb.db-shm`, `Data/cbdb.db.lock` and `Data/cbdb.db.url` **from the
dist folder** — SQLite writes the first two next to the database,
`instance_lock.go` writes the other two (the single-instance lock, and the
running instance's address for a second launch to find), an abrupt kill
leaves them behind, and they must not end up in a package. (`verify()` would in fact catch them as `UNEXPECTED` members, but
delete them anyway.)

## Never

- Edit anything under `extracted/`. It is upstream. The kit proves it was not
  written to, and that check failing means something is wrong with the kit.
- Commit `extracted/`, `dist/`, `packages/`, `maintainer-tools/` or the
  archive. `AGENTS.md` lists what each is and why it is ignored.
- Point `--work` at a path containing whitespace. Go splits `$CC` on it.
