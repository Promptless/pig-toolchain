// Package runtime implements the installed host runtime. JSON maps are confined
// to versioned wire documents and state that must preserve unknown fields.
package runtime

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"
)

const Version = "0.4.0"
const executableName = "promptless-host-runtime"
const maxStdin = 1 << 20
const maxRecord = 10 << 20
const chunkTarget = 4 << 20
const maxTransport = 10 << 20

type object = map[string]any
type Host string

const (
	Codex         Host = "codex"
	Claude        Host = "claude"
	ClaudeDesktop Host = "claude-desktop"
	Cursor        Host = "cursor"
)

func (h Host) valid() bool { return h == Codex || h == Claude || h == ClaudeDesktop || h == Cursor }
func (h Host) enrollment() Host {
	if h == ClaudeDesktop {
		return Claude
	}
	return h
}

type Metadata struct {
	BootstrapVersion string `json:"bootstrap_version"`
	ToolchainVersion string `json:"toolchain_version"`
	PluginID         string `json:"plugin_id"`
	PluginName       string `json:"plugin_name"`
	PluginVersion    string `json:"plugin_version"`
	PackageID        string `json:"package_id"`
	Target           Host   `json:"target"`
}
type Credential struct {
	Value         string `json:"value"`
	ID            string `json:"credential_id,omitempty"`
	DeploymentID  string `json:"deployment_instance_id,omitempty"`
	Internal      bool   `json:"is_internal_promptless_user,omitempty"`
	ProducerToken string `json:"-"`
}
type Policy struct {
	Version         int64
	RequiredVersion string
}
type EnrollmentContext struct {
	Worker, Dashboard, DeploymentID, HostID, Label, Platform string
	Metadata                                                 Metadata
}
type EnrollmentSession struct {
	HostedAPI    string    `json:"hosted_api_base_url,omitempty"`
	ApprovalURL  string    `json:"approval_url,omitempty"`
	ID           string    `json:"session_id"`
	DeploymentID string    `json:"deployment_instance_id"`
	DeviceCode   string    `json:"device_code"`
	PollURL      string    `json:"poll_url"`
	ExpiresAt    time.Time `json:"expires_at"`
	PollInterval int       `json:"poll_interval_seconds"`
	Target       Host      `json:"target"`
	Worker       string    `json:"worker_base_url"`
}
type HookContext struct {
	Transcript, AgentTranscript, SessionID, ParentSessionID, AgentID, AgentType, GenerationID string
}
type ConfigResult struct {
	Status       string
	NeedsRestart bool
	Effective    object
	Drift        []object
}

func obj(v any) object {
	if m, ok := v.(map[string]any); ok && m != nil {
		return m
	}
	return object{}
}
func str(v any) string       { s, _ := v.(string); return s }
func nonempty(s string) bool { return strings.TrimSpace(s) != "" }
func integer(v any) (int64, bool) {
	switch n := v.(type) {
	case json.Number:
		i, err := n.Int64()
		return i, err == nil
	case int:
		return int64(n), true
	case int64:
		return n, true
	}
	return 0, false
}
func number(v any) int64 { n, _ := integer(v); return n }
func decodeObject(data []byte) (object, error) {
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	var v any
	if err := d.Decode(&v); err != nil {
		return nil, fmt.Errorf("invalid JSON object: %w", err)
	}
	m, ok := v.(map[string]any)
	if !ok || m == nil {
		return nil, errors.New("expected a JSON object")
	}
	if err := d.Decode(new(any)); err != io.EOF {
		return nil, errors.New("unexpected data after JSON object")
	}
	return m, nil
}
func toObject(v any) object { b, _ := json.Marshal(v); m, _ := decodeObject(b); return m }
func clone(m object) object {
	n := object{}
	for k, v := range m {
		n[k] = v
	}
	return n
}
func hash(data []byte) string { h := sha256.Sum256(data); return hex.EncodeToString(h[:]) }
func randomToken(prefix string) (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return prefix + base64.RawURLEncoding.EncodeToString(b), nil
}
func nowISO() string { return time.Now().UTC().Format(time.RFC3339Nano) }
func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// legacyJSON reproduces Python json.dumps(sort_keys=True, ensure_ascii=True).
// Credential keys, ledger scopes and batch IDs must survive a language upgrade.
func legacyJSON(v any) []byte {
	var out bytes.Buffer
	var write func(any)
	writeString := func(s string) {
		out.WriteByte('"')
		for _, r := range s {
			switch r {
			case '"', '\\':
				out.WriteByte('\\')
				out.WriteRune(r)
			case '\b':
				out.WriteString(`\b`)
			case '\f':
				out.WriteString(`\f`)
			case '\n':
				out.WriteString(`\n`)
			case '\r':
				out.WriteString(`\r`)
			case '\t':
				out.WriteString(`\t`)
			default:
				if r < 32 || r > 126 {
					if r > 0xffff {
						a, b := utf16.EncodeRune(r)
						fmt.Fprintf(&out, `\u%04x\u%04x`, a, b)
					} else {
						fmt.Fprintf(&out, `\u%04x`, r)
					}
				} else {
					out.WriteRune(r)
				}
			}
		}
		out.WriteByte('"')
	}
	write = func(v any) {
		switch x := v.(type) {
		case nil:
			out.WriteString("null")
		case string:
			writeString(x)
		case Host:
			writeString(string(x))
		case bool:
			out.WriteString(strconv.FormatBool(x))
		case int:
			out.WriteString(strconv.Itoa(x))
		case int64:
			out.WriteString(strconv.FormatInt(x, 10))
		case json.Number:
			out.WriteString(string(x))
		case object:
			keys := make([]string, 0, len(x))
			for k := range x {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			out.WriteByte('{')
			for i, k := range keys {
				if i > 0 {
					out.WriteString(", ")
				}
				writeString(k)
				out.WriteString(": ")
				write(x[k])
			}
			out.WriteByte('}')
		case []any:
			out.WriteByte('[')
			for i, item := range x {
				if i > 0 {
					out.WriteString(", ")
				}
				write(item)
			}
			out.WriteByte(']')
		case []object:
			a := make([]any, len(x))
			for i, item := range x {
				a[i] = item
			}
			write(a)
		case []string:
			a := make([]any, len(x))
			for i, item := range x {
				a[i] = item
			}
			write(a)
		default:
			b, err := json.Marshal(x)
			if err != nil {
				panic(err)
			}
			d := json.NewDecoder(bytes.NewReader(b))
			d.UseNumber()
			var y any
			if err := d.Decode(&y); err != nil {
				panic(err)
			}
			write(y)
		}
	}
	write(v)
	return out.Bytes()
}

func homePath(parts ...string) string {
	h, _ := os.UserHomeDir()
	return filepath.Join(append([]string{h}, parts...)...)
}
func expandPath(path string) string {
	if path == "~" {
		return homePath()
	}
	if strings.HasPrefix(path, "~/") || strings.HasPrefix(path, `~\`) {
		return homePath(path[2:])
	}
	return path
}
func resolvePath(path string) string {
	p, _ := filepath.Abs(expandPath(path))
	if real, err := filepath.EvalSymlinks(p); err == nil {
		return real
	}
	return p
}
func exists(path string) bool { _, err := os.Stat(path); return err == nil }
func isFile(path string) bool { s, e := os.Stat(path); return e == nil && s.Mode().IsRegular() }
