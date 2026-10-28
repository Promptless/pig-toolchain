package runtime

import (
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
