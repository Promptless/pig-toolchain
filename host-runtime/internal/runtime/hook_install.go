package runtime

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"time"
)

// Terminal hooks must outlive the plugin cache directory loaded by a session.
// Keep immutable executable copies and atomically switch small launchers instead
// of replacing a running executable (which Windows does not allow).
func installHookRuntime(root string) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	return installHookExecutable(exe, root)
}

func installHookExecutable(exe, root string) error {
	base := stateSibling("runtime")
	lock, err := acquireLock(filepath.Join(base, "install.lock"), time.Now().Add(5*time.Second), true)
	if err != nil {
		return err
	}
	defer lock.close()
	binary, err := os.ReadFile(exe)
	if err != nil {
		return err
	}
	digest := hash(binary)
	name := executableName
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	destination := filepath.Join(base, digest, name)
	existing, err := os.ReadFile(destination)
	switch {
	case errors.Is(err, os.ErrNotExist):
		if err = atomicWriteMode(destination, binary, 0700); err != nil {
			return err
		}
	case err != nil:
		return err
	case hash(existing) != digest:
		return errors.New("installed hook runtime digest mismatch")
	}
	if root != "" {
		config, err := runtimeConfigBytes(root, false)
		if err != nil {
			return err
		}
		manifest, err := os.ReadFile(filepath.Join(root, "hub.managed-runtimes.json"))
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		if err == nil {
			if _, err = decodeObject(manifest); err != nil {
				return fmt.Errorf("invalid plugin runtime metadata: %w", err)
			}
			if err = atomicWrite(hookMetadataPath(root), manifest); err != nil {
				return err
			}
		}
		if err = atomicWrite(hookMetadataPath(root)+".config", config); err != nil {
			return err
		}
	}
	// Only our hex digest and fixed filename enter shell source. User paths are
	// resolved relative to the launcher, never interpolated into executable code.
	posix := "#!/bin/sh\nexec \"${0%/*}/../runtime/" + digest + "/" + name + "\" \"$@\"\n"
	if err = atomicWriteMode(stateSibling(filepath.Join("bin", executableName)), []byte(posix), 0700); err != nil {
		return err
	}
	if runtime.GOOS == "windows" {
		batch := "@echo off\r\n\"%~dp0..\\runtime\\" + digest + "\\" + name + "\" %*\r\nexit /b %ERRORLEVEL%\r\n"
		return atomicWriteMode(stateSibling(filepath.Join("bin", executableName+".cmd")), []byte(batch), 0700)
	}
	return nil
}

func hookMetadataPath(root string) string {
	// Do not resolve symlinks: this key must stay the same after root is deleted.
	absolute, err := filepath.Abs(root)
	if err == nil {
		root = absolute
	}
	return stateSibling(filepath.Join("runtime", "plugins", hash([]byte(filepath.Clean(root)))+".json"))
}
