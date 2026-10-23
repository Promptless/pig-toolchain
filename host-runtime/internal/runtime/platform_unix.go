//go:build !windows

package runtime

import (
	"errors"
	"golang.org/x/sys/unix"
	"os"
	"os/exec"
	"syscall"
)

func tryFileLock(f *os.File) (bool, error) {
	err := unix.Flock(int(f.Fd()), unix.LOCK_EX|unix.LOCK_NB)
	if errors.Is(err, unix.EWOULDBLOCK) || errors.Is(err, unix.EAGAIN) {
		return false, nil
	}
	return err == nil, err
}
func unlockFile(f *os.File)             { _ = unix.Flock(int(f.Fd()), unix.LOCK_UN) }
func replaceFile(from, to string) error { return os.Rename(from, to) }
func syncDirectory(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}
func detachProcess(cmd *exec.Cmd) { cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true} }

func createDetachedInput() (*os.File, error) { return os.CreateTemp("", "promptless-hook-*") }
