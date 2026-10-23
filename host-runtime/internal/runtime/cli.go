package runtime

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

type commandOptions struct {
	Host                                                    string
	Lifecycle, Config, EventFile                            string
	Quiet, IfSources, Active, Detach, Supervised, JSON, Yes bool
}

func parseCommand(args []string, stderr io.Writer) (string, commandOptions, error) {
	o := commandOptions{Host: "auto"}
	if len(args) == 0 {
		return "", o, errors.New("expected command: version, status, enroll, reset, ensure, session-start, collect, cursor-notify, cloud-collect")
	}
	name := args[0]
	f := flag.NewFlagSet(name, flag.ContinueOnError)
	f.SetOutput(stderr)
	switch name {
	case "version":
		f.BoolVar(&o.JSON, "json", false, "Print version metadata as JSON")
	case "cloud-collect":
		f.StringVar(&o.Config, "config", "", "Cloud integration configuration")
		f.BoolVar(&o.Detach, "detach", false, "Detach capture")
		f.StringVar(&o.EventFile, "event-file", "", "Preserved hook input")
	case "cursor-notify":
		f.StringVar(&o.Lifecycle, "lifecycle", "", "Host lifecycle event")
		f.BoolVar(&o.Detach, "detach", false, "Detach notification collector")
	case "ensure", "session-start", "collect", "status", "enroll", "reset":
		f.StringVar(&o.Host, "host", "auto", "auto, codex, claude, claude-desktop, or cursor")
		if name == "reset" {
			f.BoolVar(&o.Yes, "yes", false, "Confirm local state reset")
		}
		if name == "ensure" || name == "collect" {
			f.BoolVar(&o.Quiet, "quiet", false, "Suppress hook status output")
		}
		if name == "ensure" || name == "collect" || name == "session-start" {
			f.BoolVar(&o.IfSources, "if-sources", false, "Skip when no native sources exist")
		}
		if name == "collect" || name == "session-start" {
			f.BoolVar(&o.Detach, "detach", false, "Detach collector")
			f.BoolVar(&o.Supervised, "supervised", false, "Supervise collector")
		}
		if name == "collect" {
			f.StringVar(&o.Lifecycle, "lifecycle", "", "Host lifecycle event")
			f.BoolVar(&o.Active, "include-active", false, "Include active native sources")
		}
	default:
		return name, o, fmt.Errorf("unknown command: %s", name)
	}
	if err := f.Parse(args[1:]); err != nil {
		return name, o, err
	}
	if f.NArg() != 0 {
		return name, o, errors.New("unexpected positional arguments")
	}
	if o.Host != "auto" && !Host(o.Host).valid() {
		return name, o, errors.New("invalid host")
	}
	if o.Detach && o.Supervised {
		return name, o, errors.New("--detach and --supervised are mutually exclusive")
	}
	if name == "session-start" && !o.Detach && !o.Supervised {
		return name, o, errors.New("session-start requires --detach or --supervised")
	}
	if name == "reset" && !o.Yes {
		return name, o, errors.New("reset requires --yes")
	}
	if name == "cloud-collect" && o.Config == "" {
		return name, o, errors.New("cloud-collect requires --config")
	}
	if name == "cursor-notify" && o.Lifecycle == "" {
		return name, o, errors.New("cursor-notify requires --lifecycle")
	}
	if o.Lifecycle != "" && !validLifecycle(o.Lifecycle) {
		return name, o, errors.New("invalid lifecycle")
	}
	return name, o, nil
}
func validLifecycle(s string) bool {
	return s == "session_start" || s == "stop" || s == "session_end" || s == "subagent_stop"
}
func resolveHost(value string) Host {
	if value != "auto" {
		return Host(value)
	}
	if os.Getenv("CURSOR_PLUGIN_ROOT") != "" {
		return Cursor
	}
	if os.Getenv("CLAUDE_PLUGIN_ROOT") != "" {
		return Claude
	}
	return Codex
}
func readInput(reader io.Reader) ([]byte, error) {
	if f, ok := reader.(*os.File); ok {
		if s, err := f.Stat(); err == nil && s.Mode()&os.ModeCharDevice != 0 {
			return []byte("{}"), nil
		}
	}
	b, err := io.ReadAll(io.LimitReader(reader, maxStdin+1))
	if err != nil {
		return nil, err
	}
	if len(b) > maxStdin {
		return nil, errors.New("hook input is too large")
	}
	if len(b) == 0 {
		b = []byte("{}")
	}
	return b, nil
}
func spawnDetached(args []string, input []byte) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	f, err := createDetachedInput()
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if _, err = f.Write(input); err != nil {
		return err
	}
	if _, err = f.Seek(0, io.SeekStart); err != nil {
		return err
	}
	null, err := os.OpenFile(os.DevNull, os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer null.Close()
	cmd := exec.Command(exe, args...)
	cmd.Stdin = f
	cmd.Stdout = null
	cmd.Stderr = null
	detachProcess(cmd)
	if err = cmd.Start(); err != nil {
		return err
	}
	return cmd.Process.Release()
}

