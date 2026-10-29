r"""Build and pack all three CBDB-Desktop distributions, from Windows.

    python build-dist.py                       # the whole thing, all three targets
    python build-dist.py --audit-only          # just report what is wrong upstream
    python build-dist.py --target mac-arm64    # one target instead of all three

Input is a single upstream release archive (.7z or .zip), named by CBDB_ARCHIVE
in .env or --archive -- which can point anywhere, so a 200 MB input need not be
copied into the repository -- or else the single archive sitting in the
repository root. Nothing here names a version, a folder or a path. It is
unpacked into `extracted`, the payload root is found by looking for the
directory holding both Code/ and Data/, and the build proceeds from there.
A new upstream drop means pointing at a new archive and re-running.

Targets: mac-arm64 and mac-x64 (.tar.gz), win-x64 (.zip).

Why this exists
---------------
Four code changes -- three anchored patches and one added file -- fix defects
in the upstream source that no amount of packaging can work around. The audit
reports five findings, the fifth being a consequence another patch fixes.
Some show up only on macOS; the rest are latent everywhere and merely happened
to be masked by the machine they were first built on:

  1. parseFlags() resolves Data/, Templates/ and Static/ against the CURRENT
     WORKING DIRECTORY. Windows Explorer sets the working directory to the
     executable's folder, so nobody noticed; macOS Finder sets it to the
     user's home directory, so a double-click dies with
         Database file not found: Data/CBDB.db
     It also breaks any Windows shortcut with a different "Start in".

  2. The default database path is "Data/CBDB.db" but the shipped file is
     "Data/cbdb.db". Case-insensitive volumes (NTFS, default APFS) paper over
     the mismatch; a case-sensitive APFS volume does not.

  3. /static/ is registered TWICE: once in main() with the configured path,
     and earlier inside SetupNavigationRoutes hardcoded to "./Static/".
     gorilla/mux matches in registration order, so the hardcoded one wins and
     every asset 404s unless the process was started from the program's own
     directory. Go serves that 404 as text/plain, which is why a browser
     reports it as a stylesheet MIME-type error rather than a missing file.
     The same function's "/{page}" wildcard also swallows /favicon.ico.

  4. SetupNavigationRoutes's error is discarded. On failure no navigation
     route is registered at all -- not even "/" -- yet the server still
     starts and answers everything with a bare 404 and no log line.

  5. Content-Type for the bundled Static/ assets is taken from the HOST MIME
     database, which overrides Go's built-in table. Hardening rather than a
     fix for anything observed, but it removes the whole class of surprise.

The upstream tree is never written to
-------------------------------------
The source is COPIED to a scratch directory, the copy is patched, and the
build runs from the copy. "Restore the source afterwards" is not a step that
could be skipped or interrupted -- the original is simply never opened for
writing. The final phase proves it, by diffing a size-and-mtime snapshot taken
right after extraction against one taken at the end.

Every patch is anchored on an exact snippet of upstream text and asserted to
apply exactly once. If upstream edits those lines the program fails loudly
rather than quietly shipping an unpatched binary; if upstream ADOPTS one of
the fixes, that patch's own marker is recognised and it is skipped.
"""

import argparse
import contextlib
import fnmatch
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
import importlib.util

_spec = importlib.util.spec_from_file_location("packdist", os.path.join(HERE, "pack-dist.py"))
packdist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(packdist)

ROOT = os.path.dirname(HERE)            # the repository root
EXTRACT = os.path.join(ROOT, "extracted")
DIST = os.path.join(ROOT, "dist")
PKGS = os.path.join(ROOT, "packages")

# The upstream payload root inside `extracted`. Discovered at run time by
# looking for the directory that holds both Code/ and Data/, so a rename or a
# differently-shaped archive does not need a code change.
SRC = None

# Scratch space deliberately lives OUTSIDE the repository. Go splits $CC on
# whitespace, so if the repository sits at a path containing a space -- say
# "...\CBDB DIST\..." -- a shim compiled inside it makes cgo try to exec only
# the first word, e.g. "C:\Users\someone\Desktop\CBDB". The temp directory
# normally has no space, and keeping the scratch tree out of the repository
# means it never has to be gitignored.
def _default_work():
    """A scratch directory whose path contains no space.

    %TEMP% embeds the Windows account name, so for an account like "Jane Doe"
    the default would itself be unusable -- and without this the failure only
    surfaced after minutes of work. Prefer the drive root; fall back to temp.
    """
    drive = os.environ.get("SystemDrive", "")
    bases = ([drive + os.sep] if drive else []) + [tempfile.gettempdir()]
    for base in bases:
        candidate = os.path.join(base, "cbdb-dist-build")
        if not any(c.isspace() for c in candidate):
            return candidate
    return os.path.join(tempfile.gettempdir(), "cbdb-dist-build")


WORK = _default_work()

# ldflags, per target:
#   -s -w    strip the symbol table and DWARF. Go keeps its own pclntab, so
#            panics still name functions; this just halves the binary.
#   -B none  darwin only: skip the Mach-O UUID, whose rewrite step hits an
#            "Access is denied" rename bug when cross-building on Windows.
#            -w is also load-bearing on darwin, because with DWARF enabled Go
#            shells out to Apple's dsymutil, which does not exist here.
TARGETS = {
    "mac-arm64": {"goos": "darwin", "goarch": "arm64", "zig": "aarch64-macos",
                  "label": "Apple Silicon (M1/M2/M3/M4)", "exe": "", "pack": "tar",
                  "ldflags": "-s -w -B none"},
    "mac-x64": {"goos": "darwin", "goarch": "amd64", "zig": "x86_64-macos",
                "label": "Intel", "exe": "", "pack": "tar",
                "ldflags": "-s -w -B none"},
    "win-x64": {"goos": "windows", "goarch": "amd64", "zig": "x86_64-windows-gnu",
                "label": "64-bit Intel/AMD", "exe": ".exe", "pack": "zip",
                "ldflags": "-s -w"},
}

# Payload: what a user actually needs at run time. Everything else in the
# upstream tree is source, build tooling, maintainer tooling or stale backups.
PAYLOAD_DIRS = ["Templates", "Static"]
PAYLOAD_FILES = [os.path.join("Data", "qbe_schema.json")]   # plus the database, named at run time
# Suffixes that are never part of a runtime payload: build scripts, schema
# dumps, Windows icons and shortcuts. Matched on the extension, not as a
# substring, so an asset merely containing ".py" in its name survives.
PAYLOAD_SKIP_EXT = (".ico", ".lnk", ".bat", ".py", ".sql")
# Upstream keeps dated backups beside the live templates, e.g.
# "office.index.20260815.html". Matched by shape rather than by a hardcoded
# year, which would start shipping them in January.
PAYLOAD_SKIP_DATED = re.compile(r"\.\d{8}\.[A-Za-z0-9]+$")
# Root-level entries (of the archive or of the payload) already checked and
# known NOT to be read by the program. Only reported by the audit; nothing
# outside the allow-list above is shipped either way. The .docx are upstream's
# working notes (e.g. the 2026-10 label-revision documents): the label text
# itself is hardcoded in each Templates/*/index.html, which merely cite them in
# comments.
KNOWN_UNSHIPPED = ["Code", "Bin", "CBDBSetUpCode", "CBDBSetUpTemplate",
                   "cbdb.exe", "CBDBsetup.exe", "*.ico", "*.lnk", "*.txt", "*.docx"]

# Where the favicon shipped in the payload comes from. By default it is taken
# from the upstream archive, which is the thing the CBDB project actually
# maintains; the copy generated from LOGO.png by make-favicon.py is the
# fallback for an upstream that ships none. Resolved at run time by
# resolve_icon() and stored here as (source path, path inside the payload).
FALLBACK_ICON = os.path.join(HERE, "assets", "favicon.ico")
EXTRA_ASSETS = []


# ---------------------------------------------------------------------------
# Settings: .env, then the environment, then the command line
# ---------------------------------------------------------------------------

ENV_FILE = os.path.join(ROOT, ".env")


def load_env(path=None):
    """Read KEY=VALUE lines from .env into a dict.

    Deliberately not python-dotenv: this is the whole feature, and the build
    should not grow a dependency for it. Real environment variables win, so a
    CI job can override a developer's .env without editing it.
    """
    path = path or ENV_FILE
    values = {}
    if os.path.isfile(path):
        # utf-8-sig, not utf-8: PowerShell's > and Notepad both write a BOM by
        # default, and it would otherwise become part of the first key -- so
        # the setting is silently ignored, or a comment line fails to parse.
        with open(path, encoding="utf-8-sig") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    raise SystemExit("%s:%d: expected KEY=VALUE, got %r"
                                     % (path, lineno, raw.rstrip()))
                key, _, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    # Quoted: take it literally, minus one layer of quotes. A
                    # Windows path is far likelier to be quoted than to want
                    # the quotes kept.
                    value = value[1:-1]
                elif " #" in value:
                    # Unquoted: honour a trailing comment. .env.sample teaches a
                    # comment-heavy style, and "true  # default" would otherwise
                    # fail to parse as a boolean. Only " #" counts, so a path
                    # containing a bare # survives.
                    value = value.split(" #", 1)[0].strip()
                values[key.strip()] = value
    # Real environment variables win, so CI can override without editing a
    # file -- but an EMPTY one does not, or an unset CI secret would silently
    # erase the .env value and send the build looking elsewhere.
    values.update({k: v for k, v in os.environ.items()
                   if k.startswith("CBDB_") and v != ""})
    return values


