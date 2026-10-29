package runtime

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRuntimeConfigRejectsMalformedDestinations(t *testing.T) {
	for _, raw := range []string{
		`null`, `[]`, `{}`, `{"schema_version":true}`, `{"schema_version":1,"schema_version":1}`,
		`{"schema_version":1,"worker_base_url":"https://safe.test","worker_base_url":"https://other.test"}`,
		`{"schema_version":1,"unknown":true}`, `{"schema_version":1} {}`, `{"schema_version":1,"worker_base_url":null}`,
	} {
		if _, err := parseRuntimeConfig([]byte(raw)); err == nil {
			t.Errorf("accepted %s", raw)
		}
	}
	for _, raw := range []string{"http://127.0.0.1", "https://user:secret@worker.test", "https://worker.test/path", "https://worker.test?", "https://worker.test#", "https://worker.test:0", "https://worker.test:65536", "https://worker.test:", "https://[invalid]", " https://worker.test"} {
		t.Setenv("PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES", "1")
		if _, err := parseRuntimeConfig([]byte(`{"schema_version":1,"worker_base_url":"` + raw + `"}`)); err == nil {
			t.Errorf("accepted %s", raw)
		}
	}
}
func TestRuntimeConfigPrecedenceRetentionAndIsolation(t *testing.T) {
	privateHome(t)
	t.Setenv("PROMPTLESS_WORKER_BASE_URL", "")
	t.Setenv("PROMPTLESS_DASHBOARD_BASE_URL", "")
	first, second := t.TempDir(), t.TempDir()
	for root, host := range map[string]string{first: "first.test", second: "second.test"} {
		mustWrite(t, filepath.Join(root, runtimeConfigName), []byte(`{"schema_version":1,"worker_base_url":"https://`+host+`/","dashboard_base_url":"https://dashboard.test"}`))
		exe := filepath.Join(root, "executable")
		mustWrite(t, exe, []byte("runtime"))
		if err := installHookExecutable(exe, root); err != nil {
			t.Fatal(err)
		}
	}
	for _, removed := range []bool{false, true} {
		for root, host := range map[string]string{first: "first.test", second: "second.test"} {
			if removed {
				if err := os.RemoveAll(root); err != nil {
					t.Fatal(err)
				}
			}
			t.Setenv("PLUGIN_ROOT", root)
			if got, err := workerURL(); err != nil || got != "https://"+host {
				t.Fatalf("%s %v", got, err)
			}
			if got, err := dashboardURL(); err != nil || got != "https://dashboard.test" {
				t.Fatalf("%s %v", got, err)
			}
		}
	}
	t.Setenv("PROMPTLESS_WORKER_BASE_URL", "https://override.test")
	if got, err := workerURL(); err != nil || got != "https://override.test" {
		t.Fatalf("%s %v", got, err)
	}
	t.Setenv("PROMPTLESS_WORKER_BASE_URL", "")
	// Reusing a root without a config must not resurrect its previous destination.
	if err := os.MkdirAll(second, 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PLUGIN_ROOT", second)
	if got, err := workerURL(); err != nil || got != "https://pig.promptless.ai" {
		t.Fatalf("%s %v", got, err)
	}
	t.Setenv("PLUGIN_ROOT", t.TempDir())
	if got, err := workerURL(); err != nil || got != "https://pig.promptless.ai" {
		t.Fatalf("%s %v", got, err)
	}
}
func TestInvalidConfigFailsEvenWithEnvironmentOverride(t *testing.T) {
	privateHome(t)
	root := t.TempDir()
	t.Setenv("PLUGIN_ROOT", root)
	t.Setenv("PROMPTLESS_WORKER_BASE_URL", "https://override.test")
	mustWrite(t, filepath.Join(root, runtimeConfigName), []byte(`{"schema_version":1,"worker_base_url":"https://secret:literal-secret@worker.test"}`))
	if _, err := workerURL(); err == nil || !strings.Contains(err.Error(), runtimeConfigName) || strings.Contains(err.Error(), "literal-secret") {
		t.Fatalf("error: %v", err)
	}
	exe := filepath.Join(root, "executable")
	mustWrite(t, exe, []byte("runtime"))
	if err := installHookExecutable(exe, root); err == nil {
		t.Fatal("retained invalid config")
	}
}
func TestInstalledRuntimeConfigDiscovery(t *testing.T) {
	privateHome(t)
	root := t.TempDir()
	mustWrite(t, filepath.Join(root, runtimeConfigName), []byte(`{"schema_version":1}`))
	for _, exe := range []string{filepath.Join(root, "runtime", executableName), filepath.Join(root, "runtime", "bin", "darwin-arm64", executableName)} {
		if got := installedPluginRoot(exe); got != root {
			t.Fatalf("%s: %s", exe, got)
		}
	}
	if got := installedPluginRoot(filepath.Join(root, "other", executableName)); got != "" {
		t.Fatal(got)
	}
}

func TestPackagedWorkerReceivesCheckInAfterCacheDeletion(t *testing.T) {
	privateHome(t)
	w := newTestWorker(t, true)
	seedEnrollment(t, w, Codex)
	t.Setenv("PROMPTLESS_WORKER_BASE_URL", "")
	root := t.TempDir()
	t.Setenv("PLUGIN_ROOT", root)
	mustWrite(t, filepath.Join(root, runtimeConfigName), legacyJSON(object{"schema_version": 1, "worker_base_url": w.server.URL}))
	exe := filepath.Join(root, "executable")
	mustWrite(t, exe, []byte("runtime"))
	if err := installHookExecutable(exe, root); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err := ensure(Codex, true, false, false, &output{stdout: io.Discard, stderr: io.Discard}); err != nil {
			t.Fatal(err)
		}
		if err := os.RemoveAll(root); err != nil {
			t.Fatal(err)
		}
	}
	if got := len(w.at("/v0/host-enrollment/check-ins")); got != 2 {
		t.Fatalf("check-ins: %d", got)
	}
}
