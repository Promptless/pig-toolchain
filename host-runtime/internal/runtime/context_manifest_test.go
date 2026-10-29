package runtime

import (
	"bytes"
	"compress/gzip"
	"encoding/base64"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func contextFixture(t *testing.T, w *testWorker) (string, uploadOptions) {
	t.Helper()
	root := t.TempDir()
	skill := filepath.Join(root, "skills", "test")
	if err := os.MkdirAll(skill, 0700); err != nil {
		t.Fatal(err)
	}
	mustWrite(t, filepath.Join(skill, "SKILL.md"), []byte("---\nname: test\ndescription: Test skill\n---\n# Test\n"))
	o := testUploadOptions(w, filepath.Join(t.TempDir(), "ledger.json"))
	o.Policy.ContextVersion = 1
	o.Context.SessionID = "session"
	return root, o
}
func TestContextAcknowledgmentRetryAndLedgerIsolation(t *testing.T) {
	w := newTestWorker(t, true)
	root, o := contextFixture(t, w)
	source := filepath.Join(t.TempDir(), "trace.jsonl")
	mustWrite(t, source, []byte("{\"role\":\"user\"}\n"))
	if _, err := uploadPaths([]string{source}, o); err != nil {
		t.Fatal(err)
	}
	before, _ := loadLedger(o.Ledger)
	w.change(func() { w.rejectContext = true })
	if _, err := uploadSessionContext(root, o); err == nil {
		t.Fatal("accepted missing hash")
	}
	ledger, _ := loadLedger(o.Ledger)
	if len(ledger.ContextHashes) != 0 {
		t.Fatal("rejected context persisted")
	}
	if !bytes.Equal(legacyJSON(before.Sources), legacyJSON(ledger.Sources)) {
		t.Fatal("context changed source offsets")
	}
	w.change(func() { w.rejectContext = false })
	if sent, err := uploadSessionContext(root, o); err != nil || !sent {
		t.Fatalf("retry: %v %v", sent, err)
	}
	if sent, err := uploadSessionContext(root, o); err != nil || sent {
		t.Fatalf("duplicate: %v %v", sent, err)
	}
	if counts, err := uploadPaths([]string{source}, o); err != nil || counts.Batches != 0 {
		t.Fatal("replayed transcript", counts, err)
	}
	mustWrite(t, filepath.Join(root, "skills", "test", "reference.md"), []byte("Changed reference"))
	if sent, err := uploadSessionContext(root, o); err != nil || !sent {
		t.Fatal("changed skill not uploaded", sent, err)
	}
	ledger, _ = loadLedger(o.Ledger)
	if len(ledger.ContextHashes) != 1 {
		t.Fatal(ledger.ContextHashes)
	}
	o.Context.SessionID = "another-session"
	if sent, err := uploadSessionContext(root, o); err != nil || !sent {
		t.Fatal("new session missing", sent, err)
	}
}
func TestContextArtifactContentAndExclusions(t *testing.T) {
	w := newTestWorker(t, true)
	root, o := contextFixture(t, w)
	skill := filepath.Join(root, "skills", "test")
	mustWrite(t, filepath.Join(skill, "reference.md"), []byte("safe reference"))
	mustWrite(t, filepath.Join(skill, ".env"), []byte("secret-file"))
	mustWrite(t, filepath.Join(skill, "bad.md"), []byte(strings.Repeat("padding", 20000)+"\ntoken=abcdefghijklmnopqrstuv1234"))
	outside := filepath.Join(t.TempDir(), "outside.md")
	mustWrite(t, outside, []byte("outside-secret"))
	// Windows CI may lack symlink privileges; content exclusions still run there.
	_ = os.Symlink(outside, filepath.Join(skill, "link.md"))
	manifest, artifacts, err := sessionContext(root, o)
	if err != nil || len(artifacts) != 1 {
		t.Fatal(manifest, err)
	}
	compressed, _ := base64.StdEncoding.DecodeString(str(artifacts[0]["content_base64"]))
	if hash(compressed) != artifacts[0]["content_sha256"] {
		t.Fatal("digest mismatch")
	}
	z, err := gzip.NewReader(bytes.NewReader(compressed))
	if err != nil {
		t.Fatal(err)
	}
	defer z.Close()
	raw, _ := io.ReadAll(z)
	bundle, err := decodeObject(raw)
	if err != nil {
		t.Fatal(err)
	}
	files := bundle["files"].([]any)
	if len(files) != 2 {
		t.Fatal(string(raw))
	}
	for _, value := range files {
		file := obj(value)
		b, _ := base64.StdEncoding.DecodeString(str(file["content_base64"]))
		if bytes.Contains(b, []byte("secret")) || bytes.Contains(b, []byte("token=")) {
			t.Fatal("sensitive content uploaded")
		}
	}
	again, _, _ := sessionContext(root, o)
	if manifest["manifest_hash"] != again["manifest_hash"] {
		t.Fatal("unstable hash")
	}
	o.Policy.ContextVersion = 0
	if m, _, err := sessionContext(root, o); err != nil || m != nil {
		t.Fatal("ungated context")
	}
	o.Policy.ContextVersion = 1
	o.Context.SessionID = ""
	if m, _, _ := sessionContext(root, o); m != nil {
		t.Fatal("missing session accepted")
	}
	o.Context.SessionID = "child"
	o.Context.ParentSessionID = "parent"
	o.Lifecycle = "subagent_stop"
	if m, _, _ := sessionContext(root, o); m["session_id"] != "parent" {
		t.Fatal("subagent context assigned to wrong session")
	}
	o.Deadline = time.Now().Add(-time.Second)
	if _, _, err := sessionContext(root, o); err == nil {
		t.Fatal("ignored deadline")
	}
}
func TestCollectRetriesOptionalContextWithoutTraceReplay(t *testing.T) {
	privateHome(t)
	w := newTestWorker(t, true)
	root, _ := contextFixture(t, w)
	t.Setenv("PLUGIN_ROOT", root)
	seedEnrollment(t, w, Codex)
	w.change(func() { w.contextVersion = 1; w.rejectContext = true })
	path := filepath.Join(t.TempDir(), "trace.jsonl")
	mustWrite(t, path, []byte("{\"role\":\"user\"}\n"))
	context := HookContext{SessionID: "session", Transcript: path}
	out := &output{stdout: io.Discard, stderr: io.Discard}
	if complete, err := collect(Codex, "", context, true, true, out); err != nil || complete {
		t.Fatal(complete, err)
	}
	w.change(func() { w.rejectContext = false })
	if complete, err := collect(Codex, "", context, true, true, out); err != nil || !complete {
		t.Fatal(complete, err)
	}
	traces, contexts := 0, 0
	for _, r := range w.at("/v0/traces/batches") {
		if _, ok := r.Body["session_context_manifests"]; ok {
			contexts++
		} else {
			traces++
		}
	}
	if traces != 1 || contexts != 2 {
		t.Fatal(traces, contexts)
	}
}
func TestContextPolicyRejectsUnsupportedVersions(t *testing.T) {
	for _, value := range []any{true, "1", 0, 2, nil, 1} {
		p := object{"policy": object{"schema_version": 2, "policy_version": 1, "expires_at": time.Now().Add(time.Hour).Format(time.RFC3339), "enabled_hosts": []any{"codex"}, "trace_context_manifest_version": value}}
		policy, err := validatePolicy(p, Codex)
		if value == 1 {
			if err != nil || policy.ContextVersion != 1 {
				t.Fatal(policy, err)
			}
		} else if err == nil {
			t.Fatalf("accepted %v", value)
		}
	}
}

func TestContextAcknowledgmentRequiresExactProof(t *testing.T) {
	request := object{"batch_id": "batch"}
	p := Policy{Version: 7}
	digest := strings.Repeat("a", 64)
	valid := func() object {
		return object{"accepted": true, "batch_id": "batch", "policy_version": 7, "raw_artifact_count": 0, "skipped_record_count": 0, "raw_context_artifact_count": 1, "unparsed_record_count": 0, "acknowledged_ranges": []any{}, "accepted_session_context_manifest_hashes": []any{digest}}
	}
	if err := validateContextAck(valid(), request, p, digest, 1); err != nil {
		t.Fatal(err)
	}
	for key, value := range (object{"accepted": false, "batch_id": "wrong", "policy_version": true, "raw_artifact_count": 1, "skipped_record_count": 1, "raw_context_artifact_count": 0, "unparsed_record_count": -1, "acknowledged_ranges": nil, "accepted_session_context_manifest_hashes": []any{strings.Repeat("b", 64)}}) {
		response := valid()
		response[key] = value
		if err := validateContextAck(response, request, p, digest, 1); err == nil {
			t.Fatal("accepted invalid", key)
		}
	}
}
func TestContextFileAndInventoryLimits(t *testing.T) {
	rootPath := t.TempDir()
	root, err := os.OpenRoot(rootPath)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	file, err := os.Create(filepath.Join(rootPath, "large.md"))
	if err != nil {
		t.Fatal(err)
	}
	if err = file.Truncate(maxContextFile + 1); err != nil {
		t.Fatal(err)
	}
	file.Close()
	if _, err := contextFile(root, "large.md", maxContextSource); err == nil {
		t.Fatal("oversized file accepted")
	}
	for i := 0; i < maxContextFiles+5; i++ {
		mustWrite(t, filepath.Join(rootPath, fmt.Sprintf("file%04d.md", i)), []byte("ok"))
	}
	bundle, _, err := skillBundle(root, rootPath, "test", time.Now().Add(5*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if bundle["truncated"] != true || bundle["file_count"].(int) > maxContextFiles {
		t.Fatal("unbounded inventory")
	}
}