def env_flag(values, key, default):
    raw = values.get(key)
    if raw is None or raw.strip() == "":
        return default
    if raw.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if raw.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise SystemExit("%s must be true or false, got %r" % (key, raw))


# ---------------------------------------------------------------------------
# The patches
# ---------------------------------------------------------------------------

APP_ROOT_HELPER = '''// appRoot returns the directory holding this executable, which in a
// distribution is where Data/, Templates/ and Static/ sit. Finder starts a
// program with the user's home directory as the working directory, so
// resolving the defaults against the executable instead of the working
// directory is what lets a double-click work. On failure it returns "", and
// filepath.Join("", "Data", ...) == "Data/...", i.e. exactly the original
// working-directory-relative behaviour.
func appRoot() string {
\texe, err := os.Executable()
\tif err != nil {
\t\treturn ""
\t}
\tif resolved, err := filepath.EvalSymlinks(exe); err == nil {
\t\texe = resolved
\t}
\treturn filepath.Dir(exe)
}

'''


MIME_FIX_GO = '''package main

// Register the MIME types this program serves instead of trusting the host to
// know them.
//
// net/http derives Content-Type for a static file from mime.TypeByExtension,
// which consults the host MIME database: the registry on Windows, and on Unix
// the first that exists of /etc/mime.types, /etc/apache2/mime.types,
// /etc/apache/mime.types, /etc/httpd/conf/mime.types. Those files OVERRIDE
// Go's built-in table, so a host whose database disagrees about .css makes the
// server mislabel its own bundled stylesheet, and the browser then ignores it
// under strict MIME checking.
//
// This is hardening, not a fix for anything observed. The "MIME type
// ('text/plain')" error that prompted it turned out to be a 404 in disguise --
// a shadowed /static/ route, fixed separately in main.go -- because Go answers
// 404 with a text/plain body, which is what the browser complained about.
// Registering the types anyway costs nothing and removes the host's MIME
// database from the picture entirely.
//
// mime.AddExtensionType runs the mime package's lazy init before setting the
// value, so these registrations win over whatever the host said.
//
// This file only adds; it modifies no upstream line.

import "mime"

func init() {
\tfor ext, typ := range map[string]string{
\t\t".css":   "text/css; charset=utf-8",
\t\t".js":    "text/javascript; charset=utf-8",
\t\t".mjs":   "text/javascript; charset=utf-8",
\t\t".json":  "application/json",
\t\t".html":  "text/html; charset=utf-8",
\t\t".svg":   "image/svg+xml",
\t\t".woff":  "font/woff",
\t\t".woff2": "font/woff2",
\t\t".pdf":   "application/pdf",
\t\t".ico":   "image/x-icon",
\t} {
\t\t// Fails only on a malformed extension or media type, both constant here.
\t\t_ = mime.AddExtensionType(ext, typ)
\t}
}
'''


class Addition:
    """A whole new file added to the package -- no upstream line is touched.

    `marker` is text whose presence anywhere in the upstream sources means the
    problem is already handled, so the audit does not report it twice.
    """

    def __init__(self, name, why, filename, content, marker):
        self.name, self.why = name, why
        self.filename, self.content, self.marker = filename, content, marker


ADDITIONS = [
    Addition(
        "explicit-mime-types",
        "Content-Type for the bundled Static/ assets is taken from the host MIME "
        "database, which overrides Go's built-in table (hardening, not a fix for "
        "anything observed -- see static-route-order)",
        "zz_mime_types.go", MIME_FIX_GO, "mime.AddExtensionType"),
]


class Patch:
    """One anchored edit: `old` must appear exactly `count` times in the file.

    `marker` is a distinctive fragment of the result. Its presence means this
    particular fix is already in the source, so the patch is a no-op rather
    than a failure -- which is what lets the kit keep working after upstream
    adopts one of the fixes. Each patch gets its OWN marker; a shared test
    would let one adopted fix mask another whose anchor merely drifted.
    """

    def __init__(self, name, why, path, old, new, marker, count=1,
                 prelude="", prelude_marker=""):
        self.name, self.why, self.path = name, why, path
        self.old, self.new, self.count = old, new, count
        self.marker = marker
        # `prelude` is inserted ahead of the replacement, but only if
        # `prelude_marker` is absent -- so upstream adding its own copy of a
        # helper does not produce two definitions of it.
        self.prelude, self.prelude_marker = prelude, prelude_marker

    def staged_name(self):
        """stage() flattens Code/ into the scratch directory."""
        return os.path.basename(self.path)

    def find(self, text):
        return text.count(self.old)

    def already_applied(self, text):
        return self.marker in text

    def apply(self, text):
        n = self.find(text)
        if n == self.count:
            new = self.new
            if self.prelude and self.prelude_marker not in text:
                new = self.prelude + new
            return text.replace(self.old, new)
        if n == 0 and self.already_applied(text):
            return text                       # upstream adopted it; nothing to do
        raise SystemExit(
            "patch %r no longer applies: expected %d occurrence(s) of the anchor in %s, "
            "found %d, and the fix is not present either.\nUpstream has changed these "
            "lines; update build-dist.py before shipping.\n--- anchor ---\n%s"
            % (self.name, self.count, self.path, n, self.old))


def patches(db_filename):
    main_go = os.path.join("Code", "main.go")
    return [
        # One patch, not two: the schema default needs `root` in scope, and
        # `root` exists only because this same replacement declares it. Split
        # across two patches, skipping the first (because upstream adopted it
        # in some other shape) left the second producing `undefined: root`.
        Patch(
            "exec-relative-asset-paths",
            "Data/, Templates/, Static/ and the QBE schema are resolved against "
            "the working directory, so a Finder double-click cannot find them",
            main_go,
            "func parseFlags() AppConfig {\n"
            "\tcfg := AppConfig{}\n"
            '\tflag.StringVar(&cfg.DBPath, "db", filepath.Join("Data", "CBDB.db"),\n'
            '\t\t"Path to the SQLite database file")\n'
            '\tflag.StringVar(&cfg.TemplatePath, "templates", filepath.Join("Templates", "*.html"),\n'
            '\t\t"Glob pattern for HTML templates")\n'
            '\tflag.StringVar(&cfg.StaticPath, "static", "Static",\n'
            '\t\t"Path to the static assets directory")\n'
            '\tflag.StringVar(&cfg.Port, "port", "0",\n'
            '\t\t"HTTP listen port (0 = pick a free port automatically)")\n'
            '\tflag.StringVar(&cfg.SchemaPath, "schema", filepath.Join("Data", "qbe_schema.json"),',

            "func parseFlags() AppConfig {\n"
            "\tcfg := AppConfig{}\n"
            "\troot := appRoot()\n"
            '\tflag.StringVar(&cfg.DBPath, "db", filepath.Join(root, "Data", "%s"),\n'
            '\t\t"Path to the SQLite database file")\n'
            '\tflag.StringVar(&cfg.TemplatePath, "templates", filepath.Join(root, "Templates", "*.html"),\n'
            '\t\t"Glob pattern for HTML templates")\n'
            '\tflag.StringVar(&cfg.StaticPath, "static", filepath.Join(root, "Static"),\n'
            '\t\t"Path to the static assets directory")\n'
            '\tflag.StringVar(&cfg.Port, "port", "0",\n'
            '\t\t"HTTP listen port (0 = pick a free port automatically)")\n'
            '\tflag.StringVar(&cfg.SchemaPath, "schema", filepath.Join(root, "Data", "qbe_schema.json"),'
            % db_filename,
            marker='filepath.Join(root, "Static")',
            prelude=APP_ROOT_HELPER,
            prelude_marker="func appRoot()",
        ),
        Patch(
            "static-route-order",
            "/static/ is registered TWICE -- here with the configured path, and "
            "earlier inside SetupNavigationRoutes with a hardcoded \"./Static/\". "
            "gorilla/mux matches in registration order, so the hardcoded one wins "
            "and every asset 404s unless the process happens to be started from "
            "the program's own directory. /favicon.ico is also swallowed by that "
            "function's \"/{page}\" wildcard",
            main_go,
            "\t// Form routes must be registered before the navigation wildcard.\n"
            "\tregisterFormRoutes(db, cfg.DBPath, schema, r, cfg.TemplatePath)\n"
            "\tSetupNavigationRoutes(db, r, cfg.TemplatePath)\n"
            "\n"
            "\t// Static assets (CSS, JS, images).\n"
            '\tr.PathPrefix("/static/").Handler(\n'
            '\t\thttp.StripPrefix("/static/", http.FileServer(http.Dir(cfg.StaticPath))))\n',

            "\t// Form routes must be registered before the navigation wildcard.\n"
            "\tregisterFormRoutes(db, cfg.DBPath, schema, r, cfg.TemplatePath)\n"
            "\n"
            "\t// Static assets (CSS, JS, images), plus /favicon.ico, which browsers\n"
            "\t// request at the site root rather than under /static/.\n"
            "\t//\n"
            "\t// Both MUST be registered before SetupNavigationRoutes, because that\n"
            "\t// function registers its own /static/ handler hardcoded to \"./Static/\"\n"
            "\t// -- relative to the working directory -- and a \"/{page}\" wildcard that\n"
            "\t// matches any single-segment path, /favicon.ico included. gorilla/mux\n"
            "\t// matches in registration order, so whichever is registered first wins;\n"
            "\t// registering these first makes the ones in there unreachable, which is\n"
            "\t// what lets -static point anywhere and lets the program run from any\n"
            "\t// directory.\n"
            '\tr.PathPrefix("/static/").Handler(\n'
            '\t\thttp.StripPrefix("/static/", http.FileServer(http.Dir(cfg.StaticPath))))\n'
            '\tr.HandleFunc("/favicon.ico",\n'
            '\t\tserveStaticFile(cfg.StaticPath, "favicon.ico")).Methods("GET")\n'
            "\n"
            "\t// Do not discard this error. On failure no navigation route is\n"
            "\t// registered at all -- not even \"/\" -- and yet the server still\n"
            "\t// starts, so every page answers with a bare text/plain 404 and\n"
            "\t// nothing is logged. Unlike the database, Templates/ gets no\n"
            "\t// existence check of its own, so this is where a wrong template\n"
            "\t// root actually surfaces.\n"
            "\tif err := SetupNavigationRoutes(db, r, cfg.TemplatePath); err != nil {\n"
            '\t\tlog.Fatalf("Failed to register navigation routes: %v", err)\n'
            "\t}\n",
            marker='serveStaticFile(cfg.StaticPath, "favicon.ico")',
        ),
        Patch(
            "database-not-found-hint",
            'the "run from the project root" hint is wrong once paths are '
            "resolved against the executable",
            main_go,
            'log.Fatalf("Database file not found: %s\\nRun the server from the '
            'CBDB-GO project root directory.", cfg.DBPath)',
            'log.Fatalf("Database file not found: %s\\nIt should sit in Data/ next to '
            'this executable; pass -db to point somewhere else.", cfg.DBPath)',
            marker="It should sit in Data/ next to",
        ),
    ]


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def say(phase, msg=""):
    _out(("\n== %s ==" % phase) if not msg else "   %s" % msg)


