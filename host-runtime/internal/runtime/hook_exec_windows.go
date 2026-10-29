package runtime

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"time"
)

func portableHookPython() ([]string, error) {
	// Traditional Windows installations use python.exe or the py launcher.
	// Probe before selecting: python3.exe may be a nonfunctional Store alias.
	for _, candidate := range [][]string{{"python3"}, {"python"}, {"py", "-3"}} {
		python, err := exec.LookPath(candidate[0])
		if err != nil {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		args := append(append([]string{}, candidate[1:]...), "-I", "-c", "import sys; sys.exit(sys.version_info < (3, 9))")
		err = exec.CommandContext(ctx, python, args...).Run()
		cancel()
		if err == nil {
			return append([]string{python}, candidate[1:]...), nil
		}
	}
	return nil, errors.New("no supported Python interpreter found")
}

// Windows has no exec replacement. Keep the child attached to the host's console
// and process tree; do not detach it like the telemetry collectors.
func execPortableHook(python string, argv []string) error {
	cmd := exec.Command(python, argv[1:]...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	return cmd.Run()
}
