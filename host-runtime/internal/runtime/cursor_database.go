package runtime

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	_ "modernc.org/sqlite"
)

const cursorAdapter = "cursor-desktop-3.19-v1"

type cursorPage struct {
	Records  []object
	Children []string
	Next     int
	Complete bool
	Retry    *int
}
type savedResult struct {
	Output  any
	Quality string
}

func cursorDBPath() string {
	if v := os.Getenv("PROMPTLESS_CURSOR_DATABASE"); v != "" {
		return expandPath(v)
	}
	return filepath.Join(appConfigRoot("Cursor"), "User", "globalStorage", "state.vscdb")
}

type cursorSnapshot struct {
	tx       *sql.Tx
	ctx      context.Context
	Deadline time.Time
	Bytes    int64
}

func (s *cursorSnapshot) read(key string) ([]byte, error) {
	if time.Now().After(s.Deadline) || s.Bytes >= 32<<20 {
		return nil, errors.New("cursor_snapshot_budget")
	}
	var size sql.NullInt64
	err := s.tx.QueryRowContext(s.ctx, "SELECT length(value) FROM cursorDiskKV WHERE key = ?", key).Scan(&size)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !size.Valid {
		return nil, nil
	}
	if size.Int64 > cursorBlobLimit {
		return nil, errors.New("cursor_blob_size_limit")
	}
	if s.Bytes+size.Int64 > 32<<20 {
		return nil, errors.New("cursor_snapshot_size_limit")
	}
	var data []byte
	if err = s.tx.QueryRowContext(s.ctx, "SELECT value FROM cursorDiskKV WHERE key = ?", key).Scan(&data); err != nil {
		return nil, err
	}
	s.Bytes += int64(len(data))
	return data, nil
}
func (s *cursorSnapshot) object(key string) (object, error) {
	b, err := s.read(key)
	if err != nil {
		return nil, err
	}
	if len(b) == 0 {
		return object{}, nil
	}
	return decodeObject(b)
}
func (s *cursorSnapshot) referencedBlob(reference []byte) ([]byte, error) {
	if len(reference) == 0 || len(reference) > 64 {
		return nil, nil
	}
	b, err := s.read("agentKv:blob:" + hex.EncodeToString(reference))
	if err != nil {
		return nil, err
	}
	if bytes.Equal(b, []byte{0}) {
		return nil, nil
	}
	if len(b) > 0 && len(b)%2 == 0 {
		if decoded, e := hex.DecodeString(string(b)); e == nil {
			return decoded, nil
		}
	}
	return b, nil
}
func retryableCursor(err error) bool {
	if err == nil {
		return false
	}
	s := err.Error()
	return s == "cursor_snapshot_budget" || s == "cursor_snapshot_size_limit" || strings.Contains(s, "SQLITE_") || errors.Is(err, context.DeadlineExceeded)
}
func (s *cursorSnapshot) saved(binary any) (savedResult, error) {
	missing := savedResult{nil, "missing"}
	var data []byte
	switch value := binary.(type) {
	case string:
		if value == "" {
			return missing, nil
		}
		var err error
		data, err = base64.StdEncoding.Strict().DecodeString(value)
		if err != nil {
			return savedResult{nil, "unsupported"}, nil
		}
	case []byte:
		data = value
	case nil:
		return missing, nil
	default:
		return savedResult{nil, "unsupported"}, nil
	}
	_, output, quality, err := toolResult(data, 0)
	if err != nil {
		return savedResult{nil, "unsupported"}, nil
	}
	for _, key := range []string{"output_blob_id", "content_blob_id", "data_blob_id"} {
		ref, ok := output[key].(string)
		delete(output, key)
		if !ok {
			continue
		}
		reference, err := hex.DecodeString(ref)
		if err != nil {
			quality = "unsupported"
			continue
		}
		saved, err := s.referencedBlob(reference)
		if err != nil {
			if retryableCursor(err) {
				return savedResult{}, err
			}
			quality = "unsupported"
			if err.Error() == "cursor_blob_size_limit" {
				quality = "size_limit"
			}
			continue
		}
		if len(saved) > 0 && key != "data_blob_id" {
			if utf8.Valid(saved) {
				output["content"] = string(saved)
			} else {
				quality = "unsupported"
			}
		} else {
			quality = "pruned"
		}
	}
	for _, key := range []string{"truncated", "exceeded_limit", "elided_chars", "output_location"} {
		if truthy(output[key]) {
			quality = "pruned"
		}
	}
	return savedResult{output, quality}, nil
}
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int:
		return x != 0
	case int64:
		return x != 0
	case uint64:
		return x != 0
	case json.Number:
		return x != "0"
	case object:
		return len(x) > 0
	case []any:
		return len(x) > 0
	}
	return true
}
func jsonValue(value any) any {
	if s, ok := value.(string); ok {
		d := json.NewDecoder(strings.NewReader(s))
		d.UseNumber()
		var decoded any
		if d.Decode(&decoded) == nil {
			return decoded
		}
	}
	return value
}
func bubbleEvents(id string, bubble object, result savedResult, metadata object) []object {
	records := []object{}
	timestamp := str(bubble["createdAt"])
	if _, err := time.Parse(time.RFC3339Nano, timestamp); err != nil {
		timestamp = ""
	}
	add := func(identity string, event object, quality string) {
		if timestamp != "" {
			event["t"] = timestamp
		}
		record := clone(metadata)
		record["event_id"] = identity
		record["event"] = event
		record["generation_id"] = nullable(str(bubble["requestId"]))
		record["capture"] = object{"source": "database", "completeness": quality}
		records = append(records, record)
	}
	if text := str(bubble["text"]); text != "" {
		kind := "assistant_message"
		if number(bubble["type"]) == 1 {
			kind = "user_message"
		}
		add(id+":text", object{"kind": kind, "text": text}, "available")
	}
	if text := str(bubble["thinking"]); text != "" {
		add(id+":thinking", object{"kind": "reasoning", "text": text}, "available")
	}
	tool := obj(bubble["toolFormerData"])
	if len(tool) == 0 {
		return records
	}
	name := str(tool["name"])
	if name == "" {
		name = "unknown"
	}
	callID := str(tool["toolCallId"])
	if callID == "" {
		callID = id
	}
	params, ok := tool["params"]
	if !ok {
		params = tool["rawArgs"]
	}
	add(callID+":call", object{"kind": "tool_call", "tool": name, "call_id": callID, "input": jsonValue(params)}, "available")
	output := obj(result.Output)
	resultError := truthy(output["error"]) || truthy(output["is_error"]) || truthy(output["exit_code"])
	ui := jsonValue(tool["result"])
	if ui != nil && (!truthy(result.Output) || result.Quality == "missing" || result.Quality == "unsupported" || result.Quality == "pruned" || result.Quality == "partial") {
		result.Output = object{"saved_result": result.Output, "ui_result": ui}
		result.Quality = "partial"
	}
	status := str(tool["status"])
	if status == "cancelled" {
		result.Quality = "interrupted"
	}
	add(callID+":result", object{"kind": "tool_result", "tool": name, "call_id": callID, "is_error": resultError || status == "error" || status == "cancelled", "output": result.Output}, result.Quality)
	return records
}
func canonicalTools(snapshot *cursorSnapshot, composer object, wanted map[string]bool, result map[string][]byte) error {
	encoded := str(composer["conversationState"])
	if encoded == "" {
		return nil
	}
	data, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil {
		return err
	}
	state, err := wireFields(data)
	if err != nil {
		return err
	}
	refs := state[8]
	if len(refs) > 4000 {
		refs = refs[len(refs)-4000:]
	}
	for i := len(refs) - 1; i >= 0; i-- {
		ref := refs[i]
		if ref.Scalar {
			continue
		}
		data, err := snapshot.referencedBlob(ref.Bytes)
		if err != nil {
			return err
		}
		if len(data) == 0 {
			continue
		}
		outer, err := wireFields(data)
		if err != nil {
			return err
		}
		turn, err := wireFields(outer.blob(1))
		if err != nil {
			return err
		}
		steps := turn[2]
		if len(steps) > 4000 {
			steps = steps[:4000]
		}
		for _, ref := range steps {
			if ref.Scalar {
				continue
			}
			data, err := snapshot.referencedBlob(ref.Bytes)
			if err != nil {
				return err
			}
			if len(data) == 0 {
				continue
			}
			step, err := wireFields(data)
			if err != nil {
				return err
			}
			call := step.blob(2)
			if len(call) > 0 {
				message, err := wireFields(call)
				if err != nil {
					return err
				}
				id, err := message.text(57)
				if err != nil {
					return err
				}
				if wanted[id] {
					result[id] = call
				}
			}
			if len(result) == len(wanted) {
				return nil
			}
		}
	}
	return nil
}
func readCursorSession(id string, deadline time.Time, offset int, retry *int) (cursorPage, error) {
	page := cursorPage{Records: []object{}, Children: []string{}, Complete: true}
	path := resolvePath(cursorDBPath())
	slashPath := filepath.ToSlash(path)
	if !strings.HasPrefix(slashPath, "/") {
		slashPath = "/" + slashPath
	}
	uri := (&url.URL{Scheme: "file", Path: slashPath, RawQuery: "mode=ro"}).String()
	db, err := sql.Open("sqlite", uri)
	if err != nil {
		return page, err
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	ctx, cancel := context.WithDeadline(context.Background(), deadline)
	defer cancel()
	if _, err = db.ExecContext(ctx, "PRAGMA query_only = ON"); err != nil {
		return page, err
	}
	var mode string
	if err = db.QueryRowContext(ctx, "PRAGMA journal_mode").Scan(&mode); err != nil {
		return page, err
	}
	if strings.ToLower(mode) != "wal" {
		return page, errors.New("cursor_database_requires_wal")
	}
	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return page, err
	}
	defer tx.Rollback()
	snapshot := &cursorSnapshot{tx: tx, ctx: ctx, Deadline: deadline}
	composer, err := snapshot.object("composerData:" + id)
	if err != nil {
		return page, err
	}
	if len(composer) == 0 {
		return page, errors.New("cursor_session_not_saved")
	}
	subagent := obj(composer["subagentInfo"])
	agentID := ""
	if len(subagent) > 0 {
		agentID = id
	}
	metadata := object{"parent_session_id": nullable(str(subagent["parentComposerId"])), "agent_id": nullable(agentID), "model": nullable(str(obj(composer["modelConfig"])["modelName"]))}
	headers, ok := composer["fullConversationHeadersOnly"].([]any)
	if !ok {
		return page, errors.New("cursor_unsupported_headers")
	}
	if offset < 0 || offset >= len(headers) {
		offset = 0
	}
	page.Next = offset
	type bubbleRead struct {
		Index  int
		ID     string
		Bubble object
		Result savedResult
	}
	bubbles := []bubbleRead{}
	retryOffsets := []int{}
	if retry != nil && *retry >= 0 && *retry < len(headers) {
		retryOffsets = append(retryOffsets, *retry)
	}
	gap := func(bubbleID, reason, quality string) {
		record := clone(metadata)
		record["event_id"] = bubbleID + ":capture_gap"
		record["event"] = object{"kind": "session_event", "name": "cursor_capture_gap", "data": object{"reason": reason}}
		record["capture"] = object{"source": "database", "completeness": quality}
		page.Records = append(page.Records, record)
	}
	for _, header := range headers[offset:min(offset+128, len(headers))] {
		bubbleID := str(obj(header)["bubbleId"])
		if bubbleID == "" {
			page.Next++
			continue
		}
		bubble, readErr := snapshot.object("bubbleId:" + id + ":" + bubbleID)
		result := savedResult{nil, "missing"}
		if readErr == nil {
			result, readErr = snapshot.saved(obj(bubble["toolFormerData"])["toolCallBinary"])
		}
		if readErr != nil {
			if readErr.Error() == "cursor_blob_size_limit" {
				gap(bubbleID, "size_limit", "size_limit")
				page.Next++
				continue
			}
			page.Complete = false
			break
		}
		if len(bubble) == 0 {
			gap(bubbleID, "missing_bubble", "missing")
			retryOffsets = append(retryOffsets, page.Next)
		}
		bubbles = append(bubbles, bubbleRead{page.Next, bubbleID, bubble, result})
		page.Next++
	}
	if page.Next >= len(headers) {
		page.Next = 0
	}
	wanted := map[string]bool{}
	for _, b := range bubbles {
		if callID := str(obj(b.Bubble["toolFormerData"])["toolCallId"]); callID != "" && b.Result.Quality != "available" {
			wanted[callID] = true
		}
	}
	canonical := map[string][]byte{}
	if len(wanted) > 0 {
		snapshot.Deadline = minTime(deadline, time.Now().Add(100*time.Millisecond))
		err = canonicalTools(snapshot, composer, wanted, canonical)
		if retryableCursor(err) {
			for _, b := range bubbles {
				callID := str(obj(b.Bubble["toolFormerData"])["toolCallId"])
				if wanted[callID] && canonical[callID] == nil {
					retryOffsets = append(retryOffsets, b.Index)
				}
			}
		}
		snapshot.Deadline = deadline
	}
	quality := map[string]int{"available": 3, "partial": 2, "pruned": 1}
	for _, b := range bubbles {
		result := b.Result
		callID := str(obj(b.Bubble["toolFormerData"])["toolCallId"])
		if data := canonical[callID]; data != nil {
			candidate, err := snapshot.saved(data)
			if retryableCursor(err) {
				retryOffsets = append(retryOffsets, b.Index)
			} else if err == nil && (result.Output == nil || quality[candidate.Quality] > quality[result.Quality]) {
				result = candidate
			}
		}
		page.Records = append(page.Records, bubbleEvents(b.ID, b.Bubble, result, metadata)...)
	}
	if len(retryOffsets) > 0 {
		earliest := retryOffsets[0]
		for _, i := range retryOffsets {
			earliest = min(earliest, i)
		}
		if page.Next == 0 {
			page.Next = earliest
		} else {
			page.Retry = &earliest
		}
		page.Complete = false
	}
	children, _ := composer["subagentComposerIds"].([]any)
	for _, child := range children {
		if s, ok := child.(string); ok {
			page.Children = append(page.Children, s)
			if len(page.Children) >= 64 {
				break
			}
		}
	}
	page.Complete = page.Complete && page.Next == 0
	return page, nil
}
func minTime(a, b time.Time) time.Time {
	if a.Before(b) {
		return a
	}
	return b
}