def _out(line):
    """print(), but never raising because of the console code page.

    Windows encodes stdout with the active code page unless UTF-8 mode is on,
    so one non-representable character in a path or a tool's error text would
    raise UnicodeEncodeError -- possibly after archives had already been
    published, skipping the provenance stamp.
    """
    enc = sys.stdout.encoding or "ascii"
    print(line.encode(enc, "replace").decode(enc, "replace"))


def find_archive(explicit=None):
    """The upstream release archive.

    Either named outright -- by --archive or CBDB_ARCHIVE in .env, which can
    point anywhere, so a multi-hundred-megabyte input need not be copied into
    the repository -- or the single .7z/.zip sitting in the repository root.
    Nothing here names a version or a folder.
    """
    if explicit:
        # Anchored to the repository, not the current directory: every other
        # path here is, and the script is meant to run from anywhere.
        # os.path.join leaves an absolute path alone.
        explicit = os.path.join(ROOT, os.path.expanduser(explicit))
        if not os.path.isfile(explicit):
            raise SystemExit("no such archive: %s" % explicit)
        return os.path.abspath(explicit)
    root = glob.escape(ROOT)    # "[" and "]" are legal in Windows paths
    found = sorted(glob.glob(os.path.join(root, "*.7z")) +
                   glob.glob(os.path.join(root, "*.zip")))
    if not found:
        raise SystemExit("no .7z or .zip archive in %s\n"
                         "Drop the upstream release archive there, set CBDB_ARCHIVE in "
                         ".env, or pass --archive." % ROOT)
    if len(found) > 1:
        raise SystemExit("more than one archive in %s:\n  %s\nPass --archive to choose."
                         % (ROOT, "\n  ".join(os.path.basename(f) for f in found)))
    return found[0]


def _stamp(archive):
    """Content identity of the archive, for the extraction cache.

    Name, size and a whole-second mtime are not enough: swapping in a different
    release of the same size with a preserved timestamp would silently reuse
    the old extracted tree, and every later check would pass against stale
    content. Hashing a few hundred megabytes costs a second or two, against a
    1.3 GB extraction.
    """
    st = os.stat(archive)
    return "%s\n%d\n%s\n" % (os.path.basename(archive), st.st_size, sha256_of(archive))


def _extractor(archive):
    """Resolve how to read this archive, before anything is destroyed.

    Everything that can fail for a bad or unsupported archive -- an unknown
    extension, a missing py7zr -- is settled here, while the previous
    extraction is still on disk.
    """
    lower = archive.lower()
    if lower.endswith(".7z"):
        try:
            import py7zr
        except ImportError:
            raise SystemExit("py7zr is needed to read a .7z archive:\n"
                             "  python -m pip install -r requirements.txt")

        def unpack(dest):
            with py7zr.SevenZipFile(archive, "r") as zf:
                # zipfile sanitises member paths; py7zr does not, so check
                # before writing anything.
                for entry in zf.getnames():
                    if os.path.isabs(entry) or ".." in entry.replace("\\", "/").split("/"):
                        raise SystemExit("%s contains an unsafe member path: %s"
                                         % (os.path.basename(archive), entry))
                zf.extractall(path=dest)
        return unpack
    if lower.endswith(".zip"):
        import zipfile

        def unpack(dest):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
        return unpack
    raise SystemExit("do not know how to read %s -- expected .7z or .zip"
                     % os.path.basename(archive))


def extract(archive, force=False):
    """Unpack the archive into `extracted`, skipping the work if already done.

    The payload is well over a gigabyte, so re-extracting on every run would
    dominate the build. A stamp file records which archive produced the current
    contents; extraction is redone when the stamp does not match.

    Unpacking goes to a sibling directory which is swapped in only on success,
    so a failure part-way leaves the previous extraction intact.
    """
    say("extract")
    marker = os.path.join(EXTRACT, ".extracted-from")
    want = _stamp(archive)
    if not force and os.path.isfile(marker):
        with open(marker, encoding="utf-8") as fh:
            if fh.read() == want:
                say("", "%s already unpacked (stamp matches)" % os.path.basename(archive))
                return
        say("", "stamp differs; re-extracting")

    unpack = _extractor(archive)
    staging = EXTRACT + ".incoming"
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)

    say("", "%s -> %s" % (os.path.basename(archive), EXTRACT))
    try:
        unpack(staging)
        n = sum(len(f) for _, _, f in os.walk(staging))
        with open(os.path.join(staging, ".extracted-from"), "w", encoding="utf-8") as fh:
            fh.write(want)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # os.replace cannot overwrite an existing directory on Windows, so the
    # old tree has to go first. Move it aside rather than deleting it, so no
    # moment has neither a good old tree nor the new one: a failure during a
    # 1.3 GB rmtree (a file held open by a running cbdb.exe, antivirus) would
    # otherwise leave a half-deleted `extracted` that the next run discards.
    previous = EXTRACT + ".old"
    if os.path.isdir(previous):
        shutil.rmtree(previous, ignore_errors=True)
    moved = False
    if os.path.isdir(EXTRACT):
        os.replace(EXTRACT, previous)
        moved = True
    try:
        os.replace(staging, EXTRACT)
    except BaseException:
        # Put the old tree back rather than leaving `extracted` absent with the
        # last good copy stranded under .old, which nothing looks for.
        if moved:
            os.replace(previous, EXTRACT)
        raise
    shutil.rmtree(previous, ignore_errors=True)
    say("", "%d files" % n)


def discover_payload(explicit=None):
    """The directory inside `extracted` that holds both Code/ and Data/.

    Matched case-insensitively: an archive built on a case-sensitive
    filesystem may well store `code/`, and Windows would resolve it fine.
    """
    if explicit:
        if not os.path.isdir(explicit):
            raise SystemExit("no such directory: %s" % explicit)
        say("", "payload root: %s (given)" % explicit)
        return os.path.abspath(explicit)
    candidates = []
    for dirpath, dirnames, _ in os.walk(EXTRACT):
        lowered = {d.lower() for d in dirnames}
        if {"code", "data"} <= lowered:
            candidates.append(dirpath)
            dirnames[:] = []                 # do not descend past a match
    if not candidates:
        raise SystemExit("no directory with both Code/ and Data/ under %s -- is this the "
                         "right archive?" % EXTRACT)
    if len(candidates) > 1:
        raise SystemExit("several candidate payload roots under %s:\n  %s\n"
                         "Pass --payload-root to choose."
                         % (EXTRACT, "\n  ".join(candidates)))
    say("", "payload root: %s" % os.path.relpath(candidates[0], ROOT))
    return candidates[0]


