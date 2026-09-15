r"""Pack a CBDB-Desktop dist folder into a .tar.gz that carries Unix permissions.

NTFS has no execute bit, so every ordinary Windows packer (tar.exe, bsdtar,
Compress-Archive, 7-Zip) writes the macOS binaries as mode 0644 and the user
has to chmod them after unpacking. Here the mode is written into the archive
explicitly, so `tar xzf` on a Mac produces files that are already executable.

    python pack-dist.py "..\dist\CBDB-Desktop-mac-arm64"
    python pack-dist.py "..\dist\CBDB-Desktop-win-x64" -f zip -o ..\packages

A .zip carries the mode too -- macOS's Archive Utility and unzip both honour
the Unix mode in a zip's external attributes, provided create_system says the
archive was made on Unix, which is what -f zip here does.
"""

import argparse
import os
import posixpath
import stat
import sys
import tarfile
import time
import zipfile

# Files that must land executable, matched case-insensitively against the
# basename. Callers can pass their own set; this default covers the names the
# CBDB distributions use.
EXECUTABLE = {"cbdb", "cbdbsetup", "cbdb.exe", "cbdbsetup.exe",
              "cbdb-desktop.command"}


def walk(root):
    """Yield (absolute path, archive-relative posix path) depth-first, sorted.

    Symlinks are refused. os.walk does not follow a symlinked directory, so
    one would be stored as an empty directory with its contents silently
    lost; a dangling link would fail at getmtime part-way through writing the
    archive; and a link to the database would be dereferenced into a second
    full copy.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                raise SystemExit("%s is a symlink; archives here must be self-contained "
                                 "(copy the target in, or exclude it)" % full)
            rel = os.path.relpath(full, os.path.dirname(root))
            yield full, rel.replace(os.sep, "/")


def mode_for(relpath, isdir, executable=None):
    """0755 for directories and the named executables, 0644 for the rest.

    `executable` holds full archive-relative paths. Matching on the basename
    instead would also mark any file named e.g. "cbdb" deeper in the payload
    as executable.
    """
    if isdir:
        return 0o755
    if executable is None:                      # standalone use: basenames
        return 0o755 if posixpath.basename(relpath).lower() in EXECUTABLE else 0o644
    return 0o755 if relpath.lower() in executable else 0o644


def pack_tar(root, out, level=6, executable=None):
    total = 0
    with tarfile.open(out, "w:gz", format=tarfile.GNU_FORMAT, compresslevel=level) as tf:
        top = tarfile.TarInfo(os.path.basename(root))
        # The folder mtime, not time.time(): an otherwise reproducible archive
        # should not change just because it was repacked.
        top.type, top.mode, top.mtime = tarfile.DIRTYPE, 0o755, int(os.path.getmtime(root))
        top.uid = top.gid = 0
        top.uname = top.gname = ""
        tf.addfile(top)
        for full, rel in walk(root):
            isdir = os.path.isdir(full)
            ti = tarfile.TarInfo(rel)
            ti.mtime = int(os.path.getmtime(full))
            ti.mode = mode_for(rel, isdir, executable)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if isdir:
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                ti.size = os.path.getsize(full)
                total += ti.size
                with open(full, "rb") as fh:
                    tf.addfile(ti, fh)
                # Guard the console encoding: on Windows stdout uses the code
                # page unless UTF-8 mode is on, and one CJK filename would
                # raise UnicodeEncodeError in the middle of writing the
                # archive.
                line = "  %-9o %s" % (ti.mode, rel)
                enc = sys.stdout.encoding or "ascii"
                print(line.encode(enc, "replace").decode(enc, "replace"))
    return total


def pack_zip(root, out, level=6, executable=None):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=level) as zf:
        top = zipfile.ZipInfo(os.path.basename(root) + "/",
                              time.localtime(os.path.getmtime(root))[:6])
        top.create_system = 3
        top.external_attr = (0o755 | stat.S_IFDIR) << 16 | 0x10
        zf.writestr(top, b"")                   # pack_tar emits this too
        for full, rel in walk(root):
            isdir = os.path.isdir(full)
            mode = mode_for(rel, isdir, executable)
            zi = zipfile.ZipInfo(rel + ("/" if isdir else ""),
                                 time.localtime(os.path.getmtime(full))[:6])
            zi.create_system = 3  # Unix -- makes external_attr's mode meaningful
            zi.external_attr = (mode | (stat.S_IFDIR if isdir else stat.S_IFREG)) << 16
            if isdir:
                zi.external_attr |= 0x10
                zf.writestr(zi, b"")
            else:
                zi.compress_type = zipfile.ZIP_DEFLATED
                # ZipFile(compresslevel=...) is only consulted when ZipFile
                # builds the ZipInfo itself. A hand-made one -- which is how the
                # Unix mode gets set at all -- keeps _compresslevel = None, and
                # zlib's default silently wins, so -l would do nothing.
                if hasattr(zi, "compress_level"):   # public name, Python 3.13+
                    zi.compress_level = level
                else:
                    zi._compresslevel = level
                with open(full, "rb") as fh, zf.open(zi, "w") as dst:
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        dst.write(chunk)


def atomically(fn, root, out, level):
    """Write to out + ".part" and move it into place only on success.

    Packing takes minutes over hundreds of megabytes; without this, a failure
    or Ctrl-C leaves a truncated archive under the name someone is about to
    ship, and destroys whatever good archive was there before.
    """
    part = out + ".part"
    try:
        result = fn(root, part, level)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise
    os.replace(part, out)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="dist folder to pack")
    ap.add_argument("-o", "--outdir", help="directory to write into (default: the folder's parent)")
    ap.add_argument("-f", "--format", choices=("tar", "zip", "both"), default="tar",
                    help="tar = .tar.gz (default; best for macOS), zip = .zip, both = one of each")
    ap.add_argument("-l", "--level", type=int, default=6, choices=range(1, 10), metavar="1-9",
                    help="deflate level (default 6; 9 is much slower for ~1%% less)")
    args = ap.parse_args()

    root = os.path.abspath(args.folder.rstrip("\\/"))
    if not os.path.isdir(root):
        sys.exit("not a directory: " + root)
    outdir = os.path.abspath(args.outdir) if args.outdir else os.path.dirname(root)
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, os.path.basename(root))

    total = None
    if args.format in ("tar", "both"):
        out = base + ".tar.gz"
        print("packing %s\n     -> %s  (level %d)" % (root, out, args.level))
        total = atomically(pack_tar, root, out, args.level)
        print("\n%.2f GB in  ->  %.2f GB out (%.1fx)"
              % (total / 2**30, os.path.getsize(out) / 2**30, total / os.path.getsize(out)))

    if args.format in ("zip", "both"):
        out = base + ".zip"
        print("\npacking %s\n     -> %s  (level %d)" % (root, out, args.level))
        atomically(pack_zip, root, out, args.level)
        size = os.path.getsize(out)
        if total:
            print("%.2f GB out (%.1fx)" % (size / 2**30, total / size))
        else:
            print("%.2f GB out" % (size / 2**30))


if __name__ == "__main__":
    main()
