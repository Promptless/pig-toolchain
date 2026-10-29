package runtime

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"errors"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func TestLegacyLedgerRetainsOffsetsAndDropsObsoleteProvenance(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "ledger.json")
	source := filepath.Join(root, "session.jsonl")
	key := hash([]byte(source))
	if err := writeState(path, object{"schema_version": 1, "host_baselines": []string{"codex"}, "sources": object{key: object{"path": source, "end_offset": 123, "provenance_only": true, "instruction_hub_release_markers": []object{{"start_offset": 123}}}}}); err != nil {
		t.Fatal(err)
	}
	ledger, err := loadLedger(path)
	if err != nil {
		t.Fatal(err)
	}
	if got := obj(ledger.Sources[key]); len(got) != 2 || number(got["end_offset"]) != 123 || got["path"] != source {
		t.Fatal(got)
	}
	if err = ledger.write(); err != nil {
		t.Fatal(err)
	}
	persisted, _ := loadState(path)
	if _, ok := persisted["host_baselines"]; ok {
		t.Fatal("obsolete baseline persisted")
	}
}
func TestSourceChunkBoundariesAreStableAcrossRetries(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "session.jsonl")
	record := append(bytes.Repeat([]byte("x"), 69999), '\n')
	mustWrite(t, path, bytes.Repeat(record, 65))
	ledger, _ := loadLedger(filepath.Join(root, "ledger.json"))
	first, err := nextSourceEvent(ledger, path, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	retry, err := nextSourceEvent(ledger, path, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(first, retry) || first.Start != 0 || first.End != 59*70000 {
		t.Fatal("unstable first chunk boundary")
	}
	if err = ledger.record(*first, first.End); err != nil {
		t.Fatal(err)
	}
	second, err := nextSourceEvent(ledger, path, time.Time{})
	if err != nil || second.Start != first.End || second.End != 65*70000 {
		t.Fatalf("second chunk: %+v %v", second, err)
	}
}
func testUploadOptions(w *testWorker, ledger string) uploadOptions {
	return uploadOptions{URL: w.server.URL + "/v0/traces/batches", Host: Codex, Metadata: metadata("", Codex), Policy: Policy{Version: 1}, Ledger: ledger, Deadline: time.Now().Add(5 * time.Second)}
}
func TestUploadDeadlineBeforeWorkAndWhileWaitingForLock(t *testing.T) {
	for _, busy := range []bool{false, true} {
		t.Run(map[bool]string{false: "expired", true: "locked"}[busy], func(t *testing.T) {
			root := t.TempDir()
			w := newTestWorker(t, false)
			options := testUploadOptions(w, filepath.Join(root, "ledger.json"))
			if busy {
				lock, err := acquireLock(options.Ledger+".lock", time.Time{}, true)
				if err != nil {
					t.Fatal(err)
				}
				defer lock.close()
				options.Deadline = time.Now().Add(30 * time.Millisecond)
			} else {
				options.Deadline = time.Now().Add(-time.Second)
			}
			// Even a missing source must not be examined after the deadline.
			counts, err := uploadPaths([]string{filepath.Join(root, "does-not-exist")}, options)
			if !errors.Is(err, errDeadline) || len(counts.Unreadable) != 0 || len(w.at("")) != 0 || exists(options.Ledger) {
				t.Fatalf("work after deadline: %+v %v", counts, err)
			}
		})
	}
}
func TestUploadPersistsFirstAcknowledgmentBeforeDeadlineStopsNextSource(t *testing.T) {
	root := t.TempDir()
	w := newTestWorker(t, false)
	options := testUploadOptions(w, filepath.Join(root, "ledger.json"))
	options.Deadline = time.Now().Add(100 * time.Millisecond)
	paths := []string{filepath.Join(root, "first.jsonl"), filepath.Join(root, "second.jsonl")}
	for _, p := range paths {
		mustWrite(t, p, []byte("{}\n"))
	}
	w.onUpload = func(object) { <-time.After(time.Until(options.Deadline) + time.Millisecond) }
	counts, err := uploadPaths(paths, options)
	if !errors.Is(err, errDeadline) || counts.Batches != 1 || len(w.at("")) != 1 {
		t.Fatalf("%+v %v", counts, err)
	}
	ledger, err := loadLedger(options.Ledger)
	if err != nil || len(ledger.Sources) != 1 || number(obj(ledger.Sources[hash([]byte(paths[0]))])["end_offset"]) != 3 {
		t.Fatal("acknowledgment was not durable", ledger, err)
	}
}

type deadlineReader struct {
	deadline time.Time
	reads    int
}

func (r *deadlineReader) Read(p []byte) (int, error) {
	r.reads++
	if r.reads == 2 {
		<-time.After(time.Until(r.deadline) + time.Millisecond)
	}
	for i := range p {
		p[i] = 'x'
	}
	return len(p), nil
}
func TestSourceDeadlineIsCheckedInsideOversizedRecord(t *testing.T) {
	deadline := time.Now().Add(20 * time.Millisecond)
	source := &deadlineReader{deadline: deadline}
	_, _, _, _, err := readRecord(bufio.NewReaderSize(source, 64<<10), sha256.New(), deadline)
	if !errors.Is(err, errDeadline) || source.reads != 2 {
		t.Fatalf("unbounded record read: %d %v", source.reads, err)
	}
}
func TestCollectAcknowledgesCurrentBeforeIdleCatchUp(t *testing.T) {
	home := privateHome(t)
	t.Setenv("CODEX_HOME", filepath.Join(home, ".codex"))
	t.Setenv("PLUGIN_ROOT", "")
	w := newTestWorker(t, false)
	c := seedEnrollment(t, w, Codex)
	current := resolvePath(filepath.Join(home, "current.jsonl"))
	mustWrite(t, current, []byte("current\n"))
	idleDir := filepath.Join(home, ".codex", "sessions")
	_ = os.MkdirAll(idleDir, 0700)
	idle := resolvePath(filepath.Join(idleDir, "idle.jsonl"))
	mustWrite(t, idle, []byte("idle\n"))
	old := time.Now().Add(-24 * time.Hour)
	_ = os.Chtimes(idle, old, old)
	ledgerPath := scopedLedger(ledgerPath(), c.Worker, c.DeploymentID, c.HostID, Codex)
	uploads := 0
	var observed error
	w.onUpload = func(body object) {
		uploads++
		chunk := obj(body["chunks"].([]any)[0])
		if uploads == 1 {
			if chunk["source_path_hash"] != hash([]byte(current)) {
				observed = errors.New("idle uploaded before current")
			}
		} else {
			ledger, err := loadLedger(ledgerPath)
			if err != nil || number(obj(ledger.Sources[hash([]byte(current))])["end_offset"]) != 8 {
				observed = errors.New("current acknowledgment not durable before catch-up")
			}
		}
	}
	out := &output{stdout: io.Discard, stderr: io.Discard}
	complete, err := collect(Codex, "stop", HookContext{Transcript: current, SessionID: "current"}, false, true, out)
	w.change(func() {
		if err != nil || !complete || observed != nil || uploads != 2 {
			t.Fatalf("complete=%v uploads=%d err=%v observed=%v", complete, uploads, err, observed)
		}
	})
}
func TestAcknowledgmentUsesSnapshotAfterSourceDeletion(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "trace.jsonl")
	mustWrite(t, path, []byte("one\ntwo\n"))
	ledger, _ := loadLedger(filepath.Join(root, "ledger.json"))
	event, err := nextSourceEvent(ledger, path, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	if err = os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err = ledger.record(*event, event.End); err != nil {
		t.Fatal(err)
	}
	source := obj(ledger.Sources[event.PathHash])
	if number(source["end_offset"]) != 8 || source["prefix_sha256"] != hash([]byte("one\ntwo\n")) {
		t.Fatal(source)
	}
}
func TestConflictRecoveryUsesSnapshotAfterSourceReplacement(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "trace.jsonl")
	mustWrite(t, path, []byte("one\ntwo\n"))
	ledger, _ := loadLedger(filepath.Join(root, "ledger.json"))
	event, err := nextSourceEvent(ledger, path, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	mustWrite(t, path, []byte("replacement\n"))
	proof := object{"start_offset": 0, "end_offset": 4, "content_sha256": hash([]byte("one\n"))}
	detail := object{"code": "trace_source_sequence_conflict", "source": "codex", "source_path_hash": event.PathHash, "requested_start_offset": 0, "requested_end_offset": 8, "acknowledged_offset": 4, "acknowledged_range": proof}
	recovered, err := reconcileConflict(ledger, Codex, *event, &HTTPError{Status: 409, Body: legacyJSON(object{"detail": detail})})
	if err != nil || !recovered || obj(ledger.Sources[event.PathHash])["prefix_sha256"] != hash([]byte("one\n")) {
		t.Fatal(recovered, err)
	}
	replay, err := nextSourceEvent(ledger, path, time.Time{})
	if err != nil || replay.Start != 0 || string(replay.Content) != "replacement\n" {
		t.Fatalf("replacement was skipped: %+v %v", replay, err)
	}
}
