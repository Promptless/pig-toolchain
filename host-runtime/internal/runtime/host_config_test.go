package runtime

import (
	"io"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func symlink(t *testing.T, target, link string) {
	t.Helper()
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
}

func TestDesktopDiscoveryFollowsSymlinksWithoutCyclesOrDuplicates(t *testing.T) {
	home := privateHome(t)
	t.Setenv("CODEX_HOME", home)
	archive := t.TempDir()
	nested := t.TempDir()
	file := filepath.Join(nested, "session.jsonl")
	mustWrite(t, file, []byte("{}\n"))
	symlink(t, archive, filepath.Join(home, "archived_sessions"))
	symlink(t, nested, filepath.Join(archive, "nested"))
	symlink(t, archive, filepath.Join(nested, "cycle"))
	symlink(t, file, filepath.Join(archive, "alias.jsonl"))
	symlink(t, filepath.Join(home, "missing"), filepath.Join(archive, "broken.jsonl"))
	paths, complete := scanSources(Codex, time.Now().Add(time.Second), true)
	if !complete || !reflect.DeepEqual(paths, []string{resolvePath(file)}) {
		t.Fatalf("paths=%v complete=%v", paths, complete)
	}
	paths, complete = scanSources(Codex, time.Now().Add(-time.Second), true)
	if complete || len(paths) != 0 {
		t.Fatalf("expired scan: %v %v", paths, complete)
	}
}

func TestDesktopDiscoveryRetainsHostAndAgeFilters(t *testing.T) {
	privateHome(t)
	root := desktopRoots()[0]
	if err := os.MkdirAll(root, 0700); err != nil {
		t.Fatal(err)
	}
	archive := t.TempDir()
	file := filepath.Join(archive, "audit.jsonl")
	mustWrite(t, file, []byte("{}\n"))
	mustWrite(t, filepath.Join(archive, "other.jsonl"), []byte("{}\n"))
	symlink(t, archive, filepath.Join(root, "session"))
	if paths, complete := scanSources(ClaudeDesktop, time.Time{}, false); !complete || len(paths) != 0 {
		t.Fatalf("active files included: %v %v", paths, complete)
	}
	old := time.Now().Add(-24 * time.Hour)
	if err := os.Chtimes(file, old, old); err != nil {
		t.Fatal(err)
	}
	if paths, complete := scanSources(ClaudeDesktop, time.Time{}, false); !complete || !reflect.DeepEqual(paths, []string{resolvePath(file)}) {
		t.Fatalf("audit discovery: %v %v", paths, complete)
	}
}

func TestClaudeConfiguredRootDiscoveryAndCleanup(t *testing.T) {
	for _, override := range []string{"unset", "", "~/Claude [work profile]"} {
		t.Run(override, func(t *testing.T) {
			home := privateHome(t)
			root := filepath.Join(home, ".claude")
			if override == "unset" {
				if err := os.Unsetenv("CLAUDE_CONFIG_DIR"); err != nil {
					t.Fatal(err)
				}
			} else {
				t.Setenv("CLAUDE_CONFIG_DIR", override)
				if override != "" {
					root = filepath.Join(home, "Claude [work profile]")
				}
			}
			dir := filepath.Join(root, "projects", "project")
			if err := os.MkdirAll(dir, 0700); err != nil {
				t.Fatal(err)
			}
			transcript := resolvePath(filepath.Join(dir, "session.jsonl"))
			mustWrite(t, transcript, []byte("configured root\n"))
			transcript = resolvePath(transcript)
			settings := filepath.Join(root, "settings.json")
			mustWrite(t, settings, []byte(`{"env":{"PROMPTLESS_MANAGED_HOST_ENROLLMENT":"1"},"theme":"dark"}`))
			if override != "unset" && override != "" {
				decoy := filepath.Join(home, ".claude", "projects", "old")
				if err := os.MkdirAll(decoy, 0700); err != nil {
					t.Fatal(err)
				}
				mustWrite(t, filepath.Join(decoy, "decoy.jsonl"), []byte("wrong root\n"))
				mustWrite(t, filepath.Join(home, ".claude", "settings.json"), []byte(`{"theme":"light"}`))
			}
			desktop := desktopRoots()
			worker := newTestWorker(t, false)
			seedEnrollment(t, worker, Claude)
			out := &output{stdout: io.Discard, stderr: io.Discard}
			if err := ensure(Claude, true, false, false, out); err != nil {
				t.Fatal(err)
			}
			status := configStatus(Claude)
			if status["path"] != settings || status["managed_config_detected"] != false {
				t.Fatal(status)
			}
			data, err := loadState(settings)
			if err != nil || data["theme"] != "dark" || data["env"] != nil {
				t.Fatalf("settings=%v err=%v", data, err)
			}
			complete, err := collect(Claude, "session_start", HookContext{}, true, true, out)
			if err != nil || !complete {
				t.Fatalf("complete=%v err=%v", complete, err)
			}
			uploads := worker.at("/v0/traces/batches")
			if len(uploads) != 1 {
				t.Fatalf("uploads=%v", uploads)
			}
			chunks := uploads[0].Body["chunks"].([]any)
			if len(chunks) != 1 || obj(chunks[0])["source_path_hash"] != hash([]byte(transcript)) {
				t.Fatal(chunks)
			}
			if !reflect.DeepEqual(desktopRoots(), desktop) {
				t.Fatal("desktop roots changed")
			}
			if override != "unset" && override != "" {
				decoy, err := os.ReadFile(filepath.Join(home, ".claude", "settings.json"))
				if err != nil || string(decoy) != `{"theme":"light"}` {
					t.Fatal("default settings changed", err)
				}
			}
		})
	}
}
