package runtime

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type HTTPError struct {
	Status int
	Body   []byte
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("worker request failed with HTTP %d", e.Status)
}
func authError(err error) bool {
	var e *HTTPError
	return errors.As(err, &e) && (e.Status == 401 || e.Status == 403)
}
func testURLs() bool { return os.Getenv("PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES") == "1" }
func loopback(u *url.URL) bool {
	return u.Hostname() == "localhost" || u.Hostname() == "127.0.0.1" || u.Hostname() == "::1"
}
func validateURL(raw string, query bool) (*url.URL, error) {
	u, err := url.Parse(raw)
	if err != nil || u == nil || u.Hostname() == "" || u.User != nil || u.Fragment != "" || (!query && u.RawQuery != "") || (u.Scheme != "https" && u.Scheme != "http") || strings.ContainsAny(raw, "\\\r\n\t ") {
		return nil, errors.New("invalid HTTP URL")
	}
	if p := u.Port(); p != "" {
		n, e := strconv.Atoi(p)
		if e != nil || n < 1 || n > 65535 {
			return nil, errors.New("invalid HTTP URL port")
		}
	}
	return u, nil
}
func validateTransport(u *url.URL) error {
	if u.Scheme != "https" && !(testURLs() && loopback(u)) {
		return errors.New("worker base URL must use HTTPS unless explicitly testing a loopback worker")
	}
	return nil
}
func baseURL(env, def string) (string, error) {
	raw := os.Getenv(env)
	if raw == "" {
		raw = def
	}
	u, err := validateURL(raw, false)
	if err != nil {
		return "", err
	}
	if u.Path != "" && u.Path != "/" {
		return "", errors.New("base URL must be an origin")
	}
	if err = validateTransport(u); err != nil {
		return "", err
	}
	return strings.TrimRight(raw, "/"), nil
}
func workerURL() (string, error) {
	return baseURL("PROMPTLESS_WORKER_BASE_URL", "https://pig.promptless.ai")
}
func dashboardURL() (string, error) {
	return baseURL("PROMPTLESS_DASHBOARD_BASE_URL", "https://app.gopromptless.ai")
}
func sameOrigin(a, b *url.URL) bool {
	port := func(u *url.URL) string {
		if p := u.Port(); p != "" {
			return p
		}
		if u.Scheme == "https" {
			return "443"
		}
		return "80"
	}
	return strings.EqualFold(a.Scheme, b.Scheme) && strings.EqualFold(a.Hostname(), b.Hostname()) && port(a) == port(b)
}

func requestJSON(method, endpoint string, credential *Credential, body any) (object, error) {
	u, err := validateURL(endpoint, true)
	if err != nil {
		return nil, err
	}
	if err = validateTransport(u); err != nil {
		return nil, err
	}
	cloud := credential != nil && (credential.ProducerToken != "" || strings.HasPrefix(credential.Value, "plicg_"))
	if cloud && u.Scheme != "https" {
		return nil, errors.New("cloud credentials require HTTPS")
	}
	var data []byte
	if body != nil {
		data = legacyJSON(body)
	}
	req, err := http.NewRequest(method, endpoint, bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if credential != nil {
		if credential.Value != "" {
			req.Header.Set("Authorization", "Bearer "+credential.Value)
		}
		if credential.ProducerToken != "" {
			req.Header.Set("X-Promptless-Producer-Token", credential.ProducerToken)
		}
	}
	client := &http.Client{Timeout: 10 * time.Second, CheckRedirect: func(next *http.Request, via []*http.Request) error {
		if cloud {
			return errors.New("cloud worker redirects are forbidden")
		}
		if len(via) >= 10 {
			return errors.New("too many redirects")
		}
		if !sameOrigin(next.URL, via[0].URL) {
			return errors.New("worker redirect changed origin")
		}
		return validateTransport(next.URL)
	}}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("worker request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 65536))
		return nil, &HTTPError{resp.StatusCode, b}
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, 16<<20+1))
	if err != nil {
		return nil, err
	}
	if len(b) > 16<<20 {
		return nil, errors.New("worker response exceeds size limit")
	}
	return decodeObject(b)
}

var signaturePattern = regexp.MustCompile(`^[A-Za-z0-9._-]+:.+$`)
var semverPattern = regexp.MustCompile(`^(\d+)\.(\d+)\.(\d+)`)

