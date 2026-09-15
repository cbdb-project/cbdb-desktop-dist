crossbuild -- build the CBDB-Desktop distributions from Windows
===============================================================

Entry point:

    python build-dist.py                    # all three targets
    python build-dist.py --audit-only       # report upstream defects and stop
    python build-dist.py --target mac-arm64
    python make-favicon.py --preview        # only when the logo changes

Input is a single upstream release archive -- wherever CBDB_ARCHIVE in .env
or --archive points, or else the one sitting in the repository root.
Nothing here names a version, a folder or a path.

What is in here
---------------
  build-dist.py    the whole pipeline: extract, audit, patch a copy of the
                   source, cross-compile, assemble, pack, verify
  pack-dist.py     writes .tar.gz / .zip with explicit Unix permissions;
                   also usable on its own
  make-favicon.py  recolours the logo and emits assets/favicon.ico
  assets/          favicon.ico, committed so a plain build needs no imaging
                   libraries
  ccwrap/          source of the CC shim that fronts `zig cc`
  macstub/         hand-written .tbd stubs (libresolv, CoreFoundation,
                   Security) so the darwin link works without an Apple SDK

Scratch lives outside the repository, at %SystemDrive%\cbdb-dist-build (the
temp directory is the fallback): Go splits $CC on whitespace, so the shims must
not sit under a path containing any.

See ..\AGENTS.md for the full set of traps this kit works around and why each
patch exists.
