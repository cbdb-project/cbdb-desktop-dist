---
name: cbdb-diagnose
description: Diagnose a CBDB-Desktop runtime failure - unstyled or blank pages, "Database file not found", stylesheet MIME-type errors, 404s, macOS Gatekeeper refusals, or a binary that will not start. Use when someone pastes a browser console log or a terminal error from cbdb.
---

# Diagnosing a CBDB-Desktop failure

These are the failures actually seen in the field, with the reasoning that
identified them. `AGENTS.md` has the full background.

Every shell block below is **Git Bash** — PowerShell aliases `curl` to
`Invoke-WebRequest`, whose flags differ, and has no `grep`. Replace `<port>`
with the port the program printed and `<binary>` with a real path.

## Check the status code before believing the error message

The single most misleading symptom:

    Refused to apply style from '.../static/cbdb_styles.css' because its MIME
    type ('text/plain') is not a supported stylesheet MIME type

This is almost never a MIME configuration problem. Go answers **404** with a
`text/plain` body, so a missing file presents itself as a MIME-type complaint.
The stylesheet was never sent at all.

    curl -s -D - -o /dev/null http://localhost:<port>/static/cbdb_styles.css

- `404` → an asset-path problem, below. This was the real cause on macOS.
- `200 text/plain` → genuinely the host MIME database. The shipped builds
  register the types explicitly, so this means an unpatched binary.

## "Database file not found: Data/CBDB.db"

The program resolved its assets against the **working directory**. Either it is
an unpatched build, or it was started in a way that defeats the patch.

- macOS Finder starts a program with the user's **home** directory as the
  working directory. Either `cd` into the folder first, or use a patched build.
- On Windows, a shortcut with a different "Start in" does the same.

The `Database opened:` line is **always** absolute — upstream's
`openDatabase` calls `filepath.Abs` before logging — so its shape proves
nothing. What matters is *which* absolute path: a patched build shows one next
to the executable, an unpatched one shows the working directory joined to
`Data/...`. Start the program from an unrelated directory and compare.

Check the **case** too: upstream's default is `Data/CBDB.db` while the shipped
file is `Data/cbdb.db`. That only works because NTFS and default APFS ignore
case; on a case-sensitive volume it fails.

## Every page 404s, or the UI runs but is unstyled

`/static/` is registered twice upstream, and the hardcoded `./Static/`
registration wins unless the process started in the program's own directory.
Confirm by requesting the directory itself:

    curl -s -o /dev/null -w "%{http_code}\n" http://localhost:<port>/static/

`200` (a directory listing) means the route works. `404` means the shadowed
registration is in play.

If `/` itself 404s **with no log line at all**, `SetupNavigationRoutes` failed
and its error was discarded — an unpatched build with a wrong `Templates/`
path. A patched build calls `log.Fatalf` and says so.

## macOS will not launch the binary

- **"cannot be opened because it is from an unidentified developer"** —
  Gatekeeper. Not signed with an Apple Developer ID, not notarized.
  Right-click → Open, or System Settings → Privacy & Security → "Open Anyway".
  Once is enough; approving clears the download flag for good.
- **"Permission denied"** — the execute bit is missing. It survives
  `tar xzf` (the archives store the mode) but not a zip repacked on Windows or
  a FAT32 stick. `chmod +x cbdb`.
- **"Killed: 9" or "bad CPU type in executable"** — wrong architecture, or an
  unsigned arm64 binary. Apple Silicon requires every executable to carry at
  least an ad-hoc signature; the shipped arm64 build has one. Check the Mac
  with `uname -m` (`arm64` vs `x86_64`) and use the matching folder.

## /favicon.ico 404s

Cosmetic. Browsers probe it unprompted. Shipped builds serve it; an unpatched
one does not, because upstream's `/{page}` wildcard swallows it.

## Deciding whether a build is patched

    grep -ac "It should sit in Data/ next to" <binary>    # non-zero = patched
    grep -ac "Run the server from the CBDB-GO" <binary>   # non-zero = unpatched
    go version -m <binary> | grep -E "tags|CGO|GOARCH|GOOS"

`grep -c` counts matching *lines*, not occurrences, and exits 1 when there are
none — so read it as non-zero versus zero, not as a count.

`go version -m` works on a macOS binary from Windows, so architecture and build
tags can be checked without a Mac.