func requiresNewer(required string) bool {
	if required == "" {
		return false
	}
	a, b := semverPattern.FindStringSubmatch(required), semverPattern.FindStringSubmatch(Version)
	if a == nil || b == nil {
		return required != Version
	}
	for i := 1; i <= 3; i++ {
		x, _ := strconv.Atoi(a[i])
		y, _ := strconv.Atoi(b[i])
		if x != y {
			return x > y
		}
	}
	return false
}
func validatePolicy(payload object, host Host) (Policy, error) {
	p := obj(payload["policy"])
	if !signaturePattern.MatchString(str(payload["signature"])) {
		return Policy{}, errors.New("policy signature is missing or malformed")
	}
	if n, ok := integer(p["schema_version"]); !ok || n != 1 {
		return Policy{}, errors.New("unsupported policy schema")
	}
	exp, err := time.Parse(time.RFC3339Nano, str(p["expires_at"]))
	if err != nil || !exp.After(time.Now()) {
		return Policy{}, errors.New("policy expired or has invalid expiration")
	}
	enabled := false
	if hosts, ok := p["enabled_hosts"].([]any); ok {
		for _, h := range hosts {
			if str(h) == string(host) {
				enabled = true
			}
		}
	}
	if !enabled {
		return Policy{}, errors.New("policy does not enable this host")
	}
	v, ok := integer(p["policy_version"])
	if !ok {
		return Policy{}, errors.New("policy_version must be an integer")
	}
	if x := p["required_bootstrap_version"]; x != nil {
		if _, ok := x.(string); !ok {
			return Policy{}, errors.New("required_bootstrap_version must be a string")
		}
	}
	// The deployed contract authenticates the envelope over HTTPS. It has no
	// published trust key; checking signature syntax is not signature verification.
	return Policy{v, str(p["required_bootstrap_version"])}, nil
}
func internalIdentity(payload object) bool {
	if payload["is_internal_promptless_user"] == true {
		return true
	}
	emails := []string{str(payload["user_email"]), str(payload["email"]), str(payload["account_email"])}
	for _, k := range []string{"user", "identity", "account"} {
		m := obj(payload[k])
		emails = append(emails, str(m["email"]), str(m["user_email"]))
	}
	for _, email := range emails {
		s := strings.ToLower(strings.TrimSpace(email))
		if len(s) <= 254 && strings.HasSuffix(s, "@gopromptless.ai") && len(s) > len("@gopromptless.ai") && !strings.ContainsAny(s, " \r\n\t") {
			return true
		}
	}
	return false
}
func checkIn(endpoint string, c *Credential, h Host, m Metadata, p Policy, r ConfigResult) error {
	payload := object{"host": h, "plugin_version": m.PluginVersion, "policy_version": p.Version, "status": r.Status, "needs_restart": r.NeedsRestart, "effective_config": r.Effective, "drift_reports": redact(r.Drift), "checked_at": nowISO(), "bootstrap_version": m.BootstrapVersion}
	response, err := requestJSON("POST", endpoint, c, payload)
	if err != nil {
		return err
	}
	v, ok := integer(response["policy_version"])
	if response["accepted"] != true {
		return errors.New("check-in response was not accepted")
	}
	if !ok || v != p.Version {
		return errors.New("check-in response policy version did not match the applied policy")
	}
	return nil
}
func pluginRoot() string {
	for _, key := range []string{"CURSOR_PLUGIN_ROOT", "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"} {
		if value := os.Getenv(key); nonempty(value) {
			return expandPath(value)
		}
	}
	return ""
}
func metadata(root string, h Host) Metadata {
	m := Metadata{Version, "unknown", "unknown", "unknown", "unknown", "unknown", h}
	if root == "" {
		return m
	}
	doc, err := loadState(filepath.Join(root, "hub.managed-runtimes.json"))
	if err != nil {
		return m
	}
	items, _ := doc["managed_runtimes"].([]any)
	for _, item := range items {
		v := obj(item)
		if v["id"] != "host-runtime" || v["status"] != "included" {
			continue
		}
		for key, dest := range map[string]*string{"toolchain_version": &m.ToolchainVersion, "plugin_id": &m.PluginID, "plugin_name": &m.PluginName, "plugin_version": &m.PluginVersion, "package_id": &m.PackageID} {
			if s := str(v[key]); s != "" {
				*dest = s
			}
		}
		break
	}
	return m
}
func runtimeInfo() object {
	exe, _ := os.Executable()
	b, _ := os.ReadFile(exe)
	return object{"id": "host-runtime", "name": executableName, "version": Version, "channel": "stable", "sha256": hash(b)}
}
