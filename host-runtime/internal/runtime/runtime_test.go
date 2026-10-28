package runtime

import (
	"bytes"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func privateHome(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("APPDATA", filepath.Join(home, "AppData", "Roaming"))
	t.Setenv("LOCALAPPDATA", filepath.Join(home, "AppData", "Local"))
	t.Setenv("XDG_CONFIG_HOME", filepath.Join(home, ".config"))
	t.Setenv("CODEX_HOME", filepath.Join(home, ".codex"))
	t.Setenv("CLAUDE_CONFIG_DIR", filepath.Join(home, ".claude"))
	for _, key := range []string{"PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "CURSOR_PLUGIN_ROOT", "PROMPTLESS_CURSOR_DATABASE"} {
		t.Setenv(key, "")
	}
	t.Setenv("PROMPTLESS_HOST_RUNTIME_LEDGER", filepath.Join(home, "ledger.json"))
	return home
}
func mustWrite(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
}
func TestLegacyEncoding(t *testing.T) {
	// Literal from Python json.dumps(sort_keys=True): these bytes define persisted identities.
	value := object{"z": "é🐖", "a": []any{json.Number("1.0"), true, nil, "\n"}}
	want := `{"a": [1.0, true, null, "\n"], "z": "\u00e9\ud83d\udc16"}`
	if got := string(legacyJSON(value)); got != want {
		t.Fatalf("got %s, want %s", got, want)
	}
	for _, input := range []string{`[]`, `null`, `{} {}`, `{"n":NaN}`} {
		if _, err := decodeObject([]byte(input)); err == nil {
			t.Fatalf("accepted %s", input)
		}
	}
	if _, ok := integer(true); ok {
		t.Fatal("boolean must not be integer acknowledgment")
	}
}
func TestStatePreservesUnknownFields(t *testing.T) {
	privateHome(t)
	if err := writeState(statePath(), object{"future": object{"key": "retained"}}); err != nil {
		t.Fatal(err)
	}
	if err := editState(func(v object) error { v["host_instance_id"] = "existing"; return nil }); err != nil {
		t.Fatal(err)
	}
	state, err := loadState(statePath())
	if err != nil || obj(state["future"])["key"] != "retained" {
		t.Fatalf("%v %v", state, err)
	}
}
func TestFileLockAcrossProcesses(t *testing.T) {
	path := filepath.Join(t.TempDir(), "shared.lock")
	lock, err := acquireLock(path, time.Time{}, true)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.close()
	run := func(want string) {
		cmd := exec.Command(os.Args[0], "-test.run=^TestLockProcessHelper$")
		cmd.Env = append(os.Environ(), "PIG_TEST_LOCK="+path, "PIG_TEST_LOCK_WANT="+want)
		if data, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("child: %v %s", err, data)
		}
	}
	run("busy")
	lock.close()
	run("acquired")
}
func TestLockProcessHelper(t *testing.T) {
	path := os.Getenv("PIG_TEST_LOCK")
	if path == "" {
		return
	}
	lock, err := acquireLock(path, time.Time{}, false)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.close()
	if (lock != nil) != (os.Getenv("PIG_TEST_LOCK_WANT") == "acquired") {
		t.Fatal("lock exclusion failed")
	}
}
func TestSourceSnapshotsAndRewind(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "session.jsonl")
	mustWrite(t, path, []byte("one\ntwo\npartial"))
	ledger, err := loadLedger(filepath.Join(root, "ledger.json"))
	if err != nil {
		t.Fatal(err)
	}
	event, err := nextSourceEvent(ledger, path, time.Now().Add(time.Second))
	if err != nil || event.End != 8 || string(event.Content) != "one\ntwo\n" {
		t.Fatalf("event=%+v err=%v", event, err)
	}
	mustWrite(t, path, []byte("changed\n"))
	if err := ledger.record(*event, event.End); err != nil {
		t.Fatal(err)
	}
	if obj(ledger.Sources[event.PathHash])["prefix_sha256"] != hash([]byte("one\ntwo\n")) {
		t.Fatal("acknowledged live bytes instead of captured snapshot")
	}
	replay, err := nextSourceEvent(ledger, path, time.Now().Add(time.Second))
	if err != nil || replay.Start != 0 || string(replay.Content) != "changed\n" || len(ledger.Drift) != 1 {
		t.Fatalf("%+v %v", replay, err)
	}
}
func TestOversizedRecordAndTrailingPartial(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "session.jsonl")
	data := append(bytes.Repeat([]byte("x"), maxRecord+1), '\n')
	mustWrite(t, path, data)
	ledger, _ := loadLedger(filepath.Join(root, "ledger.json"))
	event, err := nextSourceEvent(ledger, path, time.Now().Add(5*time.Second))
	if err != nil || event.Kind != "oversized_record" || event.End != int64(len(data)) || event.Content != nil || event.ContentHash != hash(data) {
		t.Fatalf("%+v %v", event, err)
	}
	mustWrite(t, path, data[:len(data)-1])
	event, err = nextSourceEvent(ledger, path, time.Now().Add(5*time.Second))
	if err != nil || event != nil {
		t.Fatalf("partial record advanced: %+v %v", event, err)
	}
}
func TestAcknowledgmentIsExact(t *testing.T) {
	event := sourceEvent{Kind: "jsonl_range", PathHash: strings.Repeat("a", 64), Start: 0, End: 4, ContentHash: hash([]byte("one\n"))}
	policy := Policy{Version: 7}
	request := object{"batch_id": "native-test"}
	valid := object{"accepted": true, "batch_id": "native-test", "policy_version": 7, "raw_artifact_count": 1, "skipped_record_count": 0, "unparsed_record_count": 0, "acknowledged_ranges": []object{event.rangeProof()}}
	if err := validateAcknowledgment(valid, request, policy, event); err != nil {
		t.Fatal(err)
	}
	for key, bad := range (object{"accepted": false, "batch_id": "wrong", "policy_version": true, "raw_artifact_count": 0, "skipped_record_count": 1, "unparsed_record_count": -1, "acknowledged_ranges": []object{}}) {
		value := clone(valid)
		value[key] = bad
		if err := validateAcknowledgment(value, request, policy, event); err == nil {
			t.Errorf("accepted mismatched %s", key)
		}
	}
}
func TestRedirectDoesNotLeakCredential(t *testing.T) {
	t.Setenv("PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES", "1")
	hits := 0
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits++; io.WriteString(w, `{}`) }))
	defer sink.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, sink.URL, 302) }))
	defer redirect.Close()
	if _, err := requestJSON("GET", redirect.URL, &Credential{Value: "secret"}, nil); err == nil {
		t.Fatal("followed redirect")
	}
	if hits != 0 {
		t.Fatal("credential escaped origin")
	}
	if _, err := requestJSON("POST", sink.URL, &Credential{Value: "plicg_secret"}, nil); err == nil {
		t.Fatal("cloud grant used plaintext")
	}
}
func wire(n int, data []byte) []byte {
	b := binary.AppendUvarint(nil, uint64(n<<3|2))
	b = binary.AppendUvarint(b, uint64(len(data)))
	return append(b, data...)
}
func TestCursorWireAllowlist(t *testing.T) {
	result := append(wire(5, []byte("complete stdout")), wire(99, []byte("PRIVATE UNKNOWN FIELD"))...)
	data := append(wire(1, wire(2, wire(1, result))), wire(57, []byte("call-1"))...)
	id, output, quality, err := toolResult(data, 0)
	if err != nil || id != "call-1" || quality != "available" || output["stdout"] != "complete stdout" {
		t.Fatalf("%s %v %s %v", id, output, quality, err)
	}
	if strings.Contains(string(legacyJSON(output)), "PRIVATE") {
		t.Fatal("unknown field escaped allowlist")
	}
	for _, bad := range [][]byte{{0}, {0x80}, {0x0a, 0xff, 0x7f}} {
		if _, err := wireFields(bad); err == nil {
			t.Fatalf("accepted malformed wire: %x", bad)
		}
	}
}
func TestCursorWALSnapshotAndJournalRevisions(t *testing.T) {
	root := privateHome(t)
	path := filepath.Join(root, "state.vscdb")
	t.Setenv("PROMPTLESS_CURSOR_DATABASE", path)
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, query := range []string{"PRAGMA journal_mode=WAL", "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)"} {
		if _, err = db.Exec(query); err != nil {
			t.Fatal(err)
		}
	}
	put := func(key string, value object) {
		t.Helper()
		if _, err := db.Exec("INSERT OR REPLACE INTO cursorDiskKV VALUES (?,?)", key, legacyJSON(value)); err != nil {
			t.Fatal(err)
		}
	}
	put("composerData:session", object{"fullConversationHeadersOnly": []object{{"bubbleId": "a"}}})
	put("bubbleId:session:a", object{"type": 1, "text": "first"})
	page, err := readCursorSession("session", time.Now().Add(2*time.Second), 0, nil)
	if err != nil || !page.Complete || len(page.Records) != 1 {
		t.Fatalf("%+v %v", page, err)
	}
	journal, err := appendCursorObservations("session", page.Records)
	if err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(journal)
	if _, err = appendCursorObservations("session", page.Records); err != nil {
		t.Fatal(err)
	}
	same, _ := os.ReadFile(journal)
	if !bytes.Equal(before, same) {
		t.Fatal("duplicate observations appended")
	}
	put("bubbleId:session:a", object{"type": 1, "text": "second"})
	page, err = readCursorSession("session", time.Now().Add(2*time.Second), 0, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = appendCursorObservations("session", page.Records); err != nil {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(journal)
	if !bytes.HasPrefix(after, before) || bytes.Count(after, []byte{'\n'}) != 2 {
		t.Fatal("revision rewrote acknowledged journal")
	}
}
func TestCursorStdinBudget(t *testing.T) {
	r, w := io.Pipe()
	defer r.Close()
	defer w.Close()
	started := time.Now()
	data, err := readCursorInput(r)
	if err != nil || string(data) != "{}" || time.Since(started) > time.Second {
		t.Fatalf("%s %v", data, err)
	}
}
func TestCLIValidationAndFailOpen(t *testing.T) {
	privateHome(t)
	t.Setenv("PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES", "1")
	t.Setenv("PROMPTLESS_WORKER_BASE_URL", "http://127.0.0.1:1")
	for _, args := range [][]string{{}, {"reset", "--host", "codex"}, {"collect", "--host", "codex", "--det"}, {"session-start"}} {
		if code := Main(args, strings.NewReader("{}"), io.Discard, io.Discard); code != 2 {
			t.Fatalf("%v: exit %d", args, code)
		}
	}
	for _, name := range []string{"ensure", "collect"} {
		if code := Main([]string{name, "--host", "codex"}, strings.NewReader("{}"), io.Discard, io.Discard); code != 0 {
			t.Fatalf("%s blocked host: %d", name, code)
		}
	}
	if code := Main([]string{"session-start", "--host", "codex", "--supervised"}, strings.NewReader("{}"), io.Discard, io.Discard); code != 1 {
		t.Fatalf("supervisor exit %d", code)
	}
	data, err := loadState(stateSibling("last-bootstrap-status.json"))
	if err != nil || data["stage"] != "ensure" {
		t.Fatalf("%v %v", data, err)
	}
}

func TestConflictRequiresCapturedInteriorRecordProof(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "trace.jsonl")
	mustWrite(t, path, []byte("one\ntwo\n"))
	for _, scenario := range []string{"valid", "wrong_digest", "partial_record", "full_range", "wrong_source", "wrong_start", "changed_prefix", "missing_proof", "malformed_proof", "zero_watermark"} {
		t.Run(scenario, func(t *testing.T) {
			ledger, err := loadLedger(filepath.Join(root, scenario+".json"))
			if err != nil {
				t.Fatal(err)
			}
			event, err := nextSourceEvent(ledger, path, time.Now().Add(time.Second))
			if err != nil {
				t.Fatal(err)
			}
			proof := object{"start_offset": 0, "end_offset": 4, "content_sha256": hash([]byte("one\n"))}
			detail := object{"code": "trace_source_sequence_conflict", "source": "codex", "source_path_hash": event.PathHash,
				"requested_start_offset": 0, "requested_end_offset": 8, "acknowledged_offset": 4, "acknowledged_range": proof}
			switch scenario {
			case "missing_proof":
				delete(detail, "acknowledged_range")
			case "malformed_proof":
				detail["acknowledged_range"] = "not a range"
			case "zero_watermark":
				detail["acknowledged_offset"] = 0
				proof["end_offset"] = 0
				proof["content_sha256"] = hash(nil)
			case "wrong_digest":
				proof["content_sha256"] = hash([]byte("bad\n"))
			case "partial_record":
				detail["acknowledged_offset"] = 2
				proof["end_offset"] = 2
				proof["content_sha256"] = hash([]byte("on"))
			case "full_range":
				detail["acknowledged_offset"] = 8
				proof["end_offset"] = 8
				proof["content_sha256"] = event.ContentHash
			case "wrong_source":
				detail["source"] = "claude"
			case "wrong_start":
				detail["requested_start_offset"] = 1
			case "changed_prefix":
				ledger.Sources[event.PathHash] = object{"end_offset": 0, "prefix_sha256": "wrong"}
			}
			body, _ := json.Marshal(object{"detail": detail})
			recovered, err := reconcileConflict(ledger, Codex, *event, &HTTPError{Status: 409, Body: body})
			if scenario == "valid" {
				if !recovered || err != nil || number(obj(ledger.Sources[event.PathHash])["end_offset"]) != 4 {
					t.Fatalf("valid proof: %v %v", recovered, err)
				}
			} else if recovered || err == nil || number(obj(ledger.Sources[event.PathHash])["end_offset"]) != 0 {
				t.Fatalf("invalid proof advanced ledger: %v %v", recovered, err)
			}
		})
	}
}

func TestBrowserOverridePreservesQuotedArguments(t *testing.T) {
	// Reuse the test executable as a fake browser; a real browser is never launched.
	endpoint := "https://example.com/pig/enroll?state=opaque&next=one"
	output := filepath.Join(t.TempDir(), "browser-output")
	t.Setenv("PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER", "1")
	t.Setenv("PIG_TEST_BROWSER_OUTPUT", output)
	t.Setenv("BROWSER", "\""+filepath.ToSlash(os.Args[0])+"\" -test.run=^TestBrowserHelper$ -- \"argument with spaces\" %s")
	opened, err := openBrowser(endpoint)
	if err != nil || !opened {
		t.Fatalf("browser: %v %v", opened, err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(output)
		if err == nil {
			var args []string
			if err = json.Unmarshal(data, &args); err != nil {
				t.Fatal(err)
			}
			if len(args) != 2 || args[0] != "argument with spaces" || args[1] != endpoint {
				t.Fatalf("arguments: %v", args)
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("browser child did not receive arguments")
}
func TestBrowserHelper(t *testing.T) {
	path := os.Getenv("PIG_TEST_BROWSER_OUTPUT")
	if path == "" {
		return
	}
	for i, arg := range os.Args {
		if arg == "--" {
			data, _ := json.Marshal(os.Args[i+1:])
			mustWrite(t, path, data)
			return
		}
	}
	t.Fatal("missing argument separator")
}

func TestCloudEnrollmentKeepsProducerAcrossRetryAndRejectsIdentityChange(t *testing.T) {
	root := t.TempDir()
	main := filepath.Join(root, "session.jsonl")
	attempts := 0
	var producer string
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempts++
		state, err := loadState(filepath.Join(root, "producer.json"))
		if err != nil || str(state["producer_token"]) == "" {
			t.Error("producer must be durable before the first lease request", err)
			w.WriteHeader(500)
			return
		}
		if attempts == 1 {
			producer = str(state["producer_token"])
		}
		if state["producer_token"] != producer || r.Header.Get("X-Promptless-Producer-Token") != producer {
			t.Error("retry changed producer identity")
		}
		if attempts == 1 {
			w.WriteHeader(503)
			return
		}
		grant := "grant-original"
		if attempts == 3 {
			grant = "grant-replacement"
		}
		_ = json.NewEncoder(w).Encode(object{"execution_id": "session", "target": "codex", "provider": "test",
			"expires_at": time.Now().Add(time.Hour).Format(time.RFC3339), "id": "lease", "grant_id": grant,
			"host_instance_id": "host", "deployment_instance_id": "deployment"})
	}))
	defer server.Close()
	original := http.DefaultTransport
	http.DefaultTransport = server.Client().Transport
	t.Cleanup(func() { http.DefaultTransport = original })
	c := cloudConfig{Worker: server.URL, Provider: "test", Target: Codex, Transport: "proxy", Integration: "integration"}
	if _, _, err := enrollCloud(c, root, main, "session", ""); err == nil {
		t.Fatal("expected first request failure")
	}
	credential, _, err := enrollCloud(c, root, main, "session", "")
	if err != nil || credential.ProducerToken != producer {
		t.Fatal("retry did not reuse durable identity", err)
	}
	if _, _, err = enrollCloud(c, root, main, "session", ""); err == nil {
		t.Fatal("accepted changed grant identity")
	}
	state, err := loadState(filepath.Join(root, "producer.json"))
	if err != nil || state["grant_id"] != "grant-original" {
		t.Fatal("failed renewal changed durable identity", state, err)
	}
	if _, _, err = enrollCloud(c, root, main+"-changed", "session", ""); err == nil {
		t.Fatal("accepted changed transcript")
	}
	if attempts != 3 {
		t.Fatalf("changed transcript made a network request: %d", attempts)
	}
}