def resolve_icon(use_input, wanted):
    """Decide which .ico becomes Static/favicon.ico, and say so out loud.

    An icon silently coming from the wrong place is the kind of thing nobody
    notices until a release is out, so both the choice and the reason are
    printed.
    """
    say("icon")
    if SRC is None:
        raise SystemExit("resolve_icon() ran before the payload was discovered")
    if use_input:
        # isfile matters: os.listdir happily returns a DIRECTORY called
        # something.ico, and copying it later dies with a bare PermissionError
        # after the whole cross-build has run.
        # Not a symlink: os.path.isfile follows one, so a link in the payload
        # root could quietly ship a file from outside the upstream archive
        # while the log claims it came from upstream.
        candidates = sorted(n for n in os.listdir(SRC)
                            if n.lower().endswith(".ico")
                            and os.path.isfile(os.path.join(SRC, n))
                            and not os.path.islink(os.path.join(SRC, n)))
        if wanted:
            match = [n for n in candidates if n.lower() == wanted.lower()]
            if not match:
                raise SystemExit(
                    "CBDB_INPUT_ICON is %r but the payload holds %s.\n"
                    "Fix the name, or set CBDB_USE_INPUT_ICON=false to use the "
                    "committed fallback." % (wanted, ", ".join(candidates) or "no .ico at all"))
            chosen = match[0]
        elif len(candidates) == 1:
            chosen = candidates[0]
        elif not candidates:
            raise SystemExit("CBDB_USE_INPUT_ICON is true but the payload holds no .ico.\n"
                             "Set it to false, or point CBDB_INPUT_ICON at one.")
        else:
            raise SystemExit("CBDB_USE_INPUT_ICON is true and the payload holds several .ico "
                             "files:\n  %s\nName one with CBDB_INPUT_ICON."
                             % "\n  ".join(candidates))
        source = os.path.join(SRC, chosen)
        say("", "from the upstream archive: %s" % chosen)
    else:
        source = FALLBACK_ICON
        if not os.path.isfile(source):
            raise SystemExit("%s is missing; generate it with:\n  python %s --preview"
                             % (source, os.path.join(HERE, "make-favicon.py")))
        say("", "from this repository: %s" % os.path.relpath(source, ROOT))
    # Read it here. Otherwise an unreadable file is only discovered by
    # shutil.copy2 in assemble(), after every target has been cross-compiled.
    try:
        with open(source, "rb") as fh:
            fh.read(1)
    except OSError as e:
        raise SystemExit("cannot read the chosen icon %s: %s" % (source, e))
    return [(source, os.path.join("Static", "favicon.ico"))]


def actual_db_filename():
    """The database file as it is really named on disk, for the case check."""
    names = [n for n in os.listdir(os.path.join(SRC, "Data")) if n.lower() == "cbdb.db"]
    if not names:
        raise SystemExit("no cbdb.db in %s" % os.path.join(SRC, "Data"))
    return names[0]


def audit(db_filename):
    """Report what is wrong upstream; return the set of fixes already present.

    Each patch is classified by its OWN marker. A shared test -- "does
    appRoot() exist anywhere?" -- would report every patch whose anchor merely
    drifted as already fixed, which is a false clean bill of health.
    """
    say("audit")
    main_go = os.path.join(SRC, "Code", "main.go")
    # Default newline translation is load-bearing: upstream is CRLF and every
    # patch anchor is written with LF. Do not "harden" this to newline="".
    with open(main_go, encoding="utf-8") as fh:
        text = fh.read()
    problems, already_fixed, n_patches, n_additions = 0, set(), 0, 0

    for p in patches(db_filename):
        if p.find(text) == p.count:
            problems += 1
            n_patches += 1
            _out("   PROBLEM  %-28s %s" % (p.name, p.why))
        elif p.find(text) == 0 and p.already_applied(text):
            already_fixed.add(p.name)
            _out("   ok       %-28s already fixed upstream" % p.name)
        else:
            raise SystemExit(
                "cannot classify %r in %s: the anchor matched %d time(s) (expected %d) "
                "and the fix marker %r is absent.\nUpstream has changed; review "
                "build-dist.py." % (p.name, main_go, p.find(text), p.count, p.marker))

    # Only the files stage() copies: a marker present solely in a _test.go
    # would otherwise report a fix as handled upstream when the built package
    # does not contain it.
    sources = ""
    for n in os.listdir(os.path.join(SRC, "Code")):
        if n.endswith(".go") and not n.endswith("_test.go"):
            with open(os.path.join(SRC, "Code", n), encoding="utf-8") as fh:
                sources += fh.read()
    for a in ADDITIONS:
        if a.marker in sources:
            already_fixed.add(a.name)
            _out("   ok       %-28s already handled upstream" % a.name)
        else:
            problems += 1
            n_additions += 1
            _out("   PROBLEM  %-28s %s" % (a.name, a.why))

    # Not a patch of its own: the exec-relative patch writes the real filename
    # into the default, so fixing that one fixes this too. Read the literal
    # rather than testing for one spelling of it, or a differently-cased
    # default would be reported as agreeing when it does not.
    findings = problems
    m = re.search(r'filepath\.Join\("Data", "([^"]+\.db)"\)', text)
    declared = m.group(1) if m else None
    if declared is None:
        _out("   ?        %-28s no Data/*.db default found in parseFlags" % "database-filename-case")
    elif declared != db_filename:
        findings += 1
        _out("   PROBLEM  %-28s source says Data/%s, the file on disk is Data/%s"
              % ("database-filename-case", declared, db_filename))
        _out("   %-37s fixed by exec-relative-asset-paths, which writes the real name" % "")
    else:
        _out("   ok       %-28s source and file agree (%s)" % ("database-filename-case", db_filename))

    _out("\n   %d finding(s); %d patch(es) and %d added file(s) to apply"
          % (findings, n_patches, n_additions))
    report_unshipped()
    return problems, already_fixed


def report_unshipped():
    """List what sits at the archive root and the payload root but is not shipped.

    assemble() is an allow-list, so nothing here reaches a distribution. The
    risk runs the other way: upstream adds something at the root that the
    program DOES read at run time, and the build quietly leaves it out. Hence
    entries not in KNOWN_UNSHIPPED are marked NEW, for a human to check
    against Code/ before shipping. Informational only; it never fails a run.
    """
    shipped = {d.lower() for d in PAYLOAD_DIRS} | {"data"}
    rows = []
    for base in dict.fromkeys((EXTRACT, SRC)):   # the same directory if the archive has no top folder
        for n in sorted(os.listdir(base)):
            path = os.path.join(base, n)
            if n.startswith(".") or os.path.normcase(path) == os.path.normcase(SRC):
                continue
            if base == SRC and n.lower() in shipped:
                continue
            rel = os.path.relpath(path, EXTRACT).replace(os.sep, "/") + ("/" if os.path.isdir(path) else "")
            known = any(fnmatch.fnmatch(n.lower(), k.lower()) for k in KNOWN_UNSHIPPED)
            rows.append(("not shipped" if known else "NEW, check", rel))
    if not rows:
        return
    _out("\n   outside the payload (not shipped):")
    for tag, rel in rows:
        _out("   %-11s  %s" % (tag, rel))
    if any(tag != "not shipped" for tag, _ in rows):
        _out("   -> grep Code/ and Templates/ for each NEW entry; if the program reads it,\n"
             "      add it to PAYLOAD_DIRS/PAYLOAD_FILES, otherwise to KNOWN_UNSHIPPED.")


