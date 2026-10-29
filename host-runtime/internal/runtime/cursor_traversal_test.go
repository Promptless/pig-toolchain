package runtime

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestCursorChildPagesFinishBeforeChildTerminalEvent(t *testing.T) {
	f := newCursorFixture(t)
	f.session("child", 129)
	context := HookContext{SessionID: "parent", AgentID: "child", GenerationID: "g"}
	if _, complete := exportCursor(t, context, "subagent_stop"); complete || bytes.Contains(journalBytes(t, "child"), []byte("subagent_stop")) {
		t.Fatal("child ended before final page")
	}
	if _, complete := exportCursor(t, context, "subagent_stop"); !complete {
		t.Fatal("child did not finish")
	}
	rows := journalRows(t, "child")
	for _, row := range rows {
		if row["session_id"] != "child" {
			t.Fatal("child hook closed parent", row)
		}
	}
	last := rows[len(rows)-1]
	if last["parent_session_id"] != "parent" || obj(last["event"])["name"] != "subagent_stop" {
		t.Fatal(last)
	}
	if exists(filepath.Join(cursorSpool(), "journals", "parent.jsonl")) {
		t.Fatal("parent journal created by child hook")
	}
}
func TestCursorSubagentTraversalResumesAcrossBoundedPasses(t *testing.T) {
	for _, nested := range []bool{false, true} {
		t.Run(fmt.Sprint(nested), func(t *testing.T) {
			f := newCursorFixture(t)
			children := []string{}
			sessions := map[string]bool{"parent": true}
			count := 32
			if nested {
				count = 16
			}
			for i := range count {
				id := fmt.Sprintf("child-%d", i)
				children = append(children, id)
				sessions[id] = true
				descendants := []string{}
				if nested {
					descendants = []string{id + "-a", id + "-b"}
					for _, child := range descendants {
						f.session(child, 1)
						sessions[child] = true
					}
				}
				f.session(id, 1, descendants...)
			}
			f.session("parent", 1, children...)
			context := HookContext{SessionID: "parent"}
			if _, complete := exportCursor(t, context, "session_end"); complete {
				t.Fatal("unbounded first traversal")
			}
			f.session("parent", 1, append(children, "new-child")...)
			f.session("new-child", 129)
			sessions["new-child"] = true
			complete := false
			for range 6 {
				_, complete = exportCursor(t, context, "session_end")
				if complete {
					break
				}
			}
			if !complete {
				t.Fatal("bounded traversal did not resume")
			}
			paths, _ := filepath.Glob(filepath.Join(cursorSpool(), "journals", "*.jsonl"))
			if len(paths) != len(sessions) {
				t.Fatalf("missing journals: %d / %d", len(paths), len(sessions))
			}
			for id := range sessions {
				if !exists(filepath.Join(cursorSpool(), "journals", id+".jsonl")) {
					t.Fatal("missing", id)
				}
			}
			if rows := journalRows(t, "new-child"); len(rows) != 129 {
				t.Fatal("new child pages lost or duplicated", len(rows))
			}
		})
	}
}
func TestCursorCompletedChildrenRefreshAfterDatabaseChanges(t *testing.T) {
	for _, checkpoint := range []bool{false, true} {
		for _, change := range []string{"message", "grandchild"} {
			t.Run(fmt.Sprintf("%s/checkpoint=%v", change, checkpoint), func(t *testing.T) {
				f := newCursorFixture(t)
				children := []string{}
				for i := range 32 {
					id := fmt.Sprintf("child-%d", i)
					children = append(children, id)
					f.session(id, 1)
					f.put("bubbleId:"+id+":0", object{"type": 1, "text": "old"})
				}
				f.session("parent", 0, children...)
				context := HookContext{SessionID: "parent"}
				if _, complete := exportCursor(t, context, "session_end"); complete {
					t.Fatal("expected pending child")
				}
				if change == "message" {
					f.put("bubbleId:child-0:0", object{"type": 1, "text": "new"})
				} else {
					f.session("child-0", 1, "grandchild")
					f.session("grandchild", 1)
					f.put("bubbleId:grandchild:0", object{"type": 1, "text": "new descendant"})
				}
				if checkpoint {
					if _, err := f.db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
						t.Fatal(err)
					}
				}
				complete := false
				for range 5 {
					_, complete = exportCursor(t, context, "session_end")
					if complete {
						break
					}
				}
				if !complete {
					t.Fatal("changed traversal did not finish")
				}
				if change == "message" {
					rows := journalRows(t, "child-0")
					if len(rows) != 2 || obj(rows[0]["event"])["text"] != "old" || obj(rows[1]["event"])["text"] != "new" {
						t.Fatal("same-size update missed", rows)
					}
				} else if !bytes.Contains(journalBytes(t, "grandchild"), []byte("new descendant")) {
					t.Fatal("new descendant missed")
				}
			})
		}
	}
}
func TestCursorUnsavedAndUnrelatedSessionsOwnTheirCompletion(t *testing.T) {
	f := newCursorFixture(t)
	unsaved := HookContext{SessionID: "unsaved"}
	saved := HookContext{SessionID: "saved"}
	if _, complete := exportCursor(t, unsaved, "session_end"); complete {
		t.Fatal("unsaved marked complete")
	}
	paths, _ := filepath.Glob(filepath.Join(cursorSpool(), "journals", "*.jsonl"))
	if len(paths) != 0 {
		t.Fatal("unsaved emitted terminal journal")
	}
	f.session("saved", 1)
	for range 3 {
		if _, complete := exportCursor(t, saved, "session_end"); !complete {
			t.Fatal("unrelated pending session blocked notification")
		}
	}
	if _, complete := exportCursor(t, unsaved, "session_end"); complete {
		t.Fatal("unrelated notification completed unsaved session")
	}
	f.session("unsaved", 1)
	f.put("bubbleId:unsaved:0", object{"type": 1, "text": "recovered message"})
	if _, complete := exportCursor(t, unsaved, "session_end"); !complete || !bytes.Contains(journalBytes(t, "unsaved"), []byte("recovered message")) {
		t.Fatal("unsaved session did not recover")
	}
}
func TestCursorPendingSubagentDoesNotPreventRefresh(t *testing.T) {
	for _, discovered := range []bool{false, true} {
		t.Run(fmt.Sprint(discovered), func(t *testing.T) {
			f := newCursorFixture(t)
			children := []string{"missing"}
			if discovered {
				f.discovered("idle")
			} else {
				children = append(children, "idle")
			}
			f.session("parent", 0, children...)
			f.session("idle", 1)
			for _, text := range []string{"old", "new"} {
				f.put("bubbleId:idle:0", object{"type": 1, "text": text})
				if _, complete := exportCursor(t, HookContext{SessionID: "parent"}, "session_end"); complete {
					t.Fatal("missing child marked complete")
				}
			}
			rows := journalRows(t, "idle")
			if len(rows) != 2 || obj(rows[0]["event"])["text"] != "old" || obj(rows[1]["event"])["text"] != "new" {
				t.Fatal("pending child starved refresh", rows)
			}
		})
	}
}
func TestCursorBackgroundDiscoveryCannotBlockSubject(t *testing.T) {
	f := newCursorFixture(t)
	for i := range 41 {
		id := fmt.Sprintf("idle-%02d", i)
		f.discovered(id)
		if i != 0 {
			f.session(id, 0)
		}
	}
	f.session("subject", 0)
	context := HookContext{SessionID: "subject"}
	for range 4 {
		if _, complete := exportCursor(t, context, "session_end"); !complete {
			t.Fatal("background discovery blocked subject")
		}
	}
	f.session("idle-00", 1)
	f.put("bubbleId:idle-00:0", object{"type": 1, "text": "background recovered"})
	if _, complete := exportCursor(t, context, "session_end"); !complete || !bytes.Contains(journalBytes(t, "idle-00"), []byte("background recovered")) {
		t.Fatal("background retry was lost")
	}
}
func TestCursorFailedChildJournalPreservesParentPageProgress(t *testing.T) {
	f := newCursorFixture(t)
	f.session("parent", 257, "child")
	f.session("child", 1)
	childPath := filepath.Join(cursorSpool(), "journals", "child.jsonl")
	sparseFile(t, childPath, cursorJournalLimit+1)
	context := HookContext{SessionID: "parent"}
	for range 3 {
		if _, complete := exportCursor(t, context, "session_end"); complete {
			t.Fatal("failed child marked complete")
		}
	}
	count := 0
	for _, row := range journalRows(t, "parent") {
		if obj(row["event"])["kind"] == "user_message" {
			count++
		}
	}
	if count != 257 {
		t.Fatalf("parent lost progress: %d", count)
	}
	diagnostics, _ := loadState(filepath.Join(cursorSpool(), "diagnostics.json"))
	if number(obj(diagnostics["errors"])["journal_write"]) != 1 {
		t.Fatal(diagnostics)
	}
	if err := os.Remove(childPath); err != nil {
		t.Fatal(err)
	}
	complete := false
	for range 4 {
		_, complete = exportCursor(t, context, "session_end")
		if complete {
			break
		}
	}
	if !complete || len(journalRows(t, "child")) != 1 {
		t.Fatal("child journal did not recover")
	}
}
func TestCursorDiscoveredAndOverlappingChildTraversalsAdvanceIndependently(t *testing.T) {
	for _, discovered := range []bool{false, true} {
		t.Run(fmt.Sprint(discovered), func(t *testing.T) {
			f := newCursorFixture(t)
			f.session("parent", 0, "child")
			f.session("child", 385)
			if discovered {
				f.discovered("child")
			}
			parent := HookContext{SessionID: "parent"}
			child := HookContext{SessionID: "child"}
			complete := false
			for range 7 {
				_, complete = exportCursor(t, parent, "session_end")
				if !discovered {
					exportCursor(t, child, "session_end")
				}
				if complete {
					break
				}
			}
			if !complete {
				t.Fatal("overlapping owners stalled")
			}
			count := 0
			for _, row := range journalRows(t, "child") {
				if obj(row["event"])["kind"] == "user_message" {
					count++
				}
			}
			if count != 385 {
				t.Fatalf("lost or duplicated child messages: %d", count)
			}
		})
	}
}
