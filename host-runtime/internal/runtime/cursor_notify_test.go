package runtime

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func notificationFiles(t *testing.T) []string {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join(cursorSpool(), "pending", "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	return paths
}
func testNotifier(collect func(object) (bool, error)) cursorNotifier {
	return cursorNotifier{ensure: func() error { return nil }, collect: collect, sleep: func(time.Duration) {}}
}
func seedNotifications(t *testing.T, count int) {
	t.Helper()
	queue := filepath.Join(cursorSpool(), "pending")
	if err := os.MkdirAll(queue, 0700); err != nil {
		t.Fatal(err)
	}
	for i := range count {
		path := filepath.Join(queue, fmt.Sprintf("old-%04d.json", i))
		// Seed existing entries without paying for thousands of durable writes;
		// the notifier itself still uses production atomic persistence.
		mustWrite(t, path, legacyJSON(object{"conversation_id": fmt.Sprintf("old-%d", i), "lifecycle": "stop"}))
		at := time.Unix(int64(i+1), 0)
		if err := os.Chtimes(path, at, at); err != nil {
			t.Fatal(err)
		}
	}
}
func TestCursorPendingNotificationSurvivesIncompleteOrFailedUpload(t *testing.T) {
	for _, failure := range []error{nil, errors.New("upload failed")} {
		t.Run(fmt.Sprint(failure), func(t *testing.T) {
			privateHome(t)
			n := testNotifier(func(object) (bool, error) { return false, failure })
			err := n.run(object{"conversation_id": "session", "generation_id": "g", "secret": "never persist"}, "stop")
			if !errors.Is(err, failure) {
				t.Fatal(err)
			}
			paths := notificationFiles(t)
			if len(paths) != 1 {
				t.Fatal(paths)
			}
			data, _ := os.ReadFile(paths[0])
			if bytes.Contains(data, []byte("never persist")) {
				t.Fatal("notification retained secret")
			}
			attempts := 0
			n.collect = func(object) (bool, error) { attempts++; return true, nil }
			if err = n.run(object{"conversation_id": "session", "generation_id": "g"}, "stop"); err != nil {
				t.Fatal(err)
			}
			if attempts != 3 || len(notificationFiles(t)) != 0 {
				t.Fatal("queue did not survive through final settling pass")
			}
		})
	}
}
func TestCursorNotificationRotationDoesNotStarveCurrentOrQueuedWork(t *testing.T) {
	privateHome(t)
	seedNotifications(t, 32)
	attempted := []string{}
	seen := map[string]bool{}
	n := testNotifier(func(item object) (bool, error) {
		id := str(item["conversation_id"])
		attempted = append(attempted, id)
		seen[id] = true
		return id == "current", nil
	})
	if err := n.run(object{"conversation_id": "current"}, "stop"); err != nil {
		t.Fatal(err)
	}
	if attempted[0] != "current" || len(seen) != 33 || len(notificationFiles(t)) != 32 {
		t.Fatalf("rotation starved work: %v", attempted)
	}
	for _, path := range notificationFiles(t) {
		info, err := os.Stat(path)
		if err != nil || info.ModTime().Unix() <= 32 {
			t.Fatal("retry did not rotate", err)
		}
	}
}
func TestCursorFullNotificationQueueDrainsBeforeRetainingNewWork(t *testing.T) {
	privateHome(t)
	seedNotifications(t, 4096)
	attempted := map[string]bool{}
	n := testNotifier(func(item object) (bool, error) {
		if len(notificationFiles(t)) > 4096 {
			t.Fatal("queue exceeded bound")
		}
		attempted[str(item["conversation_id"])] = true
		return true, nil
	})
	if err := n.run(object{"conversation_id": "current"}, "stop"); err != nil {
		t.Fatal(err)
	}
	current := false
	for _, path := range notificationFiles(t) {
		item, err := loadState(path)
		if err != nil {
			t.Fatal(err)
		}
		current = current || item["conversation_id"] == "current"
	}
	if !current || len(attempted) == 0 {
		t.Fatal("full queue dropped current notification")
	}
	if err := n.run(object{"conversation_id": "current"}, "stop"); err != nil {
		t.Fatal(err)
	}
	if !attempted["current"] {
		t.Fatal("queued current notification was starved")
	}
}
func TestCursorBusyCollectorQueuesWithoutNetwork(t *testing.T) {
	privateHome(t)
	lock, err := acquireLock(filepath.Join(cursorSpool(), "collector.lock"), time.Time{}, true)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.close()
	n := testNotifier(func(object) (bool, error) { t.Fatal("busy collector started upload"); return false, nil })
	n.ensure = func() error { t.Fatal("busy collector enrolled"); return nil }
	started := time.Now()
	if err = n.run(object{"conversation_id": "session"}, "stop"); err != nil {
		t.Fatal(err)
	}
	if time.Since(started) > time.Second || len(notificationFiles(t)) != 1 {
		t.Fatal("busy collector waited or lost work")
	}
	lock.close()
	seen := map[string]bool{}
	n = testNotifier(func(item object) (bool, error) { seen[str(item["conversation_id"])] = true; return true, nil })
	if err = n.run(object{"conversation_id": "next"}, "session_start"); err != nil {
		t.Fatal(err)
	}
	if len(seen) != 2 || len(notificationFiles(t)) != 0 {
		t.Fatal("next wakeup failed to drain pending work")
	}
}
