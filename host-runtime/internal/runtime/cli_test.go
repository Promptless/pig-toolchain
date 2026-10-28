package runtime

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	goruntime "runtime"
	"strings"
	"testing"
	"time"
)

func TestCLICollectionCompletionExitCodes(t *testing.T) {
	for _, host := range []Host{Codex, Claude, ClaudeDesktop, Cursor} {
		for _, command := range []string{"collect", "session-start"} {
			for _, complete := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/%s/complete=%v", host, command, complete), func(t *testing.T) {
					f := newCursorFixture(t)
					for _, key := range []string{"PLUGIN_ROOT", "CURSOR_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "CLAUDE_CONFIG_DIR"} {
						t.Setenv(key, "")
					}
					t.Setenv("CODEX_HOME", filepath.Join(f.home, ".codex"))
					budget := "5"
					if !complete {
						budget = "0"
					}
					t.Setenv("PROMPTLESS_HOST_RUNTIME_COLLECT_DEADLINE_SECONDS", budget)
					w := newTestWorker(t, false)
					seedEnrollment(t, w, host.enrollment())
					path := filepath.Join(f.home, "audit.jsonl")
					mustWrite(t, path, []byte("{\"message\":\"hello\"}\n"))
					event := object{"session_id": "session", "transcript_path": path}
					if host == Cursor {
						f.session("session", 1)
						event = object{"conversation_id": "session"}
					}
					raw, _ := json.Marshal(event)
					args := []string{command, "--host", string(host), "--supervised"}
					if command == "collect" {
						args = append(args, "--lifecycle", "stop")
					}
					var stdout, stderr bytes.Buffer
					code := Main(args, bytes.NewReader(raw), &stdout, &stderr)
					want := 0
					if host == Cursor && !complete {
						want = 1
					}
					if code != want {
						t.Fatalf("exit=%d want=%d: %s %s", code, want, &stdout, &stderr)
					}
					diagnostics, err := os.ReadFile(stateSibling("host-runtime-diagnostics.jsonl"))
					if err != nil {
						t.Fatal(err)
					}
					expected := "trace_upload_partial"
					if complete {
						expected = "trace_upload_complete"
					}
					found := false
					for _, line := range bytes.Split(diagnostics, []byte("\n")) {
						entry, _ := decodeObject(line)
						if entry["host"] == string(host) && entry["status"] == expected {
							found = true
						}
					}
					if !found {
						t.Fatalf("missing %s diagnostic for %s: %s", expected, host, diagnostics)
					}
					if len(w.at("/v0/traces/batches")) == 0 {
						t.Fatal("test never reached native collection")
					}
				})
			}
		}
	}
}