def stage():
    """Copy the Go module to scratch. The upstream tree is only ever read."""
    say("stage source (upstream stays read-only)")
    dst = os.path.join(WORK, "src")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)
    # A flat package of .go files is all this copies. Anything else -- vendor/,
    # embedded assets, a subpackage, .c/.s sources -- would be dropped
    # silently and change what gets built.
    unexpected = sorted(e for e in os.listdir(os.path.join(SRC, "Code"))
                        if os.path.isdir(os.path.join(SRC, "Code", e))
                        or not (e.endswith(".go") or e in ("go.mod", "go.sum")
                                or e.endswith((".bat", ".ps1", ".md", ".txt"))))
    if unexpected:
        raise SystemExit("Code/ holds entries this kit does not know how to stage: %s\n"
                         "Upstream has changed shape; update stage() before shipping."
                         % ", ".join(unexpected))
    n = 0
    for name in sorted(os.listdir(os.path.join(SRC, "Code"))):
        if name.endswith(".go") or name in ("go.mod", "go.sum"):
            if name.endswith("_test.go"):
                continue                      # tests reach for the project root; not built here
            # Normalise to LF while copying. Upstream is CRLF, and gofmt counts
            # every CRLF line as a difference -- which would swamp the
            # before/after comparison that checks the patches for formatting
            # regressions, and make it tolerate anything.
            with open(os.path.join(SRC, "Code", name), encoding="utf-8") as fh:
                text = fh.read()
            with open(os.path.join(dst, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            n += 1
    say("", "%d files -> %s" % (n, dst))
    return dst


def patch(staged, db_filename, already_fixed=()):
    say("patch the copy")
    # Measure the formatting baseline on the STAGED file, which stage() has
    # normalised to LF, so the comparison at the end reflects only what the
    # patches did.
    before = gofmt_diff_lines(os.path.join(staged, "main.go"))
    for p in patches(db_filename):
        if p.name in already_fixed:
            say("", "skipped %s (already fixed upstream)" % p.name)
            continue
        f = os.path.join(staged, p.staged_name())
        text = open(f, encoding="utf-8").read()
        open(f, "w", encoding="utf-8", newline="\n").write(p.apply(text))
        say("", "applied %s" % p.name)
    for a in ADDITIONS:
        if a.name in already_fixed:
            say("", "skipped %s (already handled upstream)" % a.name)
            continue
        f = os.path.join(staged, a.filename)
        if os.path.exists(f):
            raise SystemExit("%s already exists in the staged package; upstream now ships a file "
                             "by that name, so rename the addition in build-dist.py" % a.filename)
        open(f, "w", encoding="utf-8", newline="\n").write(a.content)
        say("", "added %s (%s)" % (a.filename, a.name))
    gofmt_check_addition(staged)
    # Most of the upstream files, main.go among them, already fail gofmt (space
    # indentation in places), so an absolute "gofmt -l" check would be pure
    # noise. What matters is whether the patches made main.go worse.
    after = gofmt_diff_lines(os.path.join(staged, "main.go"))
    if after > before:
        raise SystemExit("the patches introduced %d new gofmt complaint(s) in main.go "
                         "(%d before, %d after); fix the patch text"
                         % (after - before, before, after))
    say("", "gofmt: no regression in main.go (%d diff line(s) before, %d after)"
        % (before, after))


def gofmt_check_addition(staged):
    """Files we author ourselves have no excuse: they must be gofmt-clean."""
    for a in ADDITIONS:
        n = gofmt_diff_lines(os.path.join(staged, a.filename))
        if n:
            out = capture(["gofmt", "-d", os.path.join(staged, a.filename)])
            raise SystemExit("%s is not gofmt-clean (%d diff line(s)):\n%s"
                             % (a.filename, n, out.stdout))
    say("", "gofmt: added file(s) clean")


def gofmt_diff_lines(path):
    """How many lines gofmt would change in `path`.

    The exit status matters: for a file that does not parse, gofmt writes the
    error to stderr, exits non-zero and leaves stdout empty -- which would come
    back as "0 lines to change", i.e. a clean bill of health for broken Go, on
    both the regression gate and the added-file check.
    """
    out = capture(["gofmt", "-d", path])
    # 0 = already formatted, 1 = differences found (the normal case here),
    # 2 = could not parse. Only the last is a failure: treating 1 as one made
    # every run abort, and ignoring 2 was what let broken Go through.
    if out.returncode not in (0, 1):
        raise SystemExit("gofmt could not parse %s (exit %d):\n%s"
                         % (path, out.returncode, out.stderr.strip()))
    return sum(1 for line in out.stdout.splitlines()
               if line[:1] in ("+", "-") and not line.startswith(("+++", "---")))


def check_work_path():
    """$CC must not contain a space; refuse before doing any work, not after."""
    if any(c.isspace() for c in WORK):
        raise SystemExit(
            "the scratch directory contains whitespace:\n  %s\n"
            "Go splits $CC on whitespace, so cgo would try to exec only the first word.\n"
            "Pass --work with a whitespace-free directory." % WORK)


def shim():
    """Build the CC shims that front `zig cc`.

    Their zig path, target and stub directory arrive through the environment
    rather than -ldflags -X: a -X value containing a space is split by the
    linker's flag parser, which corrupts the build with no obvious error, and
    this repository may well live under such a path.

    Two shims, differing only in CCWRAP_MODE, so $CXX really is a C++ driver.
    go-sqlite3 is C-only today, but a C++ translation unit anywhere in the
    dependency tree would otherwise be compiled by `zig cc` and link without
    libc++.
    """
    say("cc shims")
    check_work_path()
    env = dict(os.environ, CGO_ENABLED="0")
    env.pop("GOOS", None)
    env.pop("GOARCH", None)
    made = {}
    for mode, exe_name in (("cc", "cc.exe"), ("c++", "cxx.exe")):
        exe = os.path.join(WORK, exe_name)
        # -X is safe here only because the value has no space in it; the zig
        # path and stub directory go through the environment for that reason.
        run(["go", "build", "-ldflags", "-X main.mode=" + mode, "-o", exe, "."],
            cwd=os.path.join(HERE, "ccwrap"), env=env)
        made[mode] = exe
        say("", "%-4s %s" % (mode, exe))
    return made


def find_zig():
    """zig.exe, from PATH or from where winget unpacked it.

    PATH wins: someone who installed a newer zig by hand or with scoop should
    get theirs. winget only adds its shim directory to PATH for newly started
    shells, so its package directory is the fallback.
    """
    found = shutil.which("zig")
    if found:
        return os.path.realpath(found)
    pkgs = glob.escape(os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                    "Microsoft", "WinGet", "Packages"))

    def version_key(path):
        # "zig-x86_64-windows-0.16.0" must sort above "...-0.9.1", which it
        # does not do as a plain string.
        nums = re.findall(r"\d+", os.path.basename(os.path.dirname(path)))
        return [int(n) for n in nums[-3:]] or [0]

    for pattern in (os.path.join(pkgs, "zig.zig_*", "zig-*", "zig.exe"),
                    os.path.join(pkgs, "zig.zig_*", "zig.exe")):
        hits = glob.glob(pattern)
        if hits:
            return max(hits, key=version_key)
    return None


def require_tools(explicit_zig=None):
    """Resolve the whole toolchain, or fail -- before extracting 1.3 GB.

    go and gofmt are checked unconditionally: passing --zig says nothing about
    them, and without this the first sign of a missing Go is a raw traceback
    out of subprocess, after the extraction has already run.
    """
    missing = [t for t in ("go", "gofmt") if not shutil.which(t)]
    if missing:
        raise SystemExit("not on PATH: %s\nInstall Go from https://go.dev/dl/ -- it provides "
                         "both." % ", ".join(missing))
    if explicit_zig:
        if not os.path.isfile(explicit_zig):
            raise SystemExit("no such file: %s" % explicit_zig)
        return explicit_zig
    zig = find_zig()
    if not zig:
        raise SystemExit("zig not found. Install it with 'winget install zig.zig', or pass --zig "
                         "<path to zig.exe>. (winget puts it under %LOCALAPPDATA%\\Microsoft\\"
                         "WinGet\\Packages and only adds it to PATH for new shells.)")
    return zig


def capture(cmd, cwd=None, env=None):
    """Run cmd and capture its output as UTF-8.

    text=True alone decodes with the locale codec, which on this machine is
    cp1252; the upstream sources contain em-dashes, so gofmt's diff is not
    decodable that way and the reader thread dies.
    """
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                          encoding="utf-8", errors="replace")


def run(cmd, cwd=None, env=None):
    r = capture(cmd, cwd=cwd, env=env)
    if r.returncode != 0:
        # Through _out: capture() decodes with errors="replace", and U+FFFD is
        # exactly what a legacy console code page cannot encode -- so writing
        # a compiler diagnostic raw would replace it with a traceback.
        _out(r.stdout.rstrip())
        _out(r.stderr.rstrip())
        raise SystemExit("command failed (%d): %s" % (r.returncode, " ".join(cmd[:3])))
    return r


