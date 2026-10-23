package runtime

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"

	"fmt"
	"github.com/google/shlex"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

func enrollmentContext(m Metadata) (EnrollmentContext, error) {
	w, err := workerURL()
	if err != nil {
		return EnrollmentContext{}, err
	}
	d, err := dashboardURL()
	if err != nil {
		return EnrollmentContext{}, err
	}
	response, err := requestJSON("GET", w+"/healthz", nil, nil)
	if err != nil {
		return EnrollmentContext{}, err
	}
	deployment := str(response["deployment_instance_id"])
	if !nonempty(deployment) {
		return EnrollmentContext{}, errors.New("worker health response missing deployment_instance_id")
	}
	var hostID string
	err = editState(func(s object) error {
		hostID = str(s["host_instance_id"])
		if !nonempty(hostID) {
			b := make([]byte, 16)
			if _, e := rand.Read(b); e != nil {
				return e
			}
			b[6] = (b[6] & 15) | 64
			b[8] = (b[8] & 63) | 128
			hostID = "host-" + hex.EncodeToString(b)
			s["host_instance_id"] = hostID
		}
		return nil
	})
	if err != nil {
		return EnrollmentContext{}, err
	}
	username := "unknown-user"
	if u, e := user.Current(); e == nil {
		username = u.Username
	}
	node, _ := os.Hostname()
	if node == "" {
		node = "unknown-host"
	}
	label := []rune(username + "@" + node)
	if len(label) > 240 {
		label = label[:240]
	}
	return EnrollmentContext{w, d, deployment, hostID, string(label), runtime.GOOS + "-" + runtime.GOARCH, m}, nil
}
func (c EnrollmentContext) cacheKey() string {
	return hash(legacyJSON(object{"deployment_instance_id": c.DeploymentID, "target": c.Metadata.Target, "worker_base_url": c.Worker}))
}
func cachedCredential(c EnrollmentContext) (*Credential, error) {
	s, err := loadState(statePath())
	if err != nil {
		return nil, err
	}
	value := obj(obj(s["credentials"])[c.cacheKey()])
	if str(value["value"]) == "" {
		return nil, nil
	}
	return &Credential{Value: str(value["value"]), ID: str(value["credential_id"]), DeploymentID: str(value["deployment_instance_id"]), Internal: value["is_internal_promptless_user"] == true}, nil
}
func forgetCredential(c EnrollmentContext) error {
	return editState(func(s object) error {
		creds := obj(s["credentials"])
		delete(creds, c.cacheKey())
		s["credentials"] = creds
		return nil
	})
}
func storeIdentity(c EnrollmentContext, credential *Credential) {
	if !credential.Internal {
		return
	}
	_ = editState(func(s object) error {
		creds := obj(s["credentials"])
		v := obj(creds[c.cacheKey()])
		if len(v) > 0 {
			v["is_internal_promptless_user"] = true
			creds[c.cacheKey()] = v
			s["credentials"] = creds
		}
		return nil
	})
}
func pendingSession(c EnrollmentContext) (*EnrollmentSession, error) {
	s, err := loadState(statePath())
	if err != nil {
		return nil, err
	}
	return sessionFromState(c, s)
}
func sessionFromState(c EnrollmentContext, s object) (*EnrollmentSession, error) {
	v := obj(obj(s["pending_enrollments"])[c.cacheKey()])
	if len(v) == 0 {
		return nil, nil
	}
	b, _ := json.Marshal(v)
	var session EnrollmentSession
	if json.Unmarshal(b, &session) != nil {
		return nil, nil
	}
	if !session.ExpiresAt.After(time.Now()) || session.ID == "" || session.DeviceCode == "" || session.DeploymentID != c.DeploymentID || session.PollURL == "" || session.PollInterval < 1 || session.PollInterval > 30 {
		return nil, nil
	}
	u, err := validateURL(session.PollURL, false)
	if err != nil {
		return nil, err
	}
	if err = validateTransport(u); err != nil {
		return nil, err
	}
	return &session, nil
}
func storeSession(c EnrollmentContext, session *EnrollmentSession) (*EnrollmentSession, error) {
	err := editState(func(s object) error {
		existing, err := sessionFromState(c, s)
		if err != nil {
			return err
		}
		if existing != nil {
			session = existing
			return nil
		}
		pending := obj(s["pending_enrollments"])
		session.Target = c.Metadata.Target
		session.Worker = c.Worker
		pending[c.cacheKey()] = toObject(session)
		s["pending_enrollments"] = pending
		return nil
	})
	return session, err
}
func forgetSession(c EnrollmentContext) error {
	return editState(func(s object) error {
		pending := obj(s["pending_enrollments"])
		delete(pending, c.cacheKey())
		s["pending_enrollments"] = pending
		return nil
	})
}
func storeCredential(c EnrollmentContext, credential *Credential) error {
	return editState(func(s object) error {
		creds := obj(s["credentials"])
		v := object{"value": credential.Value, "credential_id": nullable(credential.ID), "deployment_instance_id": nullable(credential.DeploymentID), "worker_base_url": c.Worker, "target": c.Metadata.Target, "updated_at": nowISO()}
		if credential.Internal {
			v["is_internal_promptless_user"] = true
		}
		creds[c.cacheKey()] = v
		s["credentials"] = creds
		pending := obj(s["pending_enrollments"])
		delete(pending, c.cacheKey())
		s["pending_enrollments"] = pending
		return nil
	})
}
func obtainCredential(c EnrollmentContext, out *output, quiet bool) (*Credential, string, error) {
	credential, err := cachedCredential(c)
	if credential != nil || err != nil {
		return credential, "", err
	}
	return enrollCredential(c, out, quiet)
}
func enrollCredential(c EnrollmentContext, out *output, quiet bool) (*Credential, string, error) {
	session, err := pendingSession(c)
	if err != nil {
		return nil, "", err
	}
	if session != nil {
		return pollEnrollment(c, *session)
	}
	l, err := acquireLock(filepath.Join(filepath.Dir(statePath()), "host-enrollment-"+c.cacheKey()+".enroll.lock"), time.Time{}, false)
	if err != nil {
		return nil, "", err
	}
	if l == nil {
		credential, err := cachedCredential(c)
		return credential, "enrollment_in_progress", err
	}
	defer l.close()
	credential, err := cachedCredential(c)
	if credential != nil || err != nil {
		return credential, "", err
	}
	session, err = pendingSession(c)
	if err != nil {
		return nil, "", err
	}
	if session == nil {
		out.emit(object{"status": "browser_enrollment_starting", "host": c.Metadata.Target}, quiet)
		var reason string
		session, reason, err = createEnrollment(c)
		if err != nil || session == nil {
			return nil, reason, err
		}
		session, err = storeSession(c, session)
		if err != nil {
			return nil, "", err
		}
	}
	return pollEnrollment(c, *session)
}
func pollEnrollment(c EnrollmentContext, s EnrollmentSession) (*Credential, string, error) {
	deadline := time.Now().Add(35 * time.Second)
	for {
		response, err := requestJSON("POST", s.PollURL, nil, object{"device_code": s.DeviceCode})
		if err != nil {
			return nil, "", err
		}
		switch str(response["status"]) {
		case "approved":
			value := str(response["host_credential"])
			if !nonempty(value) {
				return nil, "", errors.New("approved enrollment missing host_credential")
			}
			credential := &Credential{Value: value, ID: str(response["credential_id"]), DeploymentID: s.DeploymentID, Internal: internalIdentity(response)}
			if err = storeCredential(c, credential); err != nil {
				return nil, "", err
			}
			return credential, "", nil
		case "expired", "consumed":
			if err = forgetSession(c); err != nil {
				return nil, "", err
			}
			return nil, "approval_expired", nil
		case "pending":
			remaining := time.Until(deadline)
			if remaining <= 0 {
				return nil, "approval_pending", nil
			}
			time.Sleep(min(remaining, time.Duration(s.PollInterval)*time.Second))
		default:
			return nil, "", errors.New("host enrollment poll response has invalid status")
		}
	}
}