func TestDetachedCommandAndInput(t *testing.T) {
	privateHome(t)
	for _, tc := range []struct{ in, want []string }{
		{[]string{"session-start", "--host", "codex", "--detach"}, []string{"session-start", "--host", "codex", "--supervised"}},
		{[]string{"collect", "--detach", "--host", "claude", "--quiet", "--lifecycle", "stop"}, []string{"collect", "--host", "claude", "--quiet", "--lifecycle", "stop", "--supervised"}},
		{[]string{"cursor-notify", "--lifecycle", "stop", "--detach"}, []string{"cursor-notify", "--lifecycle", "stop"}},
	} {
		if got := detachedCommand(tc.in); !reflect.DeepEqual(got, tc.want) {
			t.Fatalf("child=%v want=%v", got, tc.want)
		}
	}
	path := filepath.Join(t.TempDir(), "child-output")
	t.Setenv("PIG_TEST_DETACHED_OUTPUT", path)
	raw := []byte(" {\"session_id\":\"opaque\",\"transcript_path\":\"path with spaces\"}\n")
	if err := spawnDetached([]string{"-test.run=^TestDetachedInputHelper$", "--", "argument with spaces"}, raw); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(path)
		if err == nil {
			result, err := decodeObject(data)
			if err != nil {
				t.Fatal(err)
			}
			if result["input"] != string(raw) || result["argument"] != "argument with spaces" {
				t.Fatal(result)
			}
			if exists(ledgerPath()) {
				t.Fatal("launcher touched collection state")
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("detached child did not receive input")
}
func TestDetachedInputHelper(t *testing.T) {
	path := os.Getenv("PIG_TEST_DETACHED_OUTPUT")
	if path == "" {
		return
	}
	raw, err := io.ReadAll(os.Stdin)
	if err != nil {
		os.Exit(2)
	}
	data, _ := json.Marshal(object{"input": string(raw), "argument": os.Args[len(os.Args)-1]})
	if atomicWrite(path, data) != nil {
		os.Exit(3)
	}
	os.Exit(0)
}
func TestDetachedLaunchFailurePersistsDiagnostics(t *testing.T) {
	home := privateHome(t)
	missing := filepath.Join(home, "missing-temp-directory")
	for _, key := range []string{"TMPDIR", "TMP", "TEMP"} {
		t.Setenv(key, missing)
	}
	var stdout, stderr bytes.Buffer
	if code := Main([]string{"collect", "--host", "codex", "--detach", "--quiet"}, strings.NewReader("{}"), &stdout, &stderr); code != 1 {
		t.Fatalf("exit=%d", code)
	}
	status, err := loadState(stateSibling("last-bootstrap-status.json"))
	if err != nil {
		t.Fatal(err)
	}
	if status["status"] != "error" || status["reason"] != "collector_failed" || status["host"] != "codex" {
		t.Fatal(status)
	}
	for _, name := range []string{"last-bootstrap-status.json", "host-runtime-diagnostics.jsonl"} {
		info, err := os.Stat(stateSibling(name))
		if err != nil {
			t.Fatal(err)
		}
		if goruntime.GOOS != "windows" && info.Mode().Perm() != 0600 {
			t.Fatal(info.Mode())
		}
	}
	if exists(ledgerPath()) {
		t.Fatal("failed launcher touched ledger")
	}
}
func TestPendingNoticesAreLocalAndClaimedOnce(t *testing.T) {
	home := privateHome(t)
	t.Setenv("PLUGIN_ROOT", home)
	t.Setenv("CURSOR_PLUGIN_ROOT", "")
	t.Setenv("CLAUDE_PLUGIN_ROOT", "")
	if err := writeState(filepath.Join(home, "hub.managed-runtimes.json"), object{"managed_runtimes": []object{{"id": "host-runtime", "status": "included", "plugin_version": "0.2.0"}}}); err != nil {
		t.Fatal(err)
	}
	if err := writeState(statePath(), object{"credentials": object{"credential": object{"is_internal_promptless_user": true}}, "last_seen_plugin_versions": object{"codex": "0.1.0"}, firstPendingKey: object{"codex": "configured"}}); err != nil {
		t.Fatal(err)
	}
	original := http.DefaultTransport
	http.DefaultTransport = roundTripFunc(func(*http.Request) (*http.Response, error) {
		t.Error("local notice attempted network request")
		return nil, errors.New("unexpected network")
	})
	t.Cleanup(func() { http.DefaultTransport = original })
	for i := 0; i < 2; i++ {
		var stdout, stderr bytes.Buffer
		out := &output{stdout: &stdout, stderr: &stderr}
		pendingNotices(Codex, out)
		out.flush()
		if i == 1 {
			if stdout.Len() != 0 {
				t.Fatal("notice repeated:", stdout.String())
			}
			continue
		}
		for _, fragment := range []string{"updated to v0.2.0 (was v0.1.0)", "telemetry is now active", "welcome promptless pigfooder"} {
			if !strings.Contains(stdout.String(), fragment) {
				t.Fatalf("missing %q: %s", fragment, &stdout)
			}
		}
	}
	state, err := loadState(statePath())
	if err != nil {
		t.Fatal(err)
	}
	if str(obj(state[firstShownKey])["codex"]) == "" || str(obj(state[welcomeShownKey])["0.2.0"]) == "" || len(obj(state[firstPendingKey])) != 0 {
		t.Fatal(state)
	}
	if exists(ledgerPath()) {
		t.Fatal("local notices touched ledger")
	}
}
func TestLinuxBrowserSessionDetection(t *testing.T) {
	variables := []string{"DISPLAY", "WAYLAND_DISPLAY", "MIR_SOCKET", "WSL_INTEROP"}
	for _, key := range append(variables, "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER") {
		t.Setenv(key, "")
	}
	t.Setenv("BROWSER", "xdg-open")
	if browserSessionAvailable("linux") {
		t.Fatal("headless Linux considered graphical")
	}
	for _, key := range variables {
		t.Run(key, func(t *testing.T) {
			t.Setenv(key, "session")
			if !browserSessionAvailable("linux") {
				t.Fatal("graphical/WSL session unavailable")
			}
			t.Setenv("PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER", "0")
			if browserSessionAvailable("linux") {
				t.Fatal("explicit disable ignored")
			}
		})
	}
	t.Setenv("PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER", "1")
	if !browserSessionAvailable("linux") {
		t.Fatal("explicit enable ignored")
	}
}