def build(staged, name, shims, zig):
    t = TARGETS[name]
    out = os.path.join(WORK, "bin-%s" % name, "cbdb" + t["exe"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # CC and CXX point at two separately-built shims, each baking in its own
    # mode, so a stray CCWRAP_MODE in the environment cannot make the C++
    # driver compile as C.
    env = dict(os.environ,
               CGO_ENABLED="1", GOOS=t["goos"], GOARCH=t["goarch"],
               CC=shims["cc"], CXX=shims["c++"],
               CCWRAP_ZIG=zig, CCWRAP_TARGET=t["zig"],
               CCWRAP_STUB=os.path.join(HERE, "macstub"),
               ZIG_GLOBAL_CACHE_DIR=os.path.join(WORK, "zigcache"))
    env.pop("CCWRAP_MODE", None)
    run(["go", "build", "-tags", "sqlite_fts5", "-ldflags", t["ldflags"], "-o", out, "."],
        cwd=staged, env=env)
    return out


LAUNCHER = '''#!/bin/sh
# Double-clickable launcher for CBDB-Desktop.

# If this folder was unpacked by double-clicking the .tar.gz, Archive Utility
# stamped every extracted file with com.apple.quarantine, and Gatekeeper will
# refuse to run an un-notarized binary that carries it. Clearing it here --
# before starting cbdb -- means the single approval the user already gave to
# open this script is the only one needed; later double-clicks are silent.
# Unpacking with `tar xzf` never sets the flag, in which case this is a no-op.
cd "$(dirname "$0")" || exit 1
xattr -dr com.apple.quarantine . 2>/dev/null

# Restore the execute bit if this folder travelled through a filesystem that
# cannot record one (a Windows zip, a FAT32 stick, some network shares).
[ -x ./cbdb ] || chmod +x ./cbdb 2>/dev/null

exec ./cbdb "$@"
'''


def _restore(previous, final):
    """Put the previous folder back. True if it worked."""
    try:
        os.replace(previous, final)
        return True
    except OSError:
        return False


def _in_use(path):
    """Why a directory swap failed, in terms someone can act on.

    A raw WinError 32 names the path but not the cause, and the cause is almost
    always a cbdb.exe left running by a smoke test in that very folder.
    """
    return ("Something is using %s.\n"
            "A cbdb.exe left running from an earlier smoke test is the usual "
            "cause -- check with:  Get-Process cbdb" % path)


def require_assets():
    """EXTRA_ASSETS is filled in at run time by resolve_icon().

    verify() compares the archive against a manifest of the folder that was
    just assembled, so an empty EXTRA_ASSETS would produce a distribution
    with no favicon that still verifies as PRISTINE.
    """
    if not EXTRA_ASSETS:
        raise SystemExit("no assets resolved; resolve_icon() did not run")


def _skip(dirpath, names):
    """copytree ignore-list: drop non-payload FILES, never whole directories.

    Applied to directory names too if it matched on substrings, which is how a
    subdirectory called e.g. "pickers.bat-templates" would silently vanish
    along with everything in it.
    """
    out = []
    for n in names:
        if os.path.isdir(os.path.join(dirpath, n)):
            continue
        if n.lower().endswith(PAYLOAD_SKIP_EXT) or PAYLOAD_SKIP_DATED.search(n):
            out.append(n)
    if out:
        # Logged because the date pattern is a heuristic: a live asset named
        # like a content hash (app.12345678.js) would match, and a silently
        # missing template produces a distribution that cannot start.
        say("", "skipped in %s: %s" % (os.path.basename(dirpath), ", ".join(sorted(out))))
    return out


def assemble(name, binary, db_filename, launcher, go_version, zig_version, db_ref):
    require_assets()
    t = TARGETS[name]
    folder = "CBDB-Desktop-%s" % name
    final = os.path.join(DIST, folder)
    say("assemble %s" % folder)
    # Built beside the real folder and swapped in at the end: deleting the
    # previous one first meant a missing favicon, a full disk or Ctrl-C left
    # dist/ holding a partial folder, or none at all.
    dst = final + ".incoming"
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)

    for d in PAYLOAD_DIRS:
        shutil.copytree(os.path.join(SRC, d), os.path.join(dst, d), ignore=_skip)
    for f in PAYLOAD_FILES + [os.path.join("Data", db_filename)]:
        os.makedirs(os.path.join(dst, os.path.dirname(f)), exist_ok=True)
        shutil.copy2(os.path.join(SRC, f), os.path.join(dst, f))

    for src, rel in EXTRA_ASSETS:
        os.makedirs(os.path.join(dst, os.path.dirname(rel)), exist_ok=True)
        shutil.copy2(src, os.path.join(dst, rel))
        say("", "+ %s" % rel)

    shutil.copy2(binary, os.path.join(dst, "cbdb" + t["exe"]))

    if t["goos"] == "darwin":
        if launcher:
            with open(os.path.join(dst, "CBDB-Desktop.command"), "w",
                      encoding="utf-8", newline="\n") as fh:
                fh.write(LAUNCHER)
        other = "mac-x64" if name == "mac-arm64" else "mac-arm64"
        doc, text = "README-macOS.txt", readme_mac(name, folder, other, db_filename, launcher,
                                                   go_version, zig_version)
        eol = "\n"
    else:
        # A copy at the folder root, for anyone making a desktop shortcut.
        icon = next(src for src, rel in EXTRA_ASSETS if rel.endswith("favicon.ico"))
        shutil.copy2(icon, os.path.join(dst, "CBDB-Desktop.ico"))
        say("", "+ CBDB-Desktop.ico")
        doc, text = "README-Windows.txt", readme_win(name, folder, db_filename,
                                                     go_version, zig_version)
        eol = "\r\n"                    # Notepad-friendly
    with open(os.path.join(dst, doc), "w", encoding="utf-8", newline=eol) as fh:
        fh.write(text)

    manifest = manifest_of(dst, os.path.join("Data", db_filename), db_ref)
    say("", "%d files, manifest recorded" % len(manifest))

    # Complete: only now replace the previous folder.
    previous = final + ".old"
    shutil.rmtree(previous, ignore_errors=True)
    moved = False
    if os.path.isdir(final):
        try:
            os.replace(final, previous)
        except PermissionError as e:
            raise SystemExit("%s\n%s" % (e, _in_use(final)))
        moved = True
    try:
        os.replace(dst, final)
    except BaseException as e:
        note = _in_use(final) if isinstance(e, PermissionError) else ""
        if moved and not _restore(previous, final):
            # Both moves failed. Say where the previous release actually is,
            # rather than leaving dist/ apparently empty with no explanation.
            raise SystemExit("%s\n%s\nThe previous %s is intact at %s -- rename it back "
                             "by hand." % (e, note, os.path.basename(final), previous))
        if isinstance(e, PermissionError):
            raise SystemExit("%s\n%s" % (e, note))
        raise
    shutil.rmtree(previous, ignore_errors=True)
    return final, manifest


def manifest_of(root, db_rel, db_ref):
    """{archive-relative path: (size, sha256)} of everything assemble() wrote.

    This is what makes verification mean something. Checking only the database
    hash and a list of executable names let an archive with a zero-byte binary,
    an empty Templates/ and no stylesheet pass as "PRISTINE" -- every one of
    which produces a distribution that cannot start.

    The database is recorded from `db_ref` rather than re-read: it was copied
    from upstream, and the archive's own copy is hashed during verification, so
    upstream == archive is established without a third pass over 1.3 GB.
    """
    out = {}
    for dirpath, _, files in os.walk(root):
        for n in files:
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, root)
            size = os.path.getsize(full)
            out[rel.replace(os.sep, "/")] = (size, db_ref if rel == db_rel else sha256_of(full))
    return out


def readme_win(name, folder, db_filename, go_version, zig_version):
    return """CBDB-Desktop for Windows -- %(label)s
================================================================

Running it
----------
Unzip %(folder)s.zip anywhere you like -- your Desktop, a
Documents folder, an external drive -- and double-click cbdb.exe. A console
window opens, prints the URL and opens your browser; closing that window
stops the server.

To pin the port instead of taking a free one, from a command prompt:

    cbdb.exe -port 8080

You can run cbdb.exe from any directory, or from a shortcut with any
"Start in" setting: it finds Data\\, Templates\\ and Static\\ next to
itself rather than in the current directory.

Windows SmartScreen may warn that the publisher is unknown, because the
executable is not code-signed. Choose "More info" then "Run anyway". That
is needed once.

CBDB-Desktop.ico is here for making a shortcut with the CBDB logo on it:
right-click the shortcut, Properties, Change Icon, then browse to it.


What is in here
---------------
    cbdb.exe                the application (local web server + browser UI)
    CBDB-Desktop.ico         the logo, for a desktop shortcut
    Data\\%(db)s            the database, 1.3 GB
    Data\\qbe_schema.json    table/column whitelist the QBE form needs
    Templates\\              the HTML/JS for every form
    Static\\                 cbdb_styles.css, favicon.ico, CBDB_UserGuide.pdf

Nothing else is required. The Go sources, the database-build tool
(CBDBsetup) and the schema/codegen scripts are deliberately not shipped.


Build provenance
----------------
Built on %(date)s with:
  %(go)s, CGO_ENABLED=1, GOOS=windows GOARCH=%(goarch)s
  C compiler: %(zig)s, via 'zig cc'
  Build tags: sqlite_fts5
  Link flags: %(ldflags)s

The source was patched in a throwaway copy before compiling; the upstream
tree itself was not modified:

  * Data\\, Templates\\, Static\\ and the QBE schema are now resolved
    relative to the executable rather than the current directory. On
    Windows this only showed up through a shortcut with a different
    "Start in", or when launching from another directory -- Explorer
    happens to set the directory to the executable's own folder.
  * The default database path now matches the shipped filename exactly
    (Data\\%(db)s) instead of differing in case.
  * Content-Type for the bundled Static\\ files is registered explicitly
    instead of being read from the host MIME database. A machine whose
    registry maps .css to something else would otherwise serve the
    stylesheet with the wrong type, and the browser would ignore it.
  * /favicon.ico is served, so page loads no longer log a 404.
""" % {"label": TARGETS[name]["label"], "folder": folder, "db": db_filename,
       "goarch": TARGETS[name]["goarch"], "ldflags": TARGETS[name]["ldflags"],
       "go": go_version, "zig": zig_version,
       "date": time.strftime("%Y-%m-%d")}


