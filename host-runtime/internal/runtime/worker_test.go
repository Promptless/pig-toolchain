package runtime

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

type recordedRequest struct {
	Method, Path, Authorization, Producer string
	Body                                  object
}

// This server implements the worker protocol, including acknowledgments built
// from the received ranges. It exercises production HTTP and persistence paths.
type testWorker struct {
	mu                   sync.Mutex
	server               *httptest.Server
	requests             []recordedRequest
	required             string
	rejectAck            bool
	dropPath, rejectPath string
	leaseOverrides       object
	onUpload             func(object)
}

func newTestWorker(t *testing.T, tls bool) *testWorker {
	t.Helper()
	w := &testWorker{leaseOverrides: object{}}
	handler := http.HandlerFunc(w.serve)
	if tls {
		w.server = httptest.NewTLSServer(handler)
		original := http.DefaultTransport
		http.DefaultTransport = w.server.Client().Transport
		t.Cleanup(func() { http.DefaultTransport = original })
	} else {
		w.server = httptest.NewServer(handler)
		t.Setenv("PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES", "1")
	}
	t.Cleanup(w.server.Close)
	return w
}
func (w *testWorker) serve(rw http.ResponseWriter, r *http.Request) {
	body := object{}
	if r.Method == "POST" {
		data, err := io.ReadAll(r.Body)
		if err != nil {
			rw.WriteHeader(400)
			return
		}
		body, err = decodeObject(data)
		if err != nil {
			rw.WriteHeader(400)
			return
		}
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.requests = append(w.requests, recordedRequest{r.Method, r.URL.Path, r.Header.Get("Authorization"), r.Header.Get("X-Promptless-Producer-Token"), body})
	if w.dropPath == r.URL.Path {
		w.dropPath = ""
		conn, _, err := rw.(http.Hijacker).Hijack()
		if err == nil {
			_ = conn.Close()
		}
		return
	}
	if w.rejectPath == r.URL.Path {
		rw.WriteHeader(403)
		return
	}
	response := object{}
	switch r.URL.Path {
	case "/healthz":
		response = object{"deployment_instance_id": "deployment"}
	case "/v0/host-enrollment/policy":
		response = object{"policy": object{"schema_version": 2, "policy_version": 1, "enabled_hosts": []string{"codex", "claude", "cursor"}, "expires_at": time.Now().Add(time.Hour).Format(time.RFC3339), "required_bootstrap_version": w.required}}
	case "/v0/host-enrollment/check-ins":
		response = object{"accepted": true, "policy_version": 1}
	case "/v0/cloud-enrollment/leases":
		response = object{"id": "lease", "grant_id": "grant", "host_instance_id": "host", "deployment_instance_id": "deployment", "execution_id": body["execution_id"], "target": body["target"], "provider": "test", "expires_at": time.Now().Add(time.Hour).Format(time.RFC3339)}
		for k, v := range w.leaseOverrides {
			response[k] = v
		}
	case "/v0/traces/batches":
		if w.onUpload != nil {
			w.onUpload(body)
		}
		response = ackRequest(body)
		if w.rejectAck {
			response["acknowledged_ranges"] = []object{}
		}
	default:
		rw.WriteHeader(404)
		return
	}
	_ = json.NewEncoder(rw).Encode(response)
}
func ackRequest(request object) object {
	ranges := []object{}
	raw, skipped := 0, 0
	chunks, _ := request["chunks"].([]any)
	for _, value := range chunks {
		chunk := obj(value)
		proof := object{}
		for _, k := range []string{"kind", "source_path_hash", "start_offset", "end_offset", "content_sha256"} {
			proof[k] = chunk[k]
		}
		ranges = append(ranges, proof)
		if chunk["kind"] == "oversized_record" {
			skipped++
		} else {
			raw++
		}
	}
	return object{"accepted": true, "batch_id": request["batch_id"], "policy_version": request["policy_version"], "raw_artifact_count": raw, "skipped_record_count": skipped, "unparsed_record_count": 0, "acknowledged_ranges": ranges}
}
func (w *testWorker) at(path string) []recordedRequest {
	w.mu.Lock()
	defer w.mu.Unlock()
	result := []recordedRequest{}
	for _, r := range w.requests {
		if path == "" || r.Path == path {
			result = append(result, r)
		}
	}
	return result
}
func (w *testWorker) change(f func()) { w.mu.Lock(); defer w.mu.Unlock(); f() }
func seedEnrollment(t *testing.T, w *testWorker, h Host) EnrollmentContext {
	t.Helper()
	t.Setenv("PROMPTLESS_WORKER_BASE_URL", w.server.URL)
	c := EnrollmentContext{Worker: w.server.URL, DeploymentID: "deployment", HostID: "host", Metadata: metadata("", h)}
	if err := writeState(statePath(), object{"host_instance_id": "host"}); err != nil {
		t.Fatal(err)
	}
	if err := storeCredential(c, &Credential{Value: "test-credential", ID: "credential", DeploymentID: "deployment"}); err != nil {
		t.Fatal(err)
	}
	return c
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type timeoutBody struct{}

func (timeoutBody) Read([]byte) (int, error) { return 0, os.ErrDeadlineExceeded }
func (timeoutBody) Close() error             { return nil }

func TestWorkerTimeoutIsActionable(t *testing.T) {
	for _, tc := range []struct{ method, path, operation string }{{"GET", "policy", "policy response"}, {"POST", "check-ins", "check-in response"}} {
		for _, stage := range []string{"connect", "read"} {
			t.Run(tc.method+"/"+stage, func(t *testing.T) {
				original := http.DefaultTransport
				http.DefaultTransport = roundTripFunc(func(r *http.Request) (*http.Response, error) {
					if stage == "read" {
						return &http.Response{StatusCode: 200, Body: timeoutBody{}, Header: http.Header{}}, nil
					}
					return nil, os.ErrDeadlineExceeded
				})
				t.Cleanup(func() { http.DefaultTransport = original })
				_, err := requestJSON(tc.method, "https://worker.test/v0/host-enrollment/"+tc.path, &Credential{Value: "credential"}, object{})
				if err == nil || !errors.Is(err, os.ErrDeadlineExceeded) {
					t.Fatalf("timeout: %v", err)
				}
				for _, fragment := range []string{"within 10 seconds", tc.operation, "Retry shortly", "worker's health"} {
					if !strings.Contains(err.Error(), fragment) {
						t.Errorf("missing %q in %v", fragment, err)
					}
				}
				if msg := userMessage(object{"status": "error", "host": Claude, "message": err.Error()}); !strings.HasPrefix(msg, "Promptless host enrollment failed for Claude Code: ") {
					t.Fatal(msg)
				}
			})
		}
	}
}
func TestWorkerResponseLimits(t *testing.T) {
	for _, status := range []int{200, 409} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			limit := 65536
			if status == 200 {
				limit = 16 << 20
			}
			stream := strings.NewReader(strings.Repeat("x", limit+2))
			original := http.DefaultTransport
			http.DefaultTransport = roundTripFunc(func(r *http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: status, Body: io.NopCloser(stream), Header: http.Header{}}, nil
			})
			t.Cleanup(func() { http.DefaultTransport = original })
			_, err := requestJSON("GET", "https://worker.test/response", nil, nil)
			if err == nil {
				t.Fatal("oversized response accepted")
			}
			if stream.Len() != 1 {
				t.Fatalf("response read was not bounded: %d bytes remain", stream.Len())
			}
			var responseErr *HTTPError
			if status == 409 && (!errors.As(err, &responseErr) || len(responseErr.Body) != 0) {
				t.Fatalf("truncated body retained: %v", err)
			}
		})
	}
}
