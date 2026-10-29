package runtime

import (
	"bytes"
	"database/sql"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

type cursorFixture struct {
	t    *testing.T
	db   *sql.DB
	home string
}

func newCursorFixture(t *testing.T) cursorFixture {
	t.Helper()
	home := privateHome(t)
	path := filepath.Join(home, "state.vscdb")
	t.Setenv("PROMPTLESS_CURSOR_DATABASE", path)
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(1)
	t.Cleanup(func() { _ = db.Close() })
	for _, query := range []string{"PRAGMA journal_mode=WAL", "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)"} {
		if _, err = db.Exec(query); err != nil {
			t.Fatal(err)
		}
	}
	return cursorFixture{t, db, home}
}
func (f cursorFixture) put(key string, value any) {
	f.t.Helper()
	data, ok := value.([]byte)
	if !ok {
		data = legacyJSON(value)
	}
	if _, err := f.db.Exec("INSERT OR REPLACE INTO cursorDiskKV VALUES (?,?)", key, data); err != nil {
		f.t.Fatal(err)
	}
}
func (f cursorFixture) session(id string, count int, children ...string) {
	f.t.Helper()
	headers := []object{}
	for i := range count {
		key := fmt.Sprint(i)
		headers = append(headers, object{"bubbleId": key})
		f.put("bubbleId:"+id+":"+key, object{"type": 1, "text": key})
	}
	f.put("composerData:"+id, object{"fullConversationHeadersOnly": headers, "subagentComposerIds": children})
}
func (f cursorFixture) discovered(id string) {
	f.t.Helper()
	root := filepath.Join(f.home, ".cursor", "projects", "project", "agent-transcripts")
	if err := os.MkdirAll(root, 0700); err != nil {
		f.t.Fatal(err)
	}
	mustWrite(f.t, filepath.Join(root, id+".jsonl"), nil)
}
func (f cursorFixture) read(id string) cursorPage {
	f.t.Helper()
	page, err := readCursorSession(id, time.Now().Add(2*time.Second), 0, nil)
	if err != nil {
		f.t.Fatal(err)
	}
	return page
}
func exportCursor(t *testing.T, context HookContext, lifecycle string) (HookContext, bool) {
	t.Helper()
	result, complete, err := prepareCursorJournals(context, lifecycle)
	if err != nil {
		t.Fatal(err)
	}
	return result, complete
}
func journalBytes(t *testing.T, id string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(cursorSpool(), "journals", id+".jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	return b
}
func journalRows(t *testing.T, id string) []object {
	t.Helper()
	rows := []object{}
	for _, line := range bytes.Split(bytes.TrimSpace(journalBytes(t, id)), []byte("\n")) {
		row, err := decodeObject(line)
		if err != nil {
			t.Fatal(err)
		}
		rows = append(rows, row)
	}
	return rows
}
func wireScalar(n int, value uint64) []byte {
	return binary.AppendUvarint(binary.AppendUvarint(nil, uint64(n<<3)), value)
}
func savedCall(kind int, saved []byte) []byte {
	return append(wire(kind, wire(2, wire(1, saved))), wire(57, []byte("call-1"))...)
}
func b64(data []byte) string { return base64.StdEncoding.EncodeToString(data) }
func (f cursorFixture) canonical(saved []byte, ui any) {
	f.put("composerData:session", object{"fullConversationHeadersOnly": []object{{"bubbleId": "tool"}}, "conversationState": b64(wire(8, []byte("turn"))), "modelConfig": object{"modelName": "model"}, "encryptionKey": "SECRET"})
	f.put("bubbleId:session:tool", object{"toolFormerData": object{"name": "shell", "toolCallId": "call-1", "params": `{"command":"echo saved"}`, "toolCallBinary": ui}, "additionalData": "SECRET"})
	f.put("agentKv:blob:"+hex.EncodeToString([]byte("turn")), wire(1, wire(2, []byte("step"))))
	f.put("agentKv:blob:"+hex.EncodeToString([]byte("step")), wire(2, saved))
}
func TestCursorSavedResultFamilies(t *testing.T) {
	for _, tc := range []struct {
		name string
		kind int
		data []byte
		want object
	}{
		{"shell", 1, wire(5, []byte("saved stdout")), object{"stdout": "saved stdout"}},
		{"empty_success", 1, nil, object{}},
		{"file", 8, append(wire(1, []byte("file at execution time")), wire(7, []byte("/never-read"))...), object{"content": "file at execution time", "path": "/never-read"}},
		{"legacy_file", 8, append(wire(1, []byte("/legacy")), wire(2, []byte("legacy saved"))...), object{"path": "/legacy", "content": "legacy saved"}},
		{"edit", 12, append(wire(6, []byte("before")), wire(7, []byte("after"))...), object{"before": "before", "after": "after"}},
		{"mcp", 15, wire(1, wire(1, wire(1, []byte("MCP text")))), object{"content": []any{object{"text": object{"text": "MCP text"}}}}},
		{"subagent", 19, append(wire(1, wire(1, wire(1, []byte("child response")))), wire(2, []byte("child"))...), object{"steps": []any{object{"assistant": object{"text": "child response"}}}, "agent_id": "child"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			id, result, quality, err := toolResult(savedCall(tc.kind, tc.data), 0)
			tc.want["status"] = "success"
			if err != nil || id != "call-1" || (quality != "available" && tc.kind != 19) || !reflect.DeepEqual(result, tc.want) {
				t.Fatal(id, result, quality, err)
			}
		})
	}
	for _, tc := range []struct {
		data    []byte
		quality string
	}{{append(wire(1, nil), wire(57, []byte("call-1"))...), "missing"}, {wire(100, nil), "unsupported"}} {
		_, _, quality, err := toolResult(tc.data, 0)
		if err != nil || quality != tc.quality {
			t.Fatal(quality, err)
		}
	}
}
func TestCursorCanonicalGraphIsReadOnlyAndRecoversIncompleteUIResults(t *testing.T) {
	variants := map[string]any{"absent": nil, "missing": b64(append(wire(1, nil), wire(57, []byte("call-1"))...)), "pruned": b64(savedCall(1, append(wire(5, []byte("truncated")), wireScalar(17, 100)...))), "unsupported": b64(append(wire(100, nil), wire(57, []byte("call-1"))...)), "malformed": b64([]byte{0x0a, 0xff})}
	for name, ui := range variants {
		t.Run(name, func(t *testing.T) {
			f := newCursorFixture(t)
			f.canonical(savedCall(1, wire(5, []byte("saved stdout"))), ui)
			if _, err := f.db.Exec("BEGIN IMMEDIATE"); err != nil {
				t.Fatal(err)
			}
			if _, err := f.db.Exec("INSERT INTO cursorDiskKV VALUES ('uncommitted','value')"); err != nil {
				t.Fatal(err)
			}
			page := f.read("session")
			if !page.Complete || obj(obj(page.Records[len(page.Records)-1]["event"])["output"])["stdout"] != "saved stdout" || bytes.Contains(legacyJSON(page.Records), []byte("SECRET")) {
				t.Fatal(page)
			}
			if _, err := f.db.Exec("COMMIT"); err != nil {
				t.Fatal("collector delayed editor", err)
			}
			var count int
			if err := f.db.QueryRow("SELECT count(*) FROM cursorDiskKV").Scan(&count); err != nil || count != 5 {
				t.Fatal(count, err)
			}
		})
	}
}
func TestCursorTransientReferencesRetryBeforeTerminalEvent(t *testing.T) {
	for _, kind := range []string{"content", "canonical", "canonical-content"} {
		for _, failure := range []string{"cursor_snapshot_budget", "cursor_snapshot_size_limit", "database is locked (SQLITE_BUSY)"} {
			t.Run(kind+"/"+failure, func(t *testing.T) {
				f := newCursorFixture(t)
				var ui any
				if kind == "content" {
					ui = b64(savedCall(8, wire(10, []byte("content"))))
				}
				f.canonical(savedCall(8, wire(10, []byte("content"))), ui)
				f.put("composerData:session", object{"fullConversationHeadersOnly": []object{{"bubbleId": "first"}, {"bubbleId": "tool"}}, "conversationState": b64(wire(8, []byte("turn")))})
				f.put("bubbleId:session:first", object{"type": 1, "text": "keep this message"})
				f.put("agentKv:blob:"+hex.EncodeToString([]byte("content")), []byte("retained content"))
				failedRef := "content"
				if kind == "canonical" {
					failedRef = "turn"
				}
				reader := func(id string, deadline time.Time, offset int, retry *int) (cursorPage, error) {
					return readCursorSessionWithReferences(id, deadline, offset, retry, func(s *cursorSnapshot, key string) ([]byte, error) {
						if key == "agentKv:blob:"+hex.EncodeToString([]byte(failedRef)) {
							return nil, errors.New(failure)
						}
						return s.read(key)
					})
				}
				context := HookContext{SessionID: "session"}
				exported, complete, err := prepareCursorJournalsWithReader(context, "session_end", reader)
				if err != nil || complete || exported.Transcript == "" {
					t.Fatal(exported, complete, err)
				}
				before := journalBytes(t, "session")
				if !bytes.Contains(before, []byte("keep this message")) || bytes.Contains(before, []byte("session_end")) {
					t.Fatal("terminal event preceded retry")
				}
				state, _ := loadState(filepath.Join(cursorSpool(), "scan-state.json"))
				if number(obj(obj(obj(state["traversals"])["session"])["offsets"])["session"]) != 1 {
					t.Fatal(state)
				}
				_, complete = exportCursor(t, context, "session_end")
				after := journalBytes(t, "session")
				if !complete || !bytes.HasPrefix(after, before) || !bytes.Contains(after, []byte("retained content")) || !bytes.Contains(after, []byte("session_end")) {
					t.Fatal("retry did not recover append-only journal")
				}
			})
		}
	}
}
func TestCursorUIBlobQualityAndNoRedundantCanonicalRead(t *testing.T) {
	for _, retained := range []bool{false, true} {
		t.Run(fmt.Sprint(retained), func(t *testing.T) {
			f := newCursorFixture(t)
			f.canonical(savedCall(1, wire(5, []byte("canonical"))), b64(savedCall(8, wire(10, []byte("content")))))
			// No canonical fallback for the pruned variant; it must stay explicitly pruned.
			f.put("composerData:session", object{"fullConversationHeadersOnly": []object{{"bubbleId": "tool"}}, "conversationState": b64(wire(8, []byte("absent")))})
			if retained {
				f.put("agentKv:blob:"+hex.EncodeToString([]byte("content")), []byte("retained content"))
			}
			canonicalReads := 0
			page, err := readCursorSessionWithReferences("session", time.Now().Add(time.Second), 0, nil, func(s *cursorSnapshot, key string) ([]byte, error) {
				if key != "agentKv:blob:"+hex.EncodeToString([]byte("content")) {
					canonicalReads++
				}
				return s.read(key)
			})
			if err != nil || !page.Complete {
				t.Fatal(page, err)
			}
			last := page.Records[len(page.Records)-1]
			want := "pruned"
			if retained {
				want = "available"
				if canonicalReads != 0 || obj(obj(last["event"])["output"])["content"] != "retained content" {
					t.Fatal("redundant read or missing content")
				}
			}
			if obj(last["capture"])["completeness"] != want {
				t.Fatal(last)
			}
		})
	}
}
func TestCursorSkipsRollbackDatabaseAndDoesNotWaitForExclusiveWriter(t *testing.T) {
	f := newCursorFixture(t)
	if _, err := f.db.Exec("PRAGMA journal_mode=DELETE"); err != nil {
		t.Fatal(err)
	}
	if _, err := readCursorSession("session", time.Now().Add(time.Second), 0, nil); err == nil || err.Error() != "cursor_database_requires_wal" {
		t.Fatal(err)
	}
	if _, err := f.db.Exec("BEGIN EXCLUSIVE"); err != nil {
		t.Fatal(err)
	}
	defer f.db.Exec("ROLLBACK")
	started := time.Now()
	if _, err := readCursorSession("session", started.Add(500*time.Millisecond), 0, nil); err == nil {
		t.Fatal("exclusive database read succeeded")
	}
	if time.Since(started) >= 500*time.Millisecond {
		t.Fatal("collector waited for editor")
	}
}
func TestCursorMissingBubblesAcrossPagesDeferTerminalAndRecover(t *testing.T) {
	for _, missing := range [][]int{{0}, {128}, {0, 128}} {
		t.Run(fmt.Sprint(missing), func(t *testing.T) {
			f := newCursorFixture(t)
			f.session("session", 257)
			for _, i := range missing {
				if _, err := f.db.Exec("DELETE FROM cursorDiskKV WHERE key=?", fmt.Sprintf("bubbleId:session:%d", i)); err != nil {
					t.Fatal(err)
				}
			}
			context := HookContext{SessionID: "session"}
			for range 4 {
				if _, complete := exportCursor(t, context, "session_end"); complete {
					t.Fatal("missing bubble marked complete")
				}
			}
			before := journalBytes(t, "session")
			if bytes.Contains(before, []byte("session_end")) {
				t.Fatal("premature terminal event")
			}
			messages := 0
			for _, row := range journalRows(t, "session") {
				if obj(row["event"])["kind"] == "user_message" {
					messages++
				}
			}
			if messages != 257-len(missing) {
				t.Fatalf("later pages stalled: %d", messages)
			}
			headers := []object{}
			for i := range 258 {
				headers = append(headers, object{"bubbleId": fmt.Sprint(i)})
			}
			f.put("composerData:session", object{"fullConversationHeadersOnly": headers})
			f.put("bubbleId:session:257", object{"type": 1, "text": "appended"})
			for range 4 {
				if _, complete := exportCursor(t, context, "session_end"); complete {
					t.Fatal("new message hid missing bubble")
				}
			}
			before = journalBytes(t, "session")
			if !bytes.Contains(before, []byte("appended")) {
				t.Fatal("new page not visited")
			}
			for _, i := range missing {
				f.put(fmt.Sprintf("bubbleId:session:%d", i), object{"type": 1, "text": fmt.Sprint(i)})
			}
			complete := false
			for range 6 {
				_, complete = exportCursor(t, context, "session_end")
				if complete {
					break
				}
			}
			after := journalBytes(t, "session")
			rows := journalRows(t, "session")
			if !complete || !bytes.HasPrefix(after, before) || obj(rows[len(rows)-1]["event"])["name"] != "session_end" {
				t.Fatal("missing bubble did not recover")
			}
			messages = 0
			for _, row := range rows {
				if obj(row["event"])["kind"] == "user_message" {
					messages++
				}
			}
			if messages != 258 {
				t.Fatalf("lost or duplicated messages: %d", messages)
			}
		})
	}
}
func TestCursorJournalRepairIdentityAndQuality(t *testing.T) {
	privateHome(t)
	first := object{"event_id": "call:result", "event": object{"kind": "tool_result", "tool": "shell", "call_id": "call", "output": nil}, "capture": object{"source": "database", "completeness": "missing"}}
	path, err := appendCursorObservations("session", []object{first})
	if err != nil {
		t.Fatal(err)
	}
	before := journalBytes(t, "session")
	if _, err = appendCursorObservations("session", []object{first}); err != nil || !bytes.Equal(before, journalBytes(t, "session")) {
		t.Fatal("duplicate observation appended", err)
	}
	file, _ := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	_, err = file.WriteString(`{"torn`)
	_ = file.Close()
	if err != nil {
		t.Fatal(err)
	}
	second := object{"event_id": "call:result", "event": object{"kind": "tool_result", "tool": "shell", "call_id": "call", "output": "saved"}, "capture": object{"source": "database", "completeness": "available"}}
	if _, err = appendCursorObservations("session", []object{second}); err != nil {
		t.Fatal(err)
	}
	rows := journalRows(t, "session")
	if !bytes.HasPrefix(journalBytes(t, "session"), before) || len(rows) != 2 || number(rows[1]["revision"]) != 2 {
		t.Fatal("torn write rewrote acknowledged prefix")
	}
	for _, quality := range []string{"missing", "pruned", "partial", "unsupported", "size_limit"} {
		worse := clone(first)
		worse["capture"] = object{"source": "database", "completeness": quality}
		if _, err = appendCursorObservations("session", []object{worse}); err != nil {
			t.Fatal(err)
		}
	}
	if len(journalRows(t, "session")) != 2 {
		t.Fatal("available result downgraded")
	}
	if _, err = appendCursorObservations("../escape", []object{first}); err == nil {
		t.Fatal("unsafe session accepted")
	}
}
func TestCursorFallbackAliasesNativeIdentity(t *testing.T) {
	privateHome(t)
	message := object{"kind": "user_message", "text": "same prompt"}
	fallback := object{"event_id": "transcript:0:0", "event": message, "capture": object{"source": "transcript", "completeness": "partial"}}
	if _, err := appendCursorObservations("session", []object{fallback}); err != nil {
		t.Fatal(err)
	}
	native := object{"event_id": "bubble:text", "event": message, "capture": object{"source": "database", "completeness": "available"}}
	if _, err := appendCursorObservations("session", []object{native, native, fallback}); err != nil {
		t.Fatal(err)
	}
	rows := journalRows(t, "session")
	if len(rows) != 2 || rows[0]["event_id"] != "transcript:0:0" || rows[1]["event_id"] != rows[0]["event_id"] || rows[1]["native_event_id"] != "bubble:text" || number(rows[1]["revision"]) != 2 {
		t.Fatal(rows)
	}
}
func TestCursorOversizedFallbackPreservesOriginAndDeduplicates(t *testing.T) {
	privateHome(t)
	observation := object{"event_id": "transcript:0:0", "event": object{"kind": "session_event", "name": "cursor_transcript_tool_call", "data": object{"input": strings.Repeat("x", cursorRecordLimit+1)}}, "capture": object{"source": "transcript", "completeness": "partial"}}
	for range 2 {
		if _, err := appendCursorObservations("session", []object{observation}); err != nil {
			t.Fatal(err)
		}
	}
	rows := journalRows(t, "session")
	if len(rows) != 1 || len(journalBytes(t, "session")) > cursorRecordLimit || len(obj(obj(rows[0]["event"])["data"])) != 0 || obj(rows[0]["capture"])["source"] != "transcript" || obj(rows[0]["capture"])["completeness"] != "size_limit" {
		t.Fatal("oversized record was not bounded and deduplicated")
	}
}
func sparseFile(t *testing.T, path string, size int64) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err = f.Truncate(size); err != nil {
		t.Fatal(err)
	}
}
func TestCursorSpoolLimitPreservesAcknowledgedJournal(t *testing.T) {
	privateHome(t)
	first := object{"event_id": "a", "event": object{"kind": "user_message", "text": "saved"}}
	if _, err := appendCursorObservations("session", []object{first}); err != nil {
		t.Fatal(err)
	}
	before := journalBytes(t, "session")
	sparseFile(t, filepath.Join(cursorSpool(), "journals", "full.jsonl"), cursorSpoolLimit)
	second := clone(first)
	second["event_id"] = "b"
	if _, err := appendCursorObservations("session", []object{second}); err == nil || err.Error() != "cursor_spool_size_limit" {
		t.Fatal(err)
	}
	if !bytes.Equal(before, journalBytes(t, "session")) {
		t.Fatal("spool limit damaged existing journal")
	}
}
func TestCursorPrunedUIFallbackCannotReplaceCompleteResult(t *testing.T) {
	f := newCursorFixture(t)
	f.session("session", 0)
	f.put("composerData:session", object{"fullConversationHeadersOnly": []object{{"bubbleId": "tool"}}})
	tool := object{"name": "shell", "toolCallId": "call-1", "status": "completed", "toolCallBinary": b64(savedCall(1, wire(5, []byte("complete stdout"))))}
	f.put("bubbleId:session:tool", object{"toolFormerData": tool})
	page := f.read("session")
	if _, err := appendCursorObservations("session", page.Records); err != nil {
		t.Fatal(err)
	}
	before := journalBytes(t, "session")
	delete(tool, "toolCallBinary")
	tool["result"] = "truncated UI result"
	f.put("bubbleId:session:tool", object{"toolFormerData": tool})
	page = f.read("session")
	if obj(page.Records[len(page.Records)-1]["capture"])["completeness"] != "partial" {
		t.Fatal(page)
	}
	if _, err := appendCursorObservations("session", page.Records); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, journalBytes(t, "session")) {
		t.Fatal("UI fallback replaced complete result")
	}
}