def readme_mac(name, folder, other, db_filename, launcher, go_version, zig_version):
    label = TARGETS[name]["label"]
    suffix = name
    target = "CBDB-Desktop.command" if launcher else "cbdb"
    extra = ("    CBDB-Desktop.command    double-clickable launcher\n" if launcher else "")
    return """CBDB-Desktop for macOS -- %(label)s build (%(suffix)s)
================================================================

This folder is the %(label)s edition. If you are on the
other kind of Mac, use the %(other)s folder instead.
To check which Mac you have:  Apple menu > About This Mac.


Running it
----------
Double-click %(folder)s.tar.gz to unpack, then
double-click %(target)s inside the folder. A Terminal window opens,
prints the URL and opens your browser; closing that window stops the
server.

The very first launch will probably be refused, because the file was
downloaded and is not signed with an Apple Developer ID. Right-click
%(target)s and choose Open instead, then confirm. On recent macOS
the confirmation may appear in System Settings > Privacy & Security as an
"Open Anyway" button, right after the refusal. It is needed once --
approving it clears the download flag for good, so every later
double-click is silent.

From Terminal, the equivalent is:

    tar xzf %(folder)s.tar.gz
    cd %(folder)s
    ./cbdb                 # -port 8080 to pin the port instead of a free one

Unpacking with tar rather than Finder never sets the download flag in the
first place, so that route sees no refusal at all.

You can also run cbdb from any directory, or from a shortcut -- it finds
Data/, Templates/ and Static/ next to itself rather than in whatever
directory it happens to be started from.


What is in here
---------------
%(extra)s    cbdb                    the application (local web server + browser UI)
    Data/%(db)s            the database, 1.3 GB
    Data/qbe_schema.json    table/column whitelist the QBE form needs
    Templates/              the HTML/JS for every form
    Static/                 cbdb_styles.css, favicon.ico, CBDB_UserGuide.pdf

Nothing else is required. The Go sources, the database-build tool
(CBDBsetup) and the schema/codegen scripts are deliberately not shipped.


Build provenance
----------------
Cross-compiled from Windows on %(date)s with:
  %(go)s, CGO_ENABLED=1, GOOS=darwin GOARCH=%(goarch)s
  C compiler: %(zig)s, via 'zig cc'
  Build tags: sqlite_fts5
  Link flags: %(ldflags)s

The source was patched in a throwaway copy before compiling; the upstream
tree itself was not modified:

  * Data/, Templates/, Static/ and the QBE schema are now resolved
    relative to the executable rather than the working directory, so a
    Finder double-click works. (Windows Explorer sets the working
    directory to the executable's folder, which is why this never showed
    up there.)
  * The default database path now matches the shipped filename exactly
    (Data/%(db)s), so the program also works on a case-sensitive volume.
  * Content-Type for the bundled Static/ files is registered explicitly
    instead of being read from the host MIME database. Hardening: the
    text/plain stylesheet error seen on macOS was actually the body of a
    404 from the shadowed static route, fixed above.
  * /favicon.ico is served, so page loads no longer log a 404.

Run ./cbdb once and click through a couple of forms before distributing.
""" % {"label": label, "suffix": suffix, "other": other, "folder": folder,
       "db": db_filename, "goarch": TARGETS[name]["goarch"], "target": target, "extra": extra,
       "ldflags": TARGETS[name]["ldflags"], "go": go_version, "zig": zig_version,
       "date": time.strftime("%Y-%m-%d")}


def executables_for(name, launcher, folder_path=None):
    """Which files the archive must mark executable.

    Full archive-relative paths, not basenames: a basename match would also
    chmod any file called `cbdb` deeper in the payload.
    """
    top = os.path.basename(folder_path) if folder_path else ""
    names = {("cbdb" + TARGETS[name]["exe"])}
    if launcher:
        names.add("CBDB-Desktop.command")
    return {("%s/%s" % (top, n)).lower().lstrip("/") for n in names}


def pack(name, folder_path, level, launcher):
    """tar.gz for macOS, zip for Windows -- whichever the recipient can open
    by double-clicking without installing anything.

    Written to a .part file. The caller verifies that and only then moves it
    into place, so an interrupted or failed pack can never leave a truncated
    archive sitting under a release name in `packages`.
    """
    say("pack %s" % os.path.basename(folder_path))
    os.makedirs(PKGS, exist_ok=True)
    base = os.path.join(PKGS, os.path.basename(folder_path))
    execs = executables_for(name, launcher, folder_path)
    final = base + (".zip" if TARGETS[name]["pack"] == "zip" else ".tar.gz")
    part = final + ".part"
    try:
        if TARGETS[name]["pack"] == "zip":
            total = sum(os.path.getsize(os.path.join(d, f))
                        for d, _, fs in os.walk(folder_path) for f in fs)
            packdist.pack_zip(folder_path, part, level, executable=execs)
        else:
            total = packdist.pack_tar(folder_path, part, level, executable=execs)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise
    size = os.path.getsize(part)
    say("", "%.2f GB -> %.2f GB (%.1fx)" % (total / 2 ** 30, size / 2 ** 30, total / size))
    return part, final


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


@contextlib.contextmanager
def read_archive(archive, kind):
    """Yield (modes histogram, executable basenames, {member: reader}).

    Executable entries are reported as full archive-relative paths, matching
    what executables_for() produces: a basename would also match a file of
    that name deeper in the payload.

    A context manager because the readers are closures over the open
    tarfile/ZipFile. Handing those out without ever closing the container
    keeps the file locked on Windows, so a later delete or rewrite of the
    archive in the same run would fail with a sharing violation.
    """
    import tarfile
    import zipfile
    modes, execs, members = {}, [], {}

    def record(name, opener):
        # A dict would silently keep the last of a set of duplicates, while
        # an extraction tool may pick a different one or refuse the archive:
        # an ambiguous archive would verify clean.
        if name in members:
            raise SystemExit("%s: duplicate member %r" % (archive, name))
        members[name] = opener

    # Dispatch on the declared format, not on the filename: verification
    # runs against a ".part" file, so ".zip" is not the last suffix.
    if kind == "zip":
        # No testzip(): zipfile CRC-checks each member as it is read, and
        # verify() reads every member anyway, so testzip() would inflate the
        # whole 1.3 GB a second time for nothing.
        with zipfile.ZipFile(archive) as zf:
            for zi in zf.infolist():
                mode = zi.external_attr >> 16 & 0o7777
                modes[oct(mode)] = modes.get(oct(mode), 0) + 1
                if not zi.is_dir():
                    record(zi.filename, lambda n=zi.filename: zf.open(n))
                    if mode & 0o111:
                        execs.append(zi.filename)
            yield modes, execs, members
    else:
        with tarfile.open(archive, "r:gz") as tf:
            for ti in tf:
                modes[oct(ti.mode)] = modes.get(oct(ti.mode), 0) + 1
                if ti.isfile():
                    record(ti.name, lambda t=ti: tf.extractfile(t))
                    if ti.mode & 0o111:
                        execs.append(ti.name)
            yield modes, execs, members


def verify(name, archive, manifest, launcher, top):
    """Assert the archive reproduces, exactly, what assemble() wrote.

    Member set, sizes and SHA-256 of every file, plus the mode of every entry.
    Anything less has been shown to pass an archive holding a zero-byte
    binary, an empty Templates/ and no stylesheet -- each of which yields a
    distribution that cannot start.
    """
    kind = TARGETS[name]["pack"]
    say("verify %s" % os.path.basename(archive))

    with read_archive(archive, kind) as (modes, execs, members):
        say("", "modes: %s" % ", ".join("%s x%d" % (m, c) for m, c in sorted(modes.items())))
        if set(modes) - {oct(0o644), oct(0o755)}:
            raise SystemExit("%s: unexpected file modes %s" % (archive, sorted(modes)))

        # Every member must live under the expected release folder. Stripping
        # whatever the first segment happens to be would accept an archive
        # whose entire payload sits under some other directory name.
        stray = sorted(m for m in members if not m.startswith(top + "/"))
        if stray:
            raise SystemExit("%s: %d member(s) outside %s/, e.g. %s"
                             % (archive, len(stray), top, stray[0]))

        found = {}
        for member, opener in members.items():
            rel = member[len(top) + 1:]
            h, n = hashlib.sha256(), 0
            with opener() as fh:
                for b in iter(lambda: fh.read(1 << 22), b""):
                    h.update(b)
                    n += len(b)
            found[rel] = (n, h.hexdigest())

        missing = sorted(set(manifest) - set(found))
        extra = sorted(set(found) - set(manifest))
        wrong = sorted(k for k in set(manifest) & set(found) if manifest[k] != found[k])
        for label, items in (("MISSING", missing), ("UNEXPECTED", extra), ("CORRUPT", wrong)):
            for k in items:
                _out("   %-10s %s" % (label, k))
        if missing or extra or wrong:
            raise SystemExit("%s does not match the manifest assemble() recorded" % archive)
        say("", "contents: %d file(s), every size and SHA-256 matches the folder packed"
            % len(found))

        want = executables_for(name, launcher, top)
        if {e.lower() for e in execs} != want:
            raise SystemExit("%s: executable entries are %s, expected %s"
                             % (archive, sorted(execs), sorted(want)))
        say("", "executable: %s" % ", ".join(sorted(execs)))

    # tarfile stops at the end-of-archive blocks, so it never reads far enough
    # to validate gzip's CRC32/ISIZE trailer: an archive truncated by as little
    # as one byte passed every check above. Drain the stream to EOF.
    if kind == "tar":
        import gzip
        with gzip.open(archive, "rb") as fh:
            while fh.read(1 << 22):
                pass
        say("", "gzip stream intact to EOF (trailer CRC validated)")


