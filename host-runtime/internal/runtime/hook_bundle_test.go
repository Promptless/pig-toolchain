package runtime

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

func hookFixture(t *testing.T) (string, string, hookBundle) {
	t.Helper()
	t.Setenv("PROMPTLESS_HOOK_DATA_HOME", filepath.Join(t.TempDir(), "data"))
	source := t.TempDir()
	contents := map[string]string{hookRunner: "adapter", hookLauncher: "launcher", "example/run.py": "script", "example/helper.py": "helper"}
	manifest := hookBundle{Schema: 1, Host: "codex", Hook: "example", Entrypoint: "example/run.py", Config: json.RawMessage(`{}`), Files: map[string]hookFile{}, Runtimes: map[string]string{runtime.GOOS + "-" + runtime.GOARCH: strings.Repeat("a", 64)}}
	for name, value := range contents {
		target := filepath.Join(source, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, []byte(value), 0600); err != nil {
			t.Fatal(err)
		}
		manifest.Files[name] = hookFile{SHA256: hash([]byte(value))}
	}
	return source, writeHookFixture(t, source, manifest), manifest
}

func writeHookFixture(t *testing.T, source string, manifest hookBundle) string {
	t.Helper()
	payload, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	digest := hash(payload)
	if err = os.MkdirAll(filepath.Join(source, "_bundles"), 0700); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(source, "_bundles", digest+".json"), payload, 0600); err != nil {
		t.Fatal(err)
	}
	return digest
}

func TestHookBundleConcurrentPublicationAndSourceRemoval(t *testing.T) {
	source, digest, _ := hookFixture(t)
	var workers sync.WaitGroup
	for range 12 {
		workers.Go(func() {
			if _, _, err := retainHookBundle(source, digest); err != nil {
				t.Error(err)
			}
		})
	}
	workers.Wait()
	if err := os.RemoveAll(source); err != nil {
		t.Fatal(err)
	}
	root, _, err := retainHookBundle(source, digest)
	if err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(filepath.Dir(root))
	if err != nil || len(entries) != 1 || entries[0].Name() != digest {
		t.Fatalf("partial or duplicate bundle: %v %v", entries, err)
	}
}

func TestHookBundleRejectsCorruptionWithoutRepair(t *testing.T) {
	for _, damage := range []string{"missing", "changed", "extra", "symlink", "parent-symlink", "manifest", "directory"} {
		t.Run(damage, func(t *testing.T) {
			source, digest, _ := hookFixture(t)
			root, _, err := retainHookBundle(source, digest)
			if err != nil {
				t.Fatal(err)
			}
			helper := filepath.Join(root, "example", "helper.py")
			switch damage {
			case "missing":
				err = os.Remove(helper)
			case "changed":
				err = os.WriteFile(helper, []byte("wrong version"), 0600)
			case "extra":
				err = os.WriteFile(filepath.Join(root, "example", "unlisted.py"), nil, 0600)
			case "manifest":
				err = os.WriteFile(filepath.Join(root, "manifest.json"), []byte(`{}`), 0600)
			case "directory":
				if err = os.Remove(helper); err == nil {
					err = os.Mkdir(helper, 0700)
				}
			case "symlink", "parent-symlink":
				if damage == "parent-symlink" {
					helper = filepath.Dir(helper)
				}
				if err = os.RemoveAll(helper); err != nil {
					t.Fatal(err)
				}
				if err = os.Symlink(source, helper); err != nil && runtime.GOOS == "windows" {
					t.Skipf("symlink privilege unavailable: %v", err)
				}
			}
			if err != nil {
				t.Fatal(err)
			}
			if _, _, err = retainHookBundle(source, digest); err == nil {
				t.Fatal("executed or repaired a corrupt retained bundle")
			}
		})
	}
}

func TestHookBundleFailedPreparationPublishesNothing(t *testing.T) {
	source, digest, _ := hookFixture(t)
	if err := os.Remove(filepath.Join(source, "example", "helper.py")); err != nil {
		t.Fatal(err)
	}
	if _, _, err := retainHookBundle(source, digest); err == nil {
		t.Fatal("accepted an incomplete source")
	}
	entries, err := os.ReadDir(filepath.Join(hookDataRoot(), "bundles"))
	if err != nil || len(entries) != 0 {
		t.Fatalf("partial bundle or staging files survived: %v %v", entries, err)
	}
}

func TestHookBundleRejectsUnsafeManifestPaths(t *testing.T) {
	for _, name := range []string{"../outside", "/absolute", "example/../outside", "example//run.py", "example/./run.py", "example\\run.py", "C:/outside", "manifest.json", "other/run.py"} {
		t.Run(name, func(t *testing.T) {
			source, _, manifest := hookFixture(t)
			manifest.Files[name] = hookFile{SHA256: strings.Repeat("a", 64)}
			digest := writeHookFixture(t, source, manifest)
			if _, _, err := retainHookBundle(source, digest); err == nil {
				t.Fatal("accepted unsafe file inventory")
			}
		})
	}
}

func TestHookRuntimePinsExactExecutable(t *testing.T) {
	_, digest, manifest := hookFixture(t)
	if err := retainHookExecutable(digest, manifest); err == nil {
		t.Fatal("accepted another runtime version")
	}
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	binary, err := os.ReadFile(exe)
	if err != nil {
		t.Fatal(err)
	}
	manifest.Runtimes[runtime.GOOS+"-"+runtime.GOARCH] = hash(binary)
	for range 2 {
		if err = retainHookExecutable(digest, manifest); err != nil {
			t.Fatal(err)
		}
	}
	name := executableName
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	retained, err := os.ReadFile(filepath.Join(hookDataRoot(), "bin", digest, name))
	if err != nil || hash(retained) != hash(binary) {
		t.Fatalf("wrong pinned executable: %v", err)
	}
}

func TestHookFailuresAreNonblockingAndDeduplicated(t *testing.T) {
	source, digest, _ := hookFixture(t)
	if err := os.RemoveAll(source); err != nil {
		t.Fatal(err)
	}
	var diagnostic bytes.Buffer
	args := []string{"hook", "run", "--source", source, "--bundle", digest}
	for range 2 {
		if code := Main(args, strings.NewReader("{}"), &bytes.Buffer{}, &diagnostic); code != 0 {
			t.Fatalf("context hook blocked the host: %d", code)
		}
	}
	if strings.Count(diagnostic.String(), "context hook skipped") != 1 {
		t.Fatalf("unexpected diagnostics: %s", &diagnostic)
	}
}
