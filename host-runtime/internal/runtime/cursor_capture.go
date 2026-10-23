package runtime

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

const cursorRecordLimit = 2 << 20
const cursorJournalLimit = 128 << 20
const cursorSpoolLimit = 1 << 30
const cursorJournalCount = 4096

var cursorSessionPattern = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,200}$`)

func cursorSpool() string { return filepath.Join(filepath.Dir(ledgerPath()), "cursor") }
func compactJSON(v any) []byte {
	var b bytes.Buffer
	_ = json.Compact(&b, legacyJSON(v))
	return b.Bytes()
}
func compactDigest(v any) string { return hash(compactJSON(v)) }
func cursorRevision() string {
	path := resolvePath(cursorDBPath())
	parts := []any{path}
	for _, name := range []string{path, path + "-wal"} {
		s, err := os.Stat(name)
		if errors.Is(err, os.ErrNotExist) {
			parts = append(parts, nil)
		} else if err != nil {
			return ""
		} else {
			parts = append(parts, []any{s.Size(), s.ModTime().UnixNano(), fmt.Sprint(s.Sys())})
		}
	}
	return compactDigest(parts)
}
func cursorFallback(path string) ([]object, error) {
	records := []object{}
	if path == "" || !isFile(path) {
		return records, nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	reader := bufio.NewReaderSize(f, cursorRecordLimit+1)
	for index := 0; index < 4000; index++ {
		line, err := reader.ReadSlice('\n')
		if err != nil {
			if errors.Is(err, io.EOF) || errors.Is(err, bufio.ErrBufferFull) {
				break
			}
			return nil, err
		}
		if len(line) > cursorRecordLimit {
			break
		}
		row, err := decodeObject(line)
		if err != nil {
			return nil, err
		}
		content, _ := obj(row["message"])["content"].([]any)
		for i, b := range content {
			block := obj(b)
			var event object
			if block["type"] == "text" {
				if text, ok := block["text"].(string); ok {
					kind := "assistant_message"
					if row["role"] == "user" {
						kind = "user_message"
					}
					event = object{"kind": kind, "text": text}
				}
			} else if block["type"] == "tool_use" {
				event = object{"kind": "session_event", "name": "cursor_transcript_tool_call", "data": object{"tool": block["name"], "input": block["input"]}}
			}
			if event != nil {
				records = append(records, object{"event_id": fmt.Sprintf("transcript:%d:%d", index, i), "event": event, "capture": object{"source": "transcript", "completeness": "partial"}})
			}
		}
	}
	return records, nil
}
func cursorSemantic(event any) string {
	v := clone(obj(event))
	delete(v, "call_id")
	delete(v, "t")
	return compactDigest(v)
}

// The append-only journal is authoritative. Scan progress is committed only
// after this file is synced, making crash replay safe and preserving revisions.
func appendCursorObservations(id string, observations []object) (string, error) {
	if !cursorSessionPattern.MatchString(id) {
		return "", errors.New("cursor_invalid_session_id")
	}
	path := filepath.Join(cursorSpool(), "journals", id+".jsonl")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return "", err
	}
	journals, err := filepath.Glob(filepath.Join(filepath.Dir(path), "*.jsonl"))
	if err != nil {
		return "", err
	}
	if len(journals) >= cursorJournalCount && !exists(path) {
		return "", errors.New("cursor_journal_count_limit")
	}
	var spoolBytes int64
	for _, p := range journals {
		s, err := os.Stat(p)
		if err != nil {
			return "", err
		}
		spoolBytes += s.Size()
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return "", err
	}
	defer f.Close()
	if err = f.Chmod(0600); err != nil {
		return "", err
	}
	stat, err := f.Stat()
	if err != nil {
		return "", err
	}
	if stat.Size() > cursorJournalLimit {
		return "", errors.New("cursor_journal_size_limit")
	}
	latest := map[string]object{}
	aliases := map[string]string{}
	order := []string{}
	reader := bufio.NewReaderSize(f, cursorRecordLimit+1)
	var position int64
	for {
		line, readErr := reader.ReadSlice('\n')
		if errors.Is(readErr, bufio.ErrBufferFull) {
			return "", errors.New("cursor_record_metadata_size_limit")
		}
		if errors.Is(readErr, io.EOF) {
			if len(line) > 0 {
				if err = f.Truncate(position); err != nil {
					return "", err
				}
			}
			break
		}
		if readErr != nil {
			return "", readErr
		}
		position += int64(len(line))
		row, err := decodeObject(line)
		if err != nil {
			return "", err
		}
		identity := str(row["event_id"])
		if identity != "" {
			if latest[identity] == nil {
				order = append(order, identity)
			}
			latest[identity] = row
			if native := str(row["native_event_id"]); native != "" {
				aliases[native] = identity
			}
		}
	}
	fallback := map[string][]string{}
	hasDB := false
	for _, identity := range order {
		row := latest[identity]
		source := str(obj(row["capture"])["source"])
		if source == "transcript" {
			key := cursorSemantic(row["event"])
			fallback[key] = append(fallback[key], identity)
		}
		hasDB = hasDB || source == "database"
	}
	if _, err = f.Seek(position, io.SeekStart); err != nil {
		return "", err
	}
	for _, observation := range observations {
		capture := obj(observation["capture"])
		if capture["source"] == "transcript" && hasDB {
			continue
		}
		native := str(observation["event_id"])
		if native == "" {
			continue
		}
		identity := aliases[native]
		if identity == "" {
			identity = native
		}
		if latest[identity] == nil && capture["source"] == "database" {
			key := cursorSemantic(observation["event"])
			if matches := fallback[key]; len(matches) > 0 {
				identity = matches[0]
				fallback[key] = matches[1:]
			}
		}
		previous := latest[identity]
		if obj(previous["capture"])["completeness"] == "available" {
			switch capture["completeness"] {
			case "missing", "pruned", "partial", "unsupported", "size_limit":
				continue
			}
		}
		content := clone(observation)
		content["event_id"] = identity
		content["native_event_id"] = native
		fingerprint := compactDigest(content)
		if previous["content_sha256"] == fingerprint {
			continue
		}
		record := clone(content)
		record["format"] = "promptless.cursor"
		record["version"] = 1
		record["adapter"] = cursorAdapter
		record["session_id"] = id
		record["content_sha256"] = fingerprint
		record["revision"] = number(previous["revision"]) + 1
		record["observed_at"] = nowISO()
		encoded := append(compactJSON(record), '\n')
		if len(encoded) > cursorRecordLimit {
			event := clone(obj(record["event"]))
			for _, key := range []string{"input", "output", "text", "data"} {
				if _, ok := event[key]; ok {
					switch key {
					case "text":
						event[key] = ""
					case "data":
						event[key] = object{}
					default:
						event[key] = nil
					}
				}
			}
			record["event"] = event
			limited := clone(capture)
			limited["completeness"] = "size_limit"
			record["capture"] = limited
			encoded = append(compactJSON(record), '\n')
			if len(encoded) > cursorRecordLimit {
				return "", errors.New("cursor_record_metadata_size_limit")
			}
		}
		if spoolBytes+int64(len(encoded)) > cursorSpoolLimit {
			return "", errors.New("cursor_spool_size_limit")
		}
		if position+int64(len(encoded)) > cursorJournalLimit {
			return "", errors.New("cursor_journal_size_limit")
		}
		if _, err = f.Write(encoded); err != nil {
			return "", err
		}
		position += int64(len(encoded))
		spoolBytes += int64(len(encoded))
		latest[identity] = record
		aliases[native] = identity
	}
	if err = f.Sync(); err != nil {
		return "", err
	}
	return path, nil
}

type cursorTask struct{ ID, Path, Owner string }

func stringList(v any) []string {
	result := []string{}
	if a, ok := v.([]string); ok {
		return a
	}
	a, _ := v.([]any)
	for _, v := range a {
		if s, ok := v.(string); ok {
			result = append(result, s)
		}
	}
	return result
}
func prepareCursorJournals(context HookContext, lifecycle string) (HookContext, bool, error) {
	lock, err := acquireLock(filepath.Join(cursorSpool(), "export.lock"), time.Time{}, false)
	if err != nil {
		return context, false, err
	}
	if lock == nil {
		context.Transcript = ""
		context.AgentTranscript = ""
		return context, false, nil
	}
	defer lock.close()
	subjectPath := context.Transcript
	subject := context.SessionID
	if lifecycle == "subagent_stop" {
		subjectPath = context.AgentTranscript
		subject = context.AgentID
	}
	if subjectPath != "" {
		subject = strings.TrimSuffix(filepath.Base(subjectPath), filepath.Ext(subjectPath))
	}
	if !cursorSessionPattern.MatchString(subject) {
		subject = ""
	}
	statePath := filepath.Join(cursorSpool(), "scan-state.json")
	state, err := loadState(statePath)
	if err != nil {
		return context, false, err
	}
	traversals := obj(state["traversals"])
	revision := cursorRevision()
	after := str(state["after"])
	discovered := map[string]string{}
	discoveryDeadline := time.Now().Add(time.Second)
	truncated := false
	_ = filepath.WalkDir(homePath(".cursor", "projects"), func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if time.Now().After(discoveryDeadline) || len(discovered) >= 10000 {
			truncated = true
			return fs.SkipAll
		}
		if !entry.IsDir() && filepath.Ext(path) == ".jsonl" && strings.Contains(path, string(filepath.Separator)+"agent-transcripts"+string(filepath.Separator)) {
			id := strings.TrimSuffix(entry.Name(), ".jsonl")
			if cursorSessionPattern.MatchString(id) && id != subject {
				discovered[id] = path
			}
		}
		return nil
	})
	ordered := []string{}
	for id := range discovered {
		ordered = append(ordered, id)
	}
	sort.Strings(ordered)
	split := sort.SearchStrings(ordered, after)
	for split < len(ordered) && ordered[split] <= after {
		split++
	}
	ordered = append(append([]string{}, ordered[split:]...), ordered[:split]...)
	owners := []string{""}
	pending := []cursorTask{}
	scheduled := map[[2]string]bool{}
	if subject != "" {
		owners = []string{subject, ""}
		pending = append(pending, cursorTask{subject, subjectPath, subject})
		scheduled[[2]string{subject, subject}] = true
	}
	completed := map[string]map[string]bool{}
	offsets := map[string]object{}
	retries := map[string]object{}
	for _, owner := range owners {
		traversal := obj(traversals[owner])
		offsets[owner] = obj(traversal["offsets"])
		retries[owner] = obj(traversal["retry_offsets"])
		completed[owner] = map[string]bool{}
		if revision != "" && revision == str(traversal["database_revision"]) {
			for _, id := range stringList(traversal["completed"]) {
				completed[owner][id] = true
			}
		}
		ids := stringList(traversal["pending"])
		if owner == "" {
			ids = append(ids, ordered[:min(16, len(ordered))]...)
		}
		for _, id := range ids {
			key := [2]string{id, owner}
			if !scheduled[key] {
				pending = append(pending, cursorTask{id, discovered[id], owner})
				scheduled[key] = true
			}
		}
	}
	seen := map[[2]string]bool{}
	retry := []cursorTask{}
	deadline := time.Now().Add(5 * time.Second)
	current := ""
	errorCounts := map[string]int{}
	for len(pending) > 0 && time.Now().Before(deadline) && len(seen) < 32 {
		task := pending[0]
		pending = pending[1:]
		key := [2]string{task.ID, task.Owner}
		if seen[key] || !cursorSessionPattern.MatchString(task.ID) {
			continue
		}
		seen[key] = true
		var retryOffset *int
		if n, ok := integer(retries[task.Owner][task.ID]); ok {
			v := int(n)
			retryOffset = &v
		}
		page, readErr := readCursorSession(task.ID, minTime(deadline, time.Now().Add(500*time.Millisecond)), int(number(offsets[task.Owner][task.ID])), retryOffset)
		saveOffset := readErr == nil
		if readErr != nil {
			errorCounts["decode_or_budget"]++
			page = cursorPage{}
		}
		if len(page.Records) == 0 {
			fallback, err := cursorFallback(task.Path)
			if err != nil {
				errorCounts["transcript_unreadable"]++
			} else {
				page.Records = fallback
			}
		}
		if task.ID == subject && task.Owner == subject && lifecycle != "" && page.Next == 0 && page.Complete {
			generation := context.GenerationID
			if generation == "" {
				generation = "unknown"
			}
			parent, agent := context.ParentSessionID, context.AgentID
			if lifecycle == "subagent_stop" {
				parent = context.SessionID
				agent = subject
			}
			page.Records = append(page.Records, object{"event_id": "lifecycle:" + generation + ":" + lifecycle, "generation_id": nullable(context.GenerationID), "parent_session_id": nullable(parent), "agent_id": nullable(agent), "event": object{"kind": "session_event", "name": lifecycle, "data": object{}}, "capture": object{"source": "hook", "completeness": "available"}})
		}
		if len(page.Records) > 0 {
			path, err := appendCursorObservations(task.ID, page.Records)
			if err != nil {
				errorCounts["journal_write"]++
				saveOffset = false
				page.Complete = false
			} else if task.ID == subject && task.Owner == subject {
				current = path
			}
		}
		if saveOffset {
			offsets[task.Owner][task.ID] = page.Next
			if page.Retry == nil {
				delete(retries[task.Owner], task.ID)
			} else {
				retries[task.Owner][task.ID] = *page.Retry
			}
		}
		for _, child := range page.Children {
			key := [2]string{child, task.Owner}
			if !scheduled[key] && !completed[task.Owner][child] {
				pending = append(pending, cursorTask{child, discovered[child], task.Owner})
				scheduled[key] = true
			}
		}
		if task.Owner == "" {
			after = task.ID
		}
		if page.Complete {
			completed[task.Owner][task.ID] = true
		} else {
			delete(completed[task.Owner], task.ID)
			retry = append(retry, task)
		}
	}
	remaining := append(append([]cursorTask{}, pending...), retry...)
	complete := true
	for _, owner := range owners {
		owned := []string{}
		hasPending := false
		for _, task := range remaining {
			if task.Owner == owner {
				owned = append(owned, task.ID)
				if owner == subject {
					complete = false
				}
			}
		}
		for _, task := range pending {
			hasPending = hasPending || task.Owner == owner
		}
		if len(owned) > 0 {
			done := []string{}
			if hasPending {
				for id := range completed[owner] {
					done = append(done, id)
				}
				sort.Strings(done)
			}
			traversals[owner] = object{"pending": owned, "offsets": offsets[owner], "retry_offsets": retries[owner], "completed": done, "database_revision": nullable(revision)}
		} else {
			delete(traversals, owner)
		}
	}
	if err = writeState(statePath, object{"after": after, "traversals": traversals}); err != nil {
		return context, false, err
	}
	if err = writeState(filepath.Join(cursorSpool(), "diagnostics.json"), object{"adapter": cursorAdapter, "sessions_observed": len(seen), "errors": errorCounts, "pending_sessions": len(remaining), "discovery_truncated": truncated, "observed_at": nowISO()}); err != nil {
		return context, false, err
	}
	context.Transcript = current
	context.AgentTranscript = ""
	context.SessionID = subject
	return context, complete, nil
}