def snapshot(root):
    """{relative path: (size, mtime)} for every file under root.

    A timestamp threshold cannot do this job: extraction stamps the payload
    with times that may be "now" (zipfile does not restore mtimes) or the
    archive's own, and a same-second write would slip through either way. A
    snapshot taken after extraction and compared at the end is exact.
    """
    out = {}
    for dirpath, _, files in os.walk(root):
        for n in files:
            p = os.path.join(dirpath, n)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


def write_stamp(archive, go_version, zig_version, built):
    """Record what produced the archives now sitting in `packages`.

    Rewritten after every published target rather than once at the end: with a
    single write at the end, a later target failing left freshly published
    archives beside a missing or previous-run stamp.
    """
    # Every archive in the directory is listed, not only this run's: a partial
    # run (--target, or a later target failing) otherwise leaves archives from
    # an earlier upstream release sitting unmentioned beside fresh ones, and a
    # mixed set could be shipped without anyone noticing.
    present = sorted(f for f in os.listdir(PKGS) if f.endswith((".tar.gz", ".zip")))
    lines = ["%s  %s" % ("this run" if f in built else "EARLIER ", f) for f in present]
    body = ("archive:  %s\ngo:       %s\nzig:      %s\nbuilt:    %s\n\n%s\n"
            % (os.path.basename(archive), go_version, zig_version,
               time.strftime("%Y-%m-%d %H:%M:%S"), "\n".join(lines)))
    stamp = os.path.join(PKGS, ".built-from")
    part = stamp + ".part"                      # atomic: never a truncated stamp
    with open(part, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(part, stamp)


def prove_upstream_untouched(before):
    say("prove the upstream tree was not modified")
    after = snapshot(SRC)
    changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    if changed or added or removed:
        for label, items in (("MODIFIED", changed), ("ADDED", added), ("REMOVED", removed)):
            for k in items:
                _out("   %-8s %s" % (label, k))
        raise SystemExit("the upstream tree was written to; investigate before shipping")
    say("", "clean -- %d files under %s unchanged in size and mtime"
        % (len(after), os.path.relpath(SRC, ROOT)))


# ---------------------------------------------------------------------------

def main():
    global WORK, SRC, EXTRA_ASSETS
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", help="upstream release archive (default: CBDB_ARCHIVE from "
                                      ".env, else the single .7z or .zip in the repository "
                                      "root)")
    ap.add_argument("--icon", help="filename of the .ico to take from the payload root "
                                   "(default: CBDB_INPUT_ICON from .env)")
    ap.add_argument("--no-input-icon", action="store_true",
                    help="use this repository's generated favicon instead of one from the "
                         "upstream archive (default: CBDB_USE_INPUT_ICON from .env, true)")
    ap.add_argument("--re-extract", action="store_true",
                    help="unpack the archive again even if the stamp says it is current")
    ap.add_argument("--audit-only", action="store_true", help="report upstream problems and stop")
    ap.add_argument("--target", choices=sorted(TARGETS), action="append",
                    help="build only this target (repeatable; default all three)")
    ap.add_argument("-l", "--level", type=int, default=6, choices=range(1, 10), metavar="1-9",
                    help="deflate level (default 6)")
    ap.add_argument("--payload-root", help="the upstream payload directory inside `extracted` "
                                           "(default: the one holding Code/ and Data/)")
    ap.add_argument("--zig", help="path to zig.exe (default: found on PATH or under WinGet)")
    ap.add_argument("--work", default=WORK,
                    help="scratch directory; must contain no whitespace, because Go splits "
                         "$CC on it (default: %(default)s)")
    ap.add_argument("--launcher", action="store_true",
                    help="macOS only: also ship CBDB-Desktop.command. Obsolete now that paths are "
                         "resolved against the executable: cbdb can simply be double-clicked. "
                         "Kept for the case where a named, obviously-clickable entry point is "
                         "wanted, or where folders arrive over a filesystem that loses the "
                         "execute bit (the launcher restores it).")
    args = ap.parse_args()

    if os.name != "nt":
        raise SystemExit("this kit cross-builds FROM Windows: it uses a Windows cc shim and "
                         "looks for zig where winget puts it. On macOS or Linux build the Go "
                         "module directly.")

    WORK = os.path.abspath(args.work)
    check_work_path()

    settings = load_env()
    # Command line beats .env beats the default, throughout.
    archive_setting = args.archive or settings.get("CBDB_ARCHIVE") or None
    wanted_icon = args.icon or settings.get("CBDB_INPUT_ICON") or ""
    # Naming an icon on the command line means wanting one from the payload,
    # whatever .env says -- otherwise --icon would be silently discarded.
    use_input_icon = not args.no_input_icon and (
        bool(args.icon) or env_flag(settings, "CBDB_USE_INPUT_ICON", True))

    if not use_input_icon and not args.audit_only and not os.path.isfile(FALLBACK_ICON):
        raise SystemExit("%s is missing; generate it with:\n  python %s --preview"
                         % (FALLBACK_ICON, os.path.join(HERE, "make-favicon.py")))

    # Resolve the toolchain first: a missing Go should not cost a 1.3 GB
    # extraction before it is noticed. --audit-only needs none of it.
    zig = None
    if not args.audit_only:
        zig = require_tools(args.zig)

    archive = find_archive(archive_setting)
    extract(archive, force=args.re_extract)
    SRC = discover_payload(args.payload_root)

    db_filename = actual_db_filename()
    problems, already_fixed = audit(db_filename)
    if problems == 0:
        say("", "nothing to patch; upstream already carries every fix")

    if args.audit_only:
        return

    # After the audit: a payload holding two .ico files and no CBDB_INPUT_ICON
    # is a legitimate question to ask the user, but not one that should stop
    # --audit-only from reporting anything at all.
    EXTRA_ASSETS = resolve_icon(use_input_icon, wanted_icon)

    go_version = capture(["go", "version"]).stdout.strip() or "unknown"
    zig_version = "zig " + (capture([zig, "version"]).stdout.strip() or "unknown")

    say("toolchain")
    say("", "go:  %s" % go_version)
    say("", "zig: %s  (%s)" % (zig_version, zig))

    # Taken after extraction, compared at the very end, to prove the upstream
    # tree was only ever read.
    before = snapshot(SRC)
    db_ref = sha256_of(os.path.join(SRC, "Data", db_filename))

    os.makedirs(WORK, exist_ok=True)
    staged = stage()
    patch(staged, db_filename, already_fixed)
    shims = shim()

    built = []
    try:
        for name in (args.target or sorted(TARGETS)):
            t = TARGETS[name]
            say("build %s/%s" % (t["goos"], t["goarch"]))
            binary = build(staged, name, shims, zig)
            say("", "%.1f MB" % (os.path.getsize(binary) / 2 ** 20))
            launcher = args.launcher and t["goos"] == "darwin"
            folder, manifest = assemble(name, binary, db_filename, launcher,
                                        go_version, zig_version, db_ref)
            part, final = pack(name, folder, args.level, launcher)
            try:
                verify(name, part, manifest, launcher, os.path.basename(folder))
            except BaseException:
                os.remove(part)     # never leave a rejected archive behind
                raise
            # Only now does it get a release name.
            os.replace(part, final)
            say("", "-> %s" % final)
            built.append(os.path.basename(final))
            # Rewritten after every target, so the stamp always describes what
            # is actually in packages/ -- writing it once at the end left a
            # stale or missing stamp beside freshly published archives
            # whenever a later target failed.
            write_stamp(archive, go_version, zig_version, built)
    finally:
        # Run even on failure: if the tree WAS written to, that is exactly when
        # it matters most.
        prove_upstream_untouched(before)

    say("done")
    say("", "packages in %s" % PKGS)
    say("", "provenance in .built-from: %s" % ", ".join(built))


if __name__ == "__main__":
    main()
