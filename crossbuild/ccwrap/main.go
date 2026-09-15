package main

// Thin CC shim for cross-compiling cgo Go programs to macOS with `zig cc`.
//
// Configuration comes from the environment, falling back to values baked in
// with -ldflags -X. Prefer the environment for anything path-shaped: a -X
// value containing a space is split by the Go linker's flag parser and
// silently corrupts the build, and this repository may well live under such
// a path. Only the mode, which is "cc" or "c++", is baked in.
//
//	CCWRAP_ZIG     path to zig.exe
//	CCWRAP_TARGET  darwin target triple, e.g. aarch64-macos
//	CCWRAP_STUB    directory holding lib/*.tbd and Frameworks/*.framework
//	CCWRAP_MODE    "cc" (default) or "c++"
//
// The stub directory supplies the .tbd files for libresolv, CoreFoundation
// and Security, which zig does not bundle; without them the darwin link step
// cannot resolve the symbols crypto/x509 imports.
import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
)

// Defaults, optionally baked in via -ldflags -X (no spaces, please).
var (
	zig    string
	target string
	stub   string
	mode   = "cc"
)

// Linker flags zig's linker rejects. Go passes -no_uuid whenever the Mach-O
// UUID is suppressed with -B none, which this build does because the UUID
// rewrite step hits an "Access is denied" rename bug on Windows.
var dropped = map[string]bool{
	"-Wl,-no_uuid": true,
	"-no_uuid":     true,
}

func setting(env, baked string) string {
	if v := os.Getenv(env); v != "" {
		return v
	}
	return baked
}

// filterResponseFile copies name, minus the flags zig's linker rejects, and
// returns the path of the copy.
//
// Go's linker writes one argument per line with fmt.Fprintf("%q\n", arg), so
// every line is Go-quoted: the line to drop reads `"-Wl,-no_uuid"`, quotes
// included. Comparing the raw line against the bare flag never matches.
func filterResponseFile(name string) (string, error) {
	raw, err := os.ReadFile(name)
	if err != nil {
		return "", err
	}
	lines := strings.Split(string(raw), "\n")
	kept := make([]string, 0, len(lines))
	for _, line := range lines {
		arg := strings.TrimRight(line, "\r")
		if unquoted, err := strconv.Unquote(arg); err == nil {
			arg = unquoted
		}
		if dropped[arg] {
			continue
		}
		kept = append(kept, line) // keep the original, quoting intact
	}
	out := name + ".ccwrap"
	if err := os.WriteFile(out, []byte(strings.Join(kept, "\n")), 0o600); err != nil {
		return "", err
	}
	return out, nil
}

func main() {
	z := setting("CCWRAP_ZIG", zig)
	t := setting("CCWRAP_TARGET", target)
	s := setting("CCWRAP_STUB", stub)
	m := setting("CCWRAP_MODE", mode)

	required := map[string]string{"CCWRAP_ZIG": z, "CCWRAP_TARGET": t}
	if strings.Contains(t, "macos") || strings.Contains(t, "darwin") {
		required["CCWRAP_STUB"] = s
	}
	for name, v := range required {
		if v == "" {
			os.Stderr.WriteString("ccwrap: " + name + " is not set\n")
			os.Exit(1)
		}
	}

	args := []string{m, "-target", t}
	// The .tbd stubs exist only to satisfy the darwin link step. Passing -F to
	// a non-darwin target is rejected outright, so add them only for macOS.
	if strings.Contains(t, "macos") || strings.Contains(t, "darwin") {
		args = append(args,
			"-L"+filepath.Join(s, "lib"),
			"-F"+filepath.Join(s, "Frameworks"))
	}
	for _, a := range os.Args[1:] {
		if dropped[a] {
			continue
		}
		// Go replaces the whole command line with "@file" once the arguments
		// exceed its length limit, and the flags we drop then live inside that
		// file where an exact-match filter cannot see them.
		if strings.HasPrefix(a, "@") {
			rewritten, err := filterResponseFile(a[1:])
			if err != nil {
				os.Stderr.WriteString("ccwrap: " + err.Error() + "\n")
				os.Exit(1)
			}
			a = "@" + rewritten
		}
		args = append(args, a)
	}

	cmd := exec.Command(z, args...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			// ExitCode is -1 when the process died on a signal, and passing
			// that to os.Exit yields 0xFFFFFFFF, which Go's build reports as a
			// baffling exit status instead of a failure.
			if code := ee.ExitCode(); code > 0 {
				os.Exit(code)
			}
			os.Exit(1)
		}
		os.Stderr.WriteString("ccwrap: " + err.Error() + "\n")
		os.Exit(1)
	}
}
