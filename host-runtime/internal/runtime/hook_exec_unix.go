//go:build !windows

package runtime

import (
	"os"
	"os/exec"
	"syscall"
)

func portableHookPython() ([]string, error) {
	python, err := exec.LookPath("python3")
	return []string{python}, err
}

// Replace the launcher so host cancellation reaches the authored Python handler
// directly, with the original process ID and stdin/stdout descriptors.
func execPortableHook(python string, argv []string) error {
	return syscall.Exec(python, argv, os.Environ())
}