// Main keeps stdout restricted to one host control object or command response.
func Main(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	name, o, err := parseCommand(args, stderr)
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	out := &output{stdout: stdout, stderr: stderr}
	defer out.flush()
	h := resolveHost(o.Host)
	if name == "version" {
		if o.JSON {
			out.command(runtimeInfo())
		} else {
			fmt.Fprintf(stdout, "%s %s\n", executableName, Version)
		}
		return 0
	}
	var payload object
	switch name {
	case "status":
		payload, err = statusPayload(h)
	case "reset":
		payload, err = resetState(h)
	case "enroll":
		payload, err = runEnroll(h, out)
	case "ensure":
		err = ensure(h, o.Quiet, o.IfSources, true, out)
	case "cloud-collect":
		err = runCloud(o, stdin, out)
	default:
		var raw []byte
		if name == "cursor-notify" {
			raw, err = readCursorInput(stdin)
		} else {
			raw, err = readInput(stdin)
		}
		if err == nil {
			if o.Detach {
				child := []string{name}
				for i := 1; i < len(args); i++ {
					if args[i] != "--detach" {
						child = append(child, args[i])
					}
				}
				if name != "cursor-notify" {
					child = append(child, "--supervised")
				}
				if name == "collect" && o.IfSources && !hasSources(h) {
					out.emit(object{"status": "trace_upload_skipped", "reason": "no_sources", "host": h}, o.Quiet)
				} else {
					err = spawnDetached(child, raw)
					if err == nil && name == "session-start" {
						pendingNotices(h.enrollment(), out)
					}
				}
			} else {
				var event object
				event, err = decodeObject(raw)
				if err == nil {
					switch name {
					case "cursor-notify":
						err = cursorNotify(event, o.Lifecycle, out)
					case "session-start":
						err = ensure(h, false, o.IfSources, false, out)
						if err != nil {
							recordFailure(h, "session_start_failed", "ensure", 1, err)
							return 1
						}
						var complete bool
						complete, err = runCollect(h, "session_start", hookContext(event), true, o.IfSources, true, out)
						if err == nil && h == Cursor && !complete {
							recordFailure(h, "collector_failed", "collect", 1, nil)
							return 1
						}
						if h == Claude {
							_, desktopErr := runCollect(ClaudeDesktop, "session_start", HookContext{}, true, true, true, out)
							if err == nil {
								err = desktopErr
							}
						}
					case "collect":
						var complete bool
						complete, err = runCollect(h, o.Lifecycle, hookContext(event), o.Active, o.IfSources, o.Quiet, out)
						if err == nil && h == Cursor && !complete {
							return 1
						}
					}
				}
			}
		}
	}
	if err != nil {
		if name == "cloud-collect" {
			fmt.Fprintf(stderr, "Cloud capture failed (%T); inspect the execution status file\n", err)
			return 1
		}
		if name == "cursor-notify" {
			recordFailure(Cursor, "collector_failed", "", 1, err)
			return 1
		}
		if name == "session-start" || o.Supervised || o.Detach {
			recordFailure(h, "collector_failed", "", 1, err)
		}
		failure := object{"status": "error", "host": h, "message": redactText(err.Error())}
		if name == "status" || name == "reset" || name == "enroll" {
			out.command(failure)
		} else {
			out.emit(failure, o.Quiet)
		}
		// Host hooks fail open on enrollment or upload failures. Detached supervisors
		// retain a nonzero exit so diagnostics can distinguish failed background work.
		if (name == "ensure" || name == "collect") && !o.Supervised && !o.Detach {
			return 0
		}
		return 1
	}
	if payload != nil {
		out.command(payload)
	}
	return 0
}
func runCollect(h Host, lifecycle string, context HookContext, active, ifSources, quiet bool, out *output) (bool, error) {
	if ifSources && !hasSources(h) {
		out.emit(object{"status": "trace_upload_skipped", "reason": "no_sources", "host": h}, quiet)
		return false, nil
	}
	complete, err := collect(h, lifecycle, context, active, quiet, out)

	return complete, err
}
func runCloud(o commandOptions, stdin io.Reader, out *output) error {
	var event object
	var err error
	if o.EventFile != "" {
		event, err = loadState(o.EventFile)
	} else {
		var raw []byte
		raw, err = readInput(stdin)
		if err == nil {
			event, err = decodeObject(raw)
		}
	}
	if err != nil {
		return err
	}
	if o.Detach {
		c, err := loadCloudConfig(o.Config)
		if err != nil {
			return err
		}
		root, _, _, err := executionScope(c, event)
		if err != nil {
			return err
		}
		token, err := randomToken("")
		if err != nil {
			return err
		}
		pending := filepath.Join(root, "hook-"+token+".json")
		if err = writeState(pending, event); err != nil {
			return err
		}
		err = spawnDetached([]string{"cloud-collect", "--config", resolvePath(o.Config), "--event-file", pending}, nil)
		if err != nil {
			_ = os.Remove(pending)
		}
		return err
	}
	if o.EventFile != "" {
		defer os.Remove(o.EventFile)
	}
	result, err := collectCloud(o.Config, event, 30*time.Second)
	if err != nil {
		return err
	}
	out.command(result)
	return nil
}

// Cursor must not hold the editor hook open if the host never closes stdin.
// A complete document is required; partial input is discarded after the budget.
func readCursorInput(reader io.Reader) ([]byte, error) {
	type result struct {
		data []byte
		err  error
	}
	done := make(chan result, 1)
	go func() { b, err := readInput(reader); done <- result{b, err} }()
	select {
	case r := <-done:
		return r.data, r.err
	case <-time.After(200 * time.Millisecond):
		return []byte("{}"), nil
	}
}
