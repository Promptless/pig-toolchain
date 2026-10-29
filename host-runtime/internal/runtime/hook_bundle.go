package runtime

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

const hookRunner = "_instruction_hub_hook.py"
const hookLauncher = "_instruction_hub_hook_launcher.py"

type hookFile struct {
	SHA256     string `json:"sha256"`
	Executable bool   `json:"executable"`
}

type hookBundle struct {
	Schema     int                 `json:"schema"`
	Host       string              `json:"host"`
	Hook       string              `json:"hook"`
	Entrypoint string              `json:"entrypoint"`
	Config     json.RawMessage     `json:"config"`
	Files      map[string]hookFile `json:"files"`
	Runtimes   map[string]string   `json:"runtimes"`
}

func hookDataRoot() string {
	if override := os.Getenv("PROMPTLESS_HOOK_DATA_HOME"); override != "" {
		return filepath.Join(override, "v1")
	}
	base := os.Getenv("XDG_DATA_HOME")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, ".local", "share")
	}
	return filepath.Join(base, "promptless", "hook-runtime", "v1")
}

func hookDigest(value string) bool {
	return len(value) == 64 && strings.Trim(value, "0123456789abcdef") == ""
}

func hookRelativePath(name string) bool {
	return name != "." && fs.ValidPath(name) && !strings.ContainsAny(name, "\\:")
}

func readHookManifest(root, name, digest string) ([]byte, hookBundle, error) {
	var manifest hookBundle
	payload, err := readHookFile(root, name)
	if err != nil {
		return nil, manifest, err
	}
	if hash(payload) != digest {
		return nil, manifest, errors.New("hook manifest digest mismatch")
	}
	if err = json.Unmarshal(payload, &manifest); err != nil {
		return nil, manifest, err
	}
	if manifest.Schema != 1 || (manifest.Host != "claude" && manifest.Host != "codex" && manifest.Host != "cursor") {
		return nil, manifest, errors.New("unsupported hook bundle")
	}
	if !hookRelativePath(manifest.Hook) || strings.Contains(manifest.Hook, "/") || !strings.HasPrefix(manifest.Entrypoint, manifest.Hook+"/") {
		return nil, manifest, errors.New("invalid hook entrypoint")
	}
	for _, name := range []string{hookRunner, hookLauncher, manifest.Entrypoint} {
		if _, ok := manifest.Files[name]; !ok {
			return nil, manifest, errors.New("incomplete hook manifest")
		}
	}
	for name, expected := range manifest.Files {
		if !hookRelativePath(name) || name == "manifest.json" || !hookDigest(expected.SHA256) ||
			(name != hookRunner && name != hookLauncher && !strings.HasPrefix(name, manifest.Hook+"/")) {
			return nil, manifest, errors.New("invalid hook file inventory")
		}
	}
	if !hookDigest(manifest.Runtimes[runtime.GOOS+"-"+runtime.GOARCH]) {
		return nil, manifest, errors.New("hook bundle has no runtime for this platform")
	}
	return payload, manifest, nil
}

// Lstat every component before opening it: retained imports must never follow
// symlinks into a mutable plugin cache or execute unlisted special files.
func readHookFile(root, name string) ([]byte, error) {
	if !hookRelativePath(name) {
		return nil, errors.New("invalid hook bundle path")
	}
	current := root
	parts := append([]string{""}, strings.Split(name, "/")...)
	for i, part := range parts {
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if err != nil {
			return nil, err
		}
		if i < len(parts)-1 && !info.IsDir() || i == len(parts)-1 && !info.Mode().IsRegular() {
			return nil, errors.New("symlink or non-regular path in hook bundle")
		}
	}
	return os.ReadFile(current)
}

func checkedHookFile(root, name string, expected hookFile) ([]byte, error) {
	payload, err := readHookFile(root, name)
	if err != nil {
		return nil, err
	}
	if hash(payload) != expected.SHA256 {
		return nil, fmt.Errorf("hook file digest mismatch: %s", name)
	}
	return payload, nil
}

func validateRetainedHook(root, digest string) (hookBundle, error) {
	_, manifest, err := readHookManifest(root, "manifest.json", digest)
	if err != nil {
		return manifest, err
	}
	err = filepath.WalkDir(root, func(filename string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		relative, err := filepath.Rel(root, filename)
		if err != nil {
			return err
		}
		name := filepath.ToSlash(relative)
		_, listed := manifest.Files[name]
		if entry.Type()&os.ModeSymlink != 0 || !entry.Type().IsRegular() || (!listed && name != "manifest.json") {
			return errors.New("unexpected file in retained hook bundle")
		}
		return nil
	})
	if err != nil {
		return manifest, err
	}
	for name, expected := range manifest.Files {
		if _, err = checkedHookFile(root, name, expected); err != nil {
			return manifest, err
		}
	}
	return manifest, nil
}

