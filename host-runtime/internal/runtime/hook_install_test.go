package runtime

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

func TestHookInstallSurvivesPluginRemovalAndKeepsMetadataScoped(t *testing.T) {
	privateHome(t)
	first, second := t.TempDir(), t.TempDir()
	install := func(root, version string) string {
		t.Helper()
		exe := filepath.Join(root, "runtime")
		mustWrite(t, exe, []byte(version))
		mustWrite(t, filepath.Join(root, "hub.managed-runtimes.json"), []byte(`{"managed_runtimes":[{"id":"host-runtime","status":"included","plugin_id":"pig","plugin_version":"`+version+`"}]}`))
		if err := installHookExecutable(exe, root); err != nil {
			t.Fatal(err)
		}
		return exe
	}
	firstExe := install(first, "1")
	launcher := stateSibling(filepath.Join("bin", executableName))
	before, err := os.Stat(launcher)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && before.Mode().Perm() != 0700 {
		t.Fatalf("launcher permissions: %v", before.Mode())
	}
	if err := installHookExecutable(firstExe, first); err != nil {
		t.Fatal(err)
	}
	install(second, "2")
	if err := os.RemoveAll(first); err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(second); err != nil {
		t.Fatal(err)
	}
	for root, version := range map[string]string{first: "1", second: "2"} {
		if got := metadata(root, Codex); got.PluginVersion != version || got.PluginID != "pig" {
			t.Fatalf("metadata for %s: %+v", root, got)
		}
	}
	for _, version := range []string{"1", "2"} {
		name := executableName
		if runtime.GOOS == "windows" {
			name += ".exe"
		}
		path := stateSibling(filepath.Join("runtime", hash([]byte(version)), name))
		if data, err := os.ReadFile(path); err != nil || string(data) != version {
			t.Fatalf("retained runtime: %s, %v", data, err)
		}
	}
	if data, err := os.ReadFile(launcher); err != nil || !strings.Contains(string(data), hash([]byte("2"))) {
		t.Fatalf("launcher did not select update: %s %v", data, err)
	}
	if got := metadata(t.TempDir(), Codex); got.PluginVersion != "unknown" {
		t.Fatalf("unrelated plugin inherited metadata: %+v", got)
	}
}

func TestHookInstallFailurePreservesWorkingLauncher(t *testing.T) {
	privateHome(t)
	exe := filepath.Join(t.TempDir(), "runtime")
	mustWrite(t, exe, []byte("working"))
	if err := installHookExecutable(exe, ""); err != nil {
		t.Fatal(err)
	}
	launcher := stateSibling(filepath.Join("bin", executableName))
	before, _ := os.ReadFile(launcher)
	root := t.TempDir()
	mustWrite(t, filepath.Join(root, "hub.managed-runtimes.json"), []byte("broken metadata"))
	mustWrite(t, exe, []byte("new executable"))
	if err := installHookExecutable(exe, root); err == nil {
		t.Fatal("accepted invalid metadata")
	}
	after, err := os.ReadFile(launcher)
	if err != nil || string(after) != string(before) {
		t.Fatal("failed install replaced the working launcher")
	}
}

func TestHookInstallRejectsChangedExecutableCopy(t *testing.T) {
	privateHome(t)
	exe := filepath.Join(t.TempDir(), "runtime")
	mustWrite(t, exe, []byte("original"))
	if err := installHookExecutable(exe, ""); err != nil {
		t.Fatal(err)
	}
	name := executableName
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	installed := stateSibling(filepath.Join("runtime", hash([]byte("original")), name))
	mustWrite(t, installed, []byte("changed"))
	if err := installHookExecutable(exe, ""); err == nil || !strings.Contains(err.Error(), "digest mismatch") {
		t.Fatalf("accepted changed executable: %v", err)
	}
}

func TestConcurrentHookInstalls(t *testing.T) {
	privateHome(t)
	exe := filepath.Join(t.TempDir(), "runtime")
	mustWrite(t, exe, []byte("shared executable"))
	var group sync.WaitGroup
	for range 8 {
		group.Add(1)
		go func() {
			defer group.Done()
			if err := installHookExecutable(exe, ""); err != nil {
				t.Error(err)
			}
		}()
	}
	group.Wait()
}

func TestHookInstallFailureIsReported(t *testing.T) {
	privateHome(t)
	if err := os.MkdirAll(filepath.Dir(statePath()), 0700); err != nil {
		t.Fatal(err)
	}
	// A file in place of the runtime directory makes installation fail without
	// preventing the runtime from writing its diagnostic next to that directory.
	mustWrite(t, stateSibling("runtime"), []byte("blocked"))
	var output bytes.Buffer
	if code := Main([]string{"session-start", "--host", "codex", "--detach"}, strings.NewReader("{}"), &output, io.Discard); code != 1 {
		t.Fatalf("installation failure exit: %d", code)
	}
	if !strings.Contains(output.String(), "Could not install the persistent Promptless hook runtime") {
		t.Fatalf("missing hook error: %s", output.String())
	}
	diagnostic, err := loadState(stateSibling("last-bootstrap-status.json"))
	if err != nil || diagnostic["reason"] != "hook_install_failed" {
		t.Fatalf("missing durable diagnostic: %v %v", diagnostic, err)
	}
}
