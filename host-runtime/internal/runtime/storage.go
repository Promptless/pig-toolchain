package runtime

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"
)

func statePath() string {
	return homePath(".promptless", "instruction-hub", "host-enrollment-state.json")
}
func stateSibling(name string) string { return filepath.Join(filepath.Dir(statePath()), name) }
func ledgerPath() string {
	if p := os.Getenv("PROMPTLESS_HOST_RUNTIME_LEDGER"); nonempty(p) {
		return expandPath(p)
	}
	return stateSibling("host-runtime-ledger.json")
}
func scopedLedger(base, worker, deployment, hostID string, host Host) string {
	scope := object{"worker_base_url": strings.TrimRight(worker, "/"), "deployment_instance_id": deployment, "host_instance_id": hostID, "source": host}
	ext := filepath.Ext(base)
	return strings.TrimSuffix(base, ext) + "." + hash(legacyJSON(scope)) + ext
}
func loadState(path string) (object, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return object{}, nil
	}
	if err != nil {
		return nil, err
	}
	return decodeObject(b)
}
func writeState(path string, v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	return atomicWrite(path, append(b, '\n'))
}
func atomicWrite(path string, b []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(b); err != nil {
		f.Close()
		return err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = replaceFile(f.Name(), path); err != nil {
		return err
	}
	return syncDirectory(filepath.Dir(path))
}

type fileLock struct{ file *os.File }

func acquireLock(path string, deadline time.Time, wait bool) (*fileLock, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	for {
		ok, err := tryFileLock(f)
		if err != nil {
			f.Close()
			return nil, err
		}
		if ok {
			return &fileLock{f}, nil
		}
		if !wait {
			f.Close()
			return nil, nil
		}
		if !deadline.IsZero() && !time.Now().Before(deadline) {
			f.Close()
			return nil, errDeadline
		}
		time.Sleep(25 * time.Millisecond)
	}
}
func (l *fileLock) close() {
	if l != nil {
		unlockFile(l.file)
		l.file.Close()
	}
}
func editState(edit func(object) error) error {
	l, err := acquireLock(statePath()+".lock", time.Time{}, true)
	if err != nil {
		return err
	}
	defer l.close()
	s, err := loadState(statePath())
	if err != nil {
		return err
	}
	if err = edit(s); err != nil {
		return err
	}
	return writeState(statePath(), s)
}
