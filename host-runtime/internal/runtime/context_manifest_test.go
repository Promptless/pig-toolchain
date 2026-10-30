package runtime

import (
	"bytes"
	"compress/gzip"
	"encoding/base64"
	"fmt"
	"io"
	"math/rand"
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
	for name, content := range map[string]string{
		"config.json":  `{"api_key":"abcdefghijklmnopqrstuv1234"}`,
		"nested.json":  `{"settings":{"token": "abcdefghijklmnopqrstuv1234"}}`,
		"single.yaml":  "'password': 'abcdefghijklmnopqrstuv1234'",
		"header.md":    "Authorization: Bearer abcdefghijklmnopqrstuv1234",
		"basic.md":     "Authorization: Basic dXNlcjpwYXNz",
		"headers.json": `{"Authorization": "Bearer abcdefghijklmnopqrstuv1234"}`,
	} {
		mustWrite(t, filepath.Join(skill, name), []byte(content))
	}
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
	if _, _, err := contextFile(root, "large.md", maxContextSource); err == nil {
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

func TestCollectCursorSubagentContextUsesParentSession(t *testing.T) {
	f := newCursorFixture(t)
	f.session("child", 1)
	w := newTestWorker(t, true)
	root, _ := contextFixture(t, w)
	t.Setenv("PLUGIN_ROOT", root)
	seedEnrollment(t, w, Cursor)
	w.change(func() { w.contextVersion = 1 })
	hook := HookContext{SessionID: "parent", AgentID: "child", GenerationID: "g"}
	out := &output{stdout: io.Discard, stderr: io.Discard}
	if complete, err := collect(Cursor, "subagent_stop", hook, true, true, out); err != nil || !complete {
		t.Fatal(complete, err)
	}
	contexts := 0
	for _, r := range w.at("/v0/traces/batches") {
		manifests, ok := r.Body["session_context_manifests"].([]any)
		if !ok {
			continue
		}
		contexts++
		if len(manifests) != 1 || obj(manifests[0])["session_id"] != "parent" {
			t.Fatal("context lost original parent identity", manifests)
		}
		artifacts := r.Body["context_artifacts"].([]any)
		if len(artifacts) != 1 || obj(artifacts[0])["session_id"] != "parent" {
			t.Fatal("skill artifacts assigned to child", artifacts)
		}
	}
	if contexts != 1 {
		t.Fatal("missing context upload", contexts)
	}
	for _, row := range journalRows(t, "child") {
		if row["session_id"] != "child" {
			t.Fatal("transcript lost child identity", row)
		}
	}
}

func TestContextDiagnosticBudgetsPermitAcknowledgedUploads(t *testing.T) {
	w := newTestWorker(t, true)
	root, o := contextFixture(t, w)
	o.Deadline = time.Now().Add(time.Minute)
	// Escaped Unicode paths exercise serialized bytes rather than string lengths.
	for i := range 4 {
		dir := filepath.Join(root, "skills", fmt.Sprint(i))
		if err := os.MkdirAll(dir, 0700); err != nil {
			t.Fatal(err)
		}
		mustWrite(t, filepath.Join(dir, "SKILL.md"), []byte("# Test"))
		for j := range 128 {
			path := filepath.Join(dir, fmt.Sprintf("%03d-%s.md", j, strings.Repeat("界", 60)))
			mustWrite(t, path, []byte("token=abcdefghijklmnopqrstuv1234"))
		}
	}
	manifest, artifacts, err := sessionContext(root, o)
	if err != nil {
		t.Fatal(err)
	}
	diagnostics := manifest["diagnostics"].([]object)
	if len(legacyJSON(diagnostics)) > maxContextDiagnostics {
		t.Fatal("manifest diagnostics exceeded byte budget")
	}
	if len(diagnostics) == 0 || diagnostics[len(diagnostics)-1]["code"] != "context_diagnostics_truncated" {
		t.Fatal("missing truncation notice")
	}
	for _, artifact := range artifacts {
		compressed, _ := base64.StdEncoding.DecodeString(str(artifact["content_base64"]))
		z, err := gzip.NewReader(bytes.NewReader(compressed))
		if err != nil {
			t.Fatal(err)
		}
		raw, err := io.ReadAll(z)
		z.Close()
		if err != nil {
			t.Fatal(err)
		}
		bundle, err := decodeObject(raw)
		if err != nil || len(legacyJSON(bundle["diagnostics"])) > maxContextDiagnostics {
			t.Fatal("bundle diagnostics exceeded byte budget", err)
		}
	}
	if len(legacyJSON(contextRequest(manifest, artifacts, o))) > maxTransport {
		t.Fatal("complete request exceeded transport budget")
	}
	if sent, err := uploadSessionContext(root, o); err != nil || !sent {
		t.Fatal("bounded context was not acknowledged", sent, err)
	}
	if sent, err := uploadSessionContext(root, o); err != nil || sent {
		t.Fatal("bounded context did not deduplicate", sent, err)
	}
}

func TestContextInventoryUsesSerializedByteBudget(t *testing.T) {
	w := newTestWorker(t, true)
	root, o := contextFixture(t, w)
	o.Deadline = time.Now().Add(time.Minute)
	for i := range 64 {
		dir := filepath.Join(root, "skills", fmt.Sprint(i))
		if err := os.MkdirAll(dir, 0700); err != nil {
			t.Fatal(err)
		}
		mustWrite(t, filepath.Join(dir, "SKILL.md"), []byte("---\nname: test\ndescription: "+strings.Repeat("界", 1300)+"\n---\n"))
	}
	manifest, artifacts, err := sessionContext(root, o)
	if err != nil {
		t.Fatal(err)
	}
	skills := manifest["available_skills"].([]object)
	if len(skills) >= 64 || len(legacyJSON(skills)) > maxContextInventory {
		t.Fatal("inventory was not bounded by serialized bytes", len(skills))
	}
	if len(legacyJSON(contextRequest(manifest, artifacts, o))) > maxTransport {
		t.Fatal("complete request exceeded transport budget")
	}
}

func TestContextArtifactAdmissionIncludesRequestEnvelope(t *testing.T) {
	w := newTestWorker(t, true)
	root, o := contextFixture(t, w)
	o.Deadline = time.Now().Add(time.Minute)
	o.Context.ParentSessionID, o.Context.AgentID = "parent", "agent"
	// Hook metadata occurs in both the envelope and manifest. Make that cost
	// large enough that an artifact fitting its own budget cannot also fit it.
	o.Context.AgentType = strings.Repeat("x", 2<<20)
	payload := make([]byte, 5<<20)
	_, _ = rand.New(rand.NewSource(1)).Read(payload)
	mustWrite(t, filepath.Join(root, "skills", "test", "reference.bin"), payload)
	manifest, artifacts, err := captureSessionContext(root, o, o.Deadline)
	if err != nil {
		t.Fatal(err)
	}
	if len(artifacts) != 0 || len(manifest["raw_artifacts"].([]object)) != 0 {
		t.Fatal("artifact admission ignored envelope bytes")
	}
	for _, skill := range manifest["available_skills"].([]object) {
		if _, ok := skill["artifact_id"]; ok {
			t.Fatal("inventory references omitted artifact")
		}
	}
	if len(legacyJSON(contextRequest(manifest, artifacts, o))) > maxTransport {
		t.Fatal("complete request exceeded transport budget")
	}
	hashInput := clone(manifest)
	delete(hashInput, "manifest_hash")
	delete(hashInput, "captured_at")
	if hash(contextJSON(hashInput)) != manifest["manifest_hash"] {
		t.Fatal("hash does not describe admitted manifest")
	}
}

func TestContextSourceBudgetIncludesExcludedFileReads(t *testing.T) {
	path := t.TempDir()
	mustWrite(t, filepath.Join(path, "SKILL.md"), []byte("# Test"))
	excluded := append([]byte("token=abcdefghijklmnopqrstuv1234\n"), bytes.Repeat([]byte("x"), 10<<20)...)
	for i := range 3 {
		mustWrite(t, filepath.Join(path, fmt.Sprintf("excluded%d.md", i)), excluded)
	}
	// This otherwise safe file fits the per-file limit, but cannot fit after
	// inspecting the three excluded files. Exclusion must not replenish budget.
	mustWrite(t, filepath.Join(path, "z-reference.md"), bytes.Repeat([]byte("x"), 3<<20))
	root, err := os.OpenRoot(path)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	if _, inspected, err := contextFile(root, "excluded0.md", maxContextSource); err == nil || inspected != len(excluded) {
		t.Fatal("excluded content did not consume its read budget", inspected, err)
	}
	bundle, _, err := skillBundle(root, path, "test", time.Now().Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	files := bundle["files"].([]object)
	if len(files) != 1 || files[0]["path"] != "SKILL.md" {
		t.Fatal("excluded reads bypassed cumulative source budget", len(files))
	}
}

func TestContextScanDeadlinePreservesSendablePartialManifest(t *testing.T) {
	w := newTestWorker(t, true)
	root, o := contextFixture(t, w)
	manifest, artifacts, err := captureSessionContext(root, o, time.Now().Add(-time.Second))
	if err != nil || manifest == nil {
		t.Fatal("expired scan discarded partial context", err)
	}
	diagnostics := manifest["diagnostics"].([]object)
	if len(diagnostics) != 1 || diagnostics[0]["code"] != "context_capture_deadline_exceeded" {
		t.Fatal("partial capture not identified", diagnostics)
	}
	request := contextRequest(manifest, artifacts, o)
	response, err := requestJSON("POST", o.URL, o.Credential, request)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateContextAck(response, request, o.Policy, str(manifest["manifest_hash"]), len(artifacts)); err != nil {
		t.Fatal("partial context not acknowledged", err)
	}
}

func TestCollectSendsTranscriptBacklogBeforeOptionalContext(t *testing.T) {
	privateHome(t)
	w := newTestWorker(t, true)
	root, _ := contextFixture(t, w)
	t.Setenv("PLUGIN_ROOT", root)
	seedEnrollment(t, w, Codex)
	w.change(func() { w.contextVersion = 1 })
	parent := filepath.Join(t.TempDir(), "parent.jsonl")
	child := filepath.Join(t.TempDir(), "child.jsonl")
	mustWrite(t, parent, []byte("{\"role\":\"user\"}\n"))
	mustWrite(t, child, []byte("{\"role\":\"assistant\"}\n"))
	hook := HookContext{SessionID: "parent", Transcript: parent, AgentTranscript: child}
	out := &output{stdout: io.Discard, stderr: io.Discard}
	if complete, err := collect(Codex, "", hook, true, true, out); err != nil || !complete {
		t.Fatal(complete, err)
	}
	requests := w.at("/v0/traces/batches")
	if len(requests) != 3 {
		t.Fatal("missing transcript or context uploads", len(requests))
	}
	for i, request := range requests {
		_, context := request.Body["session_context_manifests"]
		if context != (i == 2) {
			t.Fatal("optional context ran before transcript backlog", i)
		}
	}
}
