package runtime

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type deviceAPI struct {
	response                           object
	status, creates, polls, unexpected int
	approved                           bool
	redirect                           string
}

func setupDeviceAPI(t *testing.T) (*deviceAPI, EnrollmentContext, *testWorker) {
	t.Helper()
	privateHome(t)
	w := newTestWorker(t, true)
	t.Setenv("PROMPTLESS_WORKER_BASE_URL", w.server.URL)
	t.Setenv("PROMPTLESS_DASHBOARD_BASE_URL", "https://dashboard.test")
	t.Setenv("PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER", "0")
	t.Setenv("BROWSER", "/nonexistent-browser-device-test")
	api := &deviceAPI{response: object{"session_id": "90000000-0000-4000-8000-000000000001", "device_code": "private-device-proof", "approval_url": "https://dashboard.test/pig/enroll?token=approval", "expires_at": time.Now().Add(15 * time.Minute).Format(time.RFC3339Nano), "poll_interval_seconds": 1}}
	server := httptest.NewTLSServer(http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/device-sessions"):
			api.creates++
			if api.status != 0 {
				rw.WriteHeader(api.status)
				return
			}
			if api.redirect == "create" {
				http.Redirect(rw, r, "/unexpected", 307)
				return
			}
			_ = json.NewEncoder(rw).Encode(api.response)
		case strings.HasSuffix(r.URL.Path, "/poll"):
			api.polls++
			if api.redirect == "poll" {
				http.Redirect(rw, r, "/unexpected", 307)
				return
			}
			var body object
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || str(body["device_code"]) != "private-device-proof" {
				t.Error("invalid proof exchange")
			}
			response := object{"status": "pending"}
			if api.approved {
				response = object{"status": "approved", "host_credential": "approved-private-credential", "credential_id": "id"}
			}
			_ = json.NewEncoder(rw).Encode(response)
		default:
			api.unexpected++
			rw.WriteHeader(404)
		}
	}))
	t.Cleanup(server.Close)
	t.Setenv("PROMPTLESS_HOSTED_API_BASE_URL", server.URL)
	c, err := enrollmentContext(metadata("", Codex))
	if err != nil {
		t.Fatal(err)
	}
	return api, c, w
}
func TestDevicePendingResumeAndSharedCredential(t *testing.T) {
	api, c, w := setupDeviceAPI(t)
	s, err := createDeviceEnrollment(c)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = storeSession(c, s); err != nil {
		t.Fatal(err)
	}
	if credential, reason, err := pollEnrollmentUntil(c, *s, time.Now()); err != nil || credential != nil || reason != "approval_pending" {
		t.Fatalf("pending: %v %s %v", credential, reason, err)
	}
	resumed, err := pendingSession(c)
	if err != nil || resumed == nil || !resumed.ExpiresAt.Equal(s.ExpiresAt) {
		t.Fatalf("resume: %v %v", resumed, err)
	}
	api.approved = true
	var stdout, stderr bytes.Buffer
	out := &output{stdout: &stdout, stderr: &stderr}
	response, err := runEnrollMode(Codex, out, true)
	if err != nil || response["status"] != "enrolled" {
		t.Fatalf("enroll: %v %v", response, err)
	}
	if !strings.Contains(stderr.String(), s.ApprovalURL) || strings.Contains(stderr.String()+stdout.String(), s.DeviceCode) || strings.Contains(stderr.String()+stdout.String(), "approved-private-credential") {
		t.Fatal("unsafe or missing output")
	}
	if api.creates != 1 || api.polls != 2 {
		t.Fatalf("requests: %+v", api)
	}
	if err := ensure(Codex, true, false, false, &output{stdout: io.Discard, stderr: io.Discard}); err != nil {
		t.Fatal(err)
	}
	if len(w.at("/v0/host-enrollment/check-ins")) != 1 {
		t.Fatal("device credential not reused by ensure")
	}
	if pending, err := pendingSession(c); err != nil || pending != nil {
		t.Fatalf("pending survived approval: %v", err)
	}
}
func TestDeviceSessionValidationAndNoRedirects(t *testing.T) {
	for _, field := range []string{"session_id", "device_code", "approval_url", "expires_at", "poll_interval_seconds"} {
		t.Run(field, func(t *testing.T) {
			api, c, _ := setupDeviceAPI(t)
			api.response[field] = "invalid"
			if field == "device_code" {
				api.response[field] = ""
			}
			if _, err := createDeviceEnrollment(c); err == nil {
				t.Fatal("accepted invalid response")
			}
			if pending, err := pendingSession(c); pending != nil || err != nil {
				t.Fatal("invalid response persisted")
			}
		})
	}
	for _, route := range []string{"create", "poll"} {
		t.Run(route, func(t *testing.T) {
			api, c, _ := setupDeviceAPI(t)
			s, err := createDeviceEnrollment(c)
			if err != nil {
				t.Fatal(err)
			}
			api.redirect = route
			if route == "create" {
				_, err = createDeviceEnrollment(c)
			} else {
				_, _, err = pollEnrollment(c, *s)
			}
			if err == nil || api.unexpected != 0 {
				t.Fatalf("followed redirect: %v %+v", err, api)
			}
		})
	}
	for _, status := range []int{404, 405, 429} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			api, c, _ := setupDeviceAPI(t)
			api.status = status
			_, err := createDeviceEnrollment(c)
			expected := "deploy the device-enrollment API"
			if status == 429 {
				expected = "wait before retrying"
			}
			if err == nil || !strings.Contains(err.Error(), expected) {
				t.Fatal(err)
			}
		})
	}
}
func TestDeviceResumePinsDestinationsAndRejectsMalformedState(t *testing.T) {
	for _, change := range []string{"api", "dashboard", "poll", "malformed"} {
		t.Run(change, func(t *testing.T) {
			api, c, _ := setupDeviceAPI(t)
			s, err := createDeviceEnrollment(c)
			if err != nil {
				t.Fatal(err)
			}
			if _, err = storeSession(c, s); err != nil {
				t.Fatal(err)
			}
			switch change {
			case "api":
				t.Setenv("PROMPTLESS_HOSTED_API_BASE_URL", "https://changed.test")
			case "dashboard":
				c.Dashboard = "https://changed.test"
			default:
				err = editState(func(state object) error {
					pending := obj(obj(state["pending_enrollments"])[c.cacheKey()])
					if change == "poll" {
						pending["poll_url"] = "https://changed.test/proof"
					} else {
						pending["hosted_api_base_url"] = false
					}
					return nil
				})
				if err != nil {
					t.Fatal(err)
				}
			}
			if _, err = pendingSession(c); err == nil {
				t.Fatal("accepted changed destination")
			}
			if api.polls != 0 {
				t.Fatal("sent proof")
			}
		})
	}
}
func TestEnrollmentLockAlsoProtectsPendingExchange(t *testing.T) {
	api, c, _ := setupDeviceAPI(t)
	s, err := createDeviceEnrollment(c)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = storeSession(c, s); err != nil {
		t.Fatal(err)
	}
	lock, err := acquireLock(filepath.Join(filepath.Dir(statePath()), "host-enrollment-"+c.cacheKey()+".enroll.lock"), time.Time{}, false)
	if err != nil || lock == nil {
		t.Fatal(err)
	}
	defer lock.close()
	for _, device := range []bool{false, true} {
		credential, reason, err := enrollMode(c, &output{stdout: io.Discard, stderr: io.Discard}, true, device)
		if err != nil || credential != nil || reason != "enrollment_in_progress" {
			t.Fatalf("lock: %v %s %v", credential, reason, err)
		}
	}
	if api.polls != 0 || api.creates != 1 {
		t.Fatal("follower initiated exchange")
	}
}
func TestDeviceExpiryAndApprovalControls(t *testing.T) {
	api, c, _ := setupDeviceAPI(t)
	s, err := createDeviceEnrollment(c)
	if err != nil {
		t.Fatal(err)
	}
	s.ExpiresAt = time.Now().Add(-time.Second)
	if _, reason, err := pollEnrollment(c, *s); err != nil || reason != "approval_expired" || api.polls != 0 {
		t.Fatalf("expired: %s %v", reason, err)
	}
	for _, raw := range []string{"https://evil.test/pig/enroll", "https://dashboard.test/other", "https://dashboard.test/pig/enroll?token=abc\x1b", "https://dashboard.test/pig/enroll?token=abc\u0085"} {
		if err := validateApprovalURL(raw, c.Dashboard); err == nil {
			t.Fatal("accepted unsafe approval URL")
		}
	}
	if _, _, err := parseCommand([]string{"ensure", "--device"}, io.Discard); err == nil {
		t.Fatal("device flag leaked into hooks")
	}
}

type inspectWriter func([]byte) (int, error)

func (f inspectWriter) Write(p []byte) (int, error) { return f(p) }
func TestDeviceCommandPersistsBeforeDisplayingApproval(t *testing.T) {
	api, c, _ := setupDeviceAPI(t)
	api.approved = true
	var stdout bytes.Buffer
	displayed := false
	stderr := inspectWriter(func(p []byte) (int, error) {
		if strings.Contains(string(p), "Open this link") {
			pending, err := pendingSession(c)
			if err != nil || pending == nil || !strings.Contains(string(p), pending.ApprovalURL) {
				t.Fatalf("displayed before persistence: %v", err)
			}
			displayed = true
		}
		return len(p), nil
	})
	if code := Main([]string{"enroll", "--host", "codex", "--device"}, strings.NewReader(""), &stdout, stderr); code != 0 {
		t.Fatalf("exit %d: %s", code, stdout.String())
	}
	if !displayed || !strings.Contains(stdout.String(), `"status":"enrolled"`) || strings.Contains(stdout.String(), "private") {
		t.Fatal(stdout.String())
	}
}