// Publish an entire verified directory. Existing bundles are never repaired from
// another version or overwritten; a concurrent winner must pass the same checks.
func retainHookBundle(source, digest string) (string, hookBundle, error) {
	var manifest hookBundle
	if !hookDigest(digest) {
		return "", manifest, errors.New("invalid hook bundle digest")
	}
	bundles := filepath.Join(hookDataRoot(), "bundles")
	destination := filepath.Join(bundles, digest)
	if _, err := os.Lstat(destination); errors.Is(err, os.ErrNotExist) {
		payload, parsed, err := readHookManifest(source, "_bundles/"+digest+".json", digest)
		if err != nil {
			return "", manifest, err
		}
		if err = os.MkdirAll(bundles, 0700); err != nil {
			return "", manifest, err
		}
		staging, err := os.MkdirTemp(bundles, ".staging-")
		if err != nil {
			return "", manifest, err
		}
		defer os.RemoveAll(staging)
		for name, expected := range parsed.Files {
			content, err := checkedHookFile(source, name, expected)
			if err != nil {
				return "", manifest, err
			}
			target := filepath.Join(staging, filepath.FromSlash(name))
			if err = os.MkdirAll(filepath.Dir(target), 0700); err != nil {
				return "", manifest, err
			}
			mode := fs.FileMode(0600)
			if expected.Executable {
				mode = 0700
			}
			if err = os.WriteFile(target, content, mode); err != nil {
				return "", manifest, err
			}
		}
		if err = os.WriteFile(filepath.Join(staging, "manifest.json"), payload, 0600); err != nil {
			return "", manifest, err
		}
		if err = os.Rename(staging, destination); err != nil {
			// Rename errors differ across Windows/Unix. Accept only a complete,
			// independently verified winner, regardless of the platform errno.
			if _, winnerErr := validateRetainedHook(destination, digest); winnerErr != nil {
				return "", manifest, fmt.Errorf("publish hook bundle: %w", err)
			}
		}
	} else if err != nil {
		return "", manifest, err
	}
	manifest, err := validateRetainedHook(destination, digest)
	return destination, manifest, err
}

func publishHookExecutable(destination string, binary []byte) error {
	if existing, err := readHookFile(filepath.Dir(destination), filepath.Base(destination)); err == nil {
		if hash(existing) != hash(binary) {
			return errors.New("retained hook executable digest mismatch")
		}
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := atomicWriteMode(destination, binary, 0700); err != nil {
		// A concurrent install may win while Windows holds the executable open.
		existing, readErr := readHookFile(filepath.Dir(destination), filepath.Base(destination))
		if readErr != nil || hash(existing) != hash(binary) {
			return err
		}
	}
	return nil
}

func retainHookExecutable(digest string, manifest hookBundle) error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	binary, err := os.ReadFile(executable)
	if err != nil {
		return err
	}
	binaryDigest := hash(binary)
	if binaryDigest != manifest.Runtimes[runtime.GOOS+"-"+runtime.GOARCH] {
		return errors.New("hook runtime does not match the pinned bundle")
	}
	name := executableName
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	stored := filepath.Join(hookDataRoot(), "runtimes", binaryDigest, name)
	if err = publishHookExecutable(stored, binary); err != nil {
		return err
	}
	target := filepath.Join(hookDataRoot(), "bin", digest, name)
	if err = os.MkdirAll(filepath.Dir(target), 0700); err != nil {
		return err
	}
	// Multiple hooks share one immutable binary without duplicating its bytes.
	// Filesystems without hard links can still retain a private copy.
	if err = os.Link(stored, target); err != nil {
		if err = publishHookExecutable(target, binary); err != nil {
			return err
		}
	}
	if runtime.GOOS == "windows" {
		launcher := []byte("#!/bin/sh\nexec \"${0%/*}/promptless-host-runtime.exe\" \"$@\"\n")
		return publishHookExecutable(filepath.Join(filepath.Dir(target), executableName), launcher)
	}
	return nil
}

func reportHookUnavailable(digest string, cause error, stderr io.Writer) {
	warning := filepath.Join(hookDataRoot(), "warnings", hash([]byte(digest)))
	if err := os.MkdirAll(filepath.Dir(warning), 0700); err == nil {
		f, err := os.OpenFile(warning, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if errors.Is(err, os.ErrExist) {
			return
		}
		if err == nil {
			_ = f.Close()
		}
	}
	detail := strings.ReplaceAll(cause.Error(), "\n", " ")
	fmt.Fprintf(stderr, "Instruction Hub context hook skipped: %.500s\n", detail)
}

func portableHookMain(args []string, stderr io.Writer) int {
	var source, digest string
	flags := flag.NewFlagSet("hook", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	flags.StringVar(&source, "source", "", "Plugin hook directory used only for initial preparation")
	flags.StringVar(&digest, "bundle", "", "Exact bundle digest")
	var err error
	if len(args) == 0 || (args[0] != "prepare" && args[0] != "run") {
		err = errors.New("expected hook prepare or hook run")
	} else if err = flags.Parse(args[1:]); err == nil && (flags.NArg() != 0 || source == "") {
		err = errors.New("hook requires --bundle and --source")
	}
	if err != nil {
		reportHookUnavailable(digest, err, stderr)
		return 0
	}
	root, manifest, err := retainHookBundle(source, digest)
	if err == nil {
		err = retainHookExecutable(digest, manifest)
	}
	if err != nil {
		reportHookUnavailable(digest, err, stderr)
		return 0
	}
	if args[0] == "run" {
		argv, lookupErr := portableHookPython()
		if lookupErr != nil {
			reportHookUnavailable(digest, errors.New("Instruction Hub hooks require Python 3.9 or newer"), stderr)
			return 0
		}
		_ = os.Remove(filepath.Join(hookDataRoot(), "warnings", hash([]byte(digest))))
		argv = append(argv, "-I", "-B", filepath.Join(root, hookLauncher), filepath.Join(root, hookRunner),
			"--host", manifest.Host, "--entrypoint", filepath.Join(root, filepath.FromSlash(manifest.Entrypoint)))
		if err = execPortableHook(argv[0], argv); err != nil {
			reportHookUnavailable(digest, err, stderr)
		}
	} else {
		_ = os.Remove(filepath.Join(hookDataRoot(), "warnings", hash([]byte(digest))))
	}
	return 0
}
