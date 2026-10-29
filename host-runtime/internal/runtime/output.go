package runtime

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"time"
)

var bearerPattern = regexp.MustCompile(`Bearer\s+[A-Za-z0-9._~+/=-]+`)
var tokenPattern = regexp.MustCompile(`(plihost_|plihenroll_|plicg_|plicl_)[A-Za-z0-9._~+/=-]+`)

func redactText(s string) string {
	s = bearerPattern.ReplaceAllString(s, "Bearer <redacted>")
	return tokenPattern.ReplaceAllString(s, "${1}<redacted>")
}
func redact(v any) any {
	switch x := v.(type) {
	case string:
		return redactText(x)
	case Host:
		return string(x)
	case object:
		m := object{}
		for k, v := range x {
			switch strings.ToLower(k) {
			case "authorization", "device_code", "host_credential", "token", "producer_token":
				m[k] = "<redacted>"
			default:
				m[k] = redact(v)
			}
		}
		return m
	case []any:
		a := make([]any, len(x))
		for i, v := range x {
			a[i] = redact(v)
		}
		return a
	case []object:
		a := make([]any, len(x))
		for i, v := range x {
			a[i] = redact(v)
		}
		return a
	default:
		return v
	}
}

type output struct {
	stdout, stderr io.Writer
	pending        object
	priority       int
}

func (o *output) command(payload object) {
	b, _ := json.Marshal(redact(payload))
	fmt.Fprintln(o.stdout, string(b))
}
func (o *output) flush() {
	if o.pending != nil {
		o.command(o.pending)
		o.pending = nil
		o.priority = 0
	}
}
func hostName(h Host) string {
	switch h {
	case Claude:
		return "Claude Code"
	case ClaudeDesktop:
		return "Claude Desktop"
	case Codex:
		return "Codex"
	}
	return ""
}
func userMessage(p object) string {
	h := Host(str(p["host"]))
	if v, ok := p["host"].(Host); ok {
		h = v
	}
	name := hostName(h)
	status := str(p["status"])
	if status == "error" {
		if name == "" {
			name = "the agent host"
		}
		if details := str(p["message"]); details != "" {
			return "Promptless host enrollment failed for " + name + ": " + details
		}
		return "Promptless host enrollment failed for " + name + ". Check the hook diagnostic output."
	}
	if name == "" {
		return ""
	}
	switch status {
	case "browser_enrollment_starting":
		return "Promptless Instruction Governance telemetry is starting browser-based enrollment. Approve the Promptless browser tab to continue."
	case "needs_restart":
		return "Promptless removed legacy managed telemetry config from " + name + ". Restart " + name + " to apply the cleanup."
	case "setup_pending":
		switch str(p["reason"]) {
		case "browser_launch_failed":
			return "Promptless host enrollment could not open a browser for " + name + ". Restart from a desktop session with browser access, then approve the Promptless enrollment prompt. Details were saved to ~/.promptless/instruction-hub/last-bootstrap-status.json."
		case "browser_approval_timeout":
			return "Promptless host enrollment did not receive browser approval before timing out. Restart " + name + " and approve the Promptless enrollment prompt."
		case "enrollment_in_progress":
			return "Promptless host enrollment is already running in another installed Promptless plugin. Finish that approval, then restart " + name + "."
		case "approval_expired":
			return "Promptless host enrollment approval expired. Restart " + name + " to request a fresh approval."
		default:
			return "Promptless host enrollment is awaiting browser approval. Approve in the browser tab that opened, then restart " + name + "."
		}
	case "blocked":
		if p["reason"] == "bootstrap_upgrade_required" {
			return "Promptless host enrollment is blocked: a newer Promptless host runtime is required."
		}
		path := "~/.codex/config.toml"
		if h == Claude {
			path = "~/.claude/settings.json"
		} else if h == ClaudeDesktop {
			path = "Claude Desktop local audit store"
		}
		return "Promptless host enrollment is blocked: existing telemetry settings were left unchanged. Review " + path + "."
	}
	return ""
}
func (o *output) emit(p object, quiet bool, notices ...string) {
	parts := []string{}
	for _, s := range append(notices, userMessage(p)) {
		if s != "" {
			parts = append(parts, s)
		}
	}
	sep := " "
	for _, s := range parts {
		if strings.Contains(s, "\n") {
			sep = "\n\n"
		}
	}
	message := strings.Join(parts, sep)
	diagnostic := clone(p)
	control := object{}
	if message != "" {
		diagnostic["systemMessage"] = message
		control["systemMessage"] = message
	}
	h := fmt.Sprint(p["host"])
	status := str(p["status"])
	if message != "" && h == "claude" {
		body := ""
		switch status {
		case "error":
			body = "Host enrollment failed"
		case "blocked":
			body = "Host enrollment needs review"
		case "needs_restart":
			body = "Telemetry configured; restart Claude Code"
		case "browser_enrollment_starting", "setup_pending":
			body = "Host enrollment needs approval"
		}
		if body != "" {
			sequence := "\x1b]777;notify;Promptless;" + body + "\x07"
			diagnostic["terminalSequence"] = sequence
			control["terminalSequence"] = sequence
		}
	}
	writeDiagnostic(diagnostic)
	if quiet {
		return
	}
	writeLastStatus(diagnostic)
	b, _ := json.Marshal(redact(diagnostic))
	fmt.Fprintln(o.stderr, string(b))
	priority := map[string]int{"browser_enrollment_starting": 10, "configured": 20, "needs_restart": 80, "setup_pending": 90, "blocked": 100, "error": 110}[status]
	if len(control) > 0 && (o.pending == nil || priority >= o.priority) {
		o.pending = control
		o.priority = priority
	}
}
func writeLastStatus(p object) {
	p = clone(p)
	p["emitted_at"] = nowISO()
	_ = writeState(stateSibling("last-bootstrap-status.json"), redact(p))
}
func writeDiagnostic(p object) {
	p = clone(p)
	p["emitted_at"] = nowISO()
	b, _ := json.Marshal(redact(p))
	b = append(b, '\n')
	path := stateSibling("host-runtime-diagnostics.jsonl")
	l, err := acquireLock(path+".lock", time.Now().Add(time.Second), true)
	if err != nil {
		return
	}
	defer l.close()
	if info, err := os.Stat(path); err == nil && info.Size()+int64(len(b)) > 512<<10 {
		_ = os.Remove(path + ".1")
		if err = replaceFile(path, path+".1"); err != nil {
			return
		}
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.Write(b)
}
func recordFailure(h Host, reason, stage string, code int, err error) {
	p := object{"status": "error", "reason": reason, "host": h}
	if stage != "" {
		p["stage"] = stage
	}
	if err != nil {
		p["error_code"] = fmt.Sprintf("%T", err)
	} else {
		p["exit_code"] = code
	}
	writeDiagnostic(p)
	writeLastStatus(p)
}
