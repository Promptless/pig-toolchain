package runtime

import (
	"errors"
	"os"
	"path/filepath"
	"sort"
	"time"
)

func cursorNotify(event object, lifecycle string, out *output) error {
	root := cursorSpool()
	queue := filepath.Join(root, "pending")
	if err := os.MkdirAll(queue, 0700); err != nil {
		return err
	}
	payload := object{"lifecycle": lifecycle}
	for _, key := range []string{"conversation_id", "generation_id", "transcript_path", "agent_transcript_path", "parent_conversation_id", "agent_id"} {
		if s, ok := event[key].(string); ok && len([]rune(s)) <= 4096 {
			payload[key] = s
		}
	}
	path := filepath.Join(queue, compactDigest(payload)+".json")
	entries, _ := filepath.Glob(filepath.Join(queue, "*.json"))
	queued := exists(path) || len(entries) < 4096
	if queued {
		if err := writeState(path, payload); err != nil {
			return err
		}
	}
	lock, err := acquireLock(filepath.Join(root, "collector.lock"), time.Time{}, false)
	if err != nil {
		return err
	}
	if lock == nil {
		if !queued {
			return errors.New("cursor_pending_limit")
		}
		return nil
	}
	defer lock.close()
	if err = ensure(Cursor, true, false, false, out); err != nil {
		return err
	}
	deadline := time.Now().Add(time.Minute)
	for _, delay := range []time.Duration{200 * time.Millisecond, 2 * time.Second, 5 * time.Second} {
		time.Sleep(delay)
		entries, _ = filepath.Glob(filepath.Join(queue, "*.json"))
		sort.Slice(entries, func(i, j int) bool {
			if entries[i] == path {
				return entries[j] != path
			}
			if entries[j] == path {
				return false
			}
			a, ea := os.Stat(entries[i])
			b, eb := os.Stat(entries[j])
			if ea != nil || eb != nil {
				return entries[i] < entries[j]
			}
			return a.ModTime().Before(b.ModTime())
		})
		for _, pending := range entries[:min(16, len(entries))] {
			if time.Now().After(deadline) {
				if !queued {
					return errors.New("cursor_pending_limit")
				}
				return nil
			}
			now := time.Now()
			if err = os.Chtimes(pending, now, now); err != nil {
				return err
			}
			item, err := loadState(pending)
			if err != nil {
				return err
			}
			ev := str(item["lifecycle"])
			if !validLifecycle(ev) {
				continue
			}
			complete, err := collect(Cursor, ev, hookContext(item), true, true, out)
			if err != nil {
				return err
			}
			if delay == 5*time.Second && complete {
				if err = os.Remove(pending); err != nil && !errors.Is(err, os.ErrNotExist) {
					return err
				}
			}
		}
	}
	if !queued {
		entries, _ = filepath.Glob(filepath.Join(queue, "*.json"))
		if len(entries) >= 4096 {
			return errors.New("cursor_pending_limit")
		}
		return writeState(path, payload)
	}
	return nil
}