func createEnrollment(c EnrollmentContext) (*EnrollmentSession, string, error) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return nil, "", err
	}
	state, err := randomToken("")
	if err != nil {
		listener.Close()
		return nil, "", err
	}
	callback := "http://" + listener.Addr().String() + "/callback?" + url.Values{"state": {state}}.Encode()
	results := make(chan url.Values, 1)
	dashboard, _ := url.Parse(c.Dashboard)
	server := &http.Server{ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, MaxHeaderBytes: 16384}
	server.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.URL.Path != "/callback" {
			http.NotFound(w, r)
			return
		}
		q, err := url.ParseQuery(r.URL.RawQuery)
		if err != nil || len(q["state"]) != 1 || q.Get("state") != state {
			http.Error(w, "Promptless enrollment callback rejected.", http.StatusForbidden)
			return
		}
		q.Del("state")
		for k, values := range q {
			if len(values) != 1 {
				q.Del(k)
			}
		}
		if q.Get("status") == "pending" {
			approval, e := validateURL(q.Get("approval_url"), true)
			if e != nil || !sameOrigin(approval, dashboard) || approval.Path != "/pig/enroll" {
				q = url.Values{"status": {"error"}, "message": {"host enrollment approval URL did not match dashboard enrollment route"}}
				select {
				case results <- q:
				default:
				}
				http.Error(w, q.Get("message"), http.StatusBadRequest)
				return
			}
			select {
			case results <- q:
			default:
			}
			http.Redirect(w, r, approval.String(), http.StatusFound)
			return
		}
		select {
		case results <- q:
		default:
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, "Promptless enrollment received. You can return to your agent.")
	})
	go func() { _ = server.Serve(listener) }()
	defer server.Close()
	q := url.Values{"callback_url": {callback}, "deployment_instance_id": {c.DeploymentID}, "target": {string(c.Metadata.Target)}, "plugin_id": {c.Metadata.PluginID}, "plugin_version": {c.Metadata.PluginVersion}, "package_id": {c.Metadata.PackageID}, "bootstrap_version": {Version}, "toolchain_version": {c.Metadata.ToolchainVersion}, "host_instance_id": {c.HostID}, "host_label": {c.Label}, "host_platform": {c.Platform}, "pending_callback": {"1"}}
	ok, err := openBrowser(c.Dashboard + "/pig/enroll/start?" + q.Encode())
	if err != nil {
		return nil, "", err
	}
	if !ok {
		return nil, "browser_launch_failed", nil
	}
	select {
	case payload := <-results:
		s, err := sessionFromCallback(c, payload)
		return s, "", err
	case <-time.After(300 * time.Second):
		return nil, "browser_approval_timeout", nil
	}
}
func sessionFromCallback(c EnrollmentContext, p url.Values) (*EnrollmentSession, error) {
	if p.Get("status") == "error" {
		return nil, errors.New(redactText(p.Get("message")))
	}
	if p.Get("status") != "pending" && p.Get("status") != "approved" {
		return nil, errors.New("host enrollment callback had invalid status")
	}
	s := &EnrollmentSession{ID: p.Get("session_id"), DeploymentID: p.Get("deployment_instance_id"), DeviceCode: p.Get("device_code"), PollURL: p.Get("poll_url")}
	if !nonempty(s.ID) || !nonempty(s.DeviceCode) || !nonempty(s.PollURL) || s.DeploymentID != c.DeploymentID {
		return nil, errors.New("host enrollment callback missing required fields or mismatched worker identity")
	}
	var err error
	s.ExpiresAt, err = time.Parse(time.RFC3339Nano, p.Get("expires_at"))
	if err != nil {
		return nil, errors.New("host enrollment callback expires_at is invalid")
	}
	s.PollInterval, err = strconv.Atoi(p.Get("poll_interval_seconds"))
	if err != nil || s.PollInterval < 1 || s.PollInterval > 30 {
		return nil, errors.New("host enrollment poll interval must be between 1 and 30 seconds")
	}
	u, err := validateURL(s.PollURL, false)
	if err != nil {
		return nil, err
	}
	if err = validateTransport(u); err != nil {
		return nil, err
	}
	return s, nil
}
func openBrowser(endpoint string) (bool, error) {
	setting := os.Getenv("PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER")
	available := setting == "1" || (setting != "0" && (runtime.GOOS != "linux" || os.Getenv("DISPLAY") != "" || os.Getenv("WAYLAND_DISPLAY") != "" || os.Getenv("MIR_SOCKET") != "" || os.Getenv("WSL_INTEROP") != ""))
	if !available {
		u, _ := url.Parse(endpoint)
		if testURLs() && loopback(u) {
			client := http.Client{Timeout: 10 * time.Second}
			resp, err := client.Get(endpoint)
			if err != nil {
				return false, err
			}
			defer resp.Body.Close()
			if resp.StatusCode >= 400 {
				return false, fmt.Errorf("hosted enrollment start request failed with HTTP %d", resp.StatusCode)
			}
			_, err = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
			return err == nil, err
		}
		return false, nil
	}
	var cmd *exec.Cmd
	if browser := os.Getenv("BROWSER"); browser != "" {
		parts, err := shlex.Split(browser)
		if err != nil {
			return false, errors.New("invalid quoted BROWSER command")
		}
		if len(parts) > 0 {
			args := parts[1:]
			replaced := false
			for i, arg := range args {
				if strings.Contains(arg, "%s") {
					args[i] = strings.ReplaceAll(arg, "%s", endpoint)
					replaced = true
				}
			}
			if !replaced {
				args = append(args, endpoint)
			}
			cmd = exec.Command(parts[0], args...)
		}
	}
	if cmd == nil {
		switch runtime.GOOS {
		case "darwin":
			cmd = exec.Command("/usr/bin/open", endpoint)
		case "windows":
			cmd = exec.Command("rundll32.exe", "url.dll,FileProtocolHandler", endpoint)
		default:
			cmd = exec.Command("xdg-open", endpoint)
		}
	}
	if err := cmd.Start(); err != nil {
		return false, nil
	}
	go func() { _ = cmd.Wait() }()
	return true, nil
}
