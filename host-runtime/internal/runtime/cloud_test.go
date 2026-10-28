package runtime

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	goruntime "runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const testGrant = "plicg_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

type cloudFixture struct {
	config, main, root string
	event              object
	worker             *testWorker
}

func newCloudFixture(t *testing.T) cloudFixture {
	t.Helper()
	home := privateHome(t)
	w := newTestWorker(t, true)
	t.Setenv("PROMPTLESS_CLOUD_GRANT", testGrant)
	t.Setenv("PROMPTLESS_CLOUD_GRANT_FILE", "")
	config := filepath.Join(home, "cloud.json")
	if err := writeState(config, object{"worker_url": w.server.URL, "provider": "test", "target": "claude", "transport": "secret", "integration_id": "test-integration"}); err != nil {
		t.Fatal(err)
	}
	main := filepath.Join(home, "session.jsonl")
	mustWrite(t, main, []byte("{\"type\":\"user\",\"message\":\"hello\"}\n"))
	event := object{"session_id": "root-session", "transcript_path": main, "hook_event_name": "Stop"}
	c, err := loadCloudConfig(config)
	if err != nil {
		t.Fatal(err)
	}
	root, _, _, err := executionScope(c, event)
	if err != nil {
		t.Fatal(err)
	}
	return cloudFixture{config, main, root, event, w}
}
func (f cloudFixture) collect() (object, error) { return collectCloud(f.config, f.event, 0) }
func (f cloudFixture) assertNoLedger(t *testing.T) {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join(f.root, "ledger.*.json"))
	if err != nil || len(paths) != 0 {
		t.Fatalf("unexpected acknowledged state: %v %v", paths, err)
	}
}
func TestCloudPolicyVersionGate(t *testing.T) {
	for _, host := range []Host{Claude, Codex} {
		for _, version := range []string{"", "0.0.1", Version, "999.0.0"} {
			t.Run(string(host)+"/"+version, func(t *testing.T) {
				f := newCloudFixture(t)
				config, _ := loadState(f.config)
				config["target"] = host
				_ = writeState(f.config, config)
				f.worker.change(func() { f.worker.required = version })
				status, err := f.collect()
				if err != nil {
					t.Fatal(err)
				}
				uploads := len(f.worker.at("/v0/traces/batches"))
				if version == "999.0.0" {
					if status["status"] != "blocked" || status["reason"] != "bootstrap_upgrade_required" || uploads != 0 {
						t.Fatal(status, uploads)
					}
				} else if status["status"] != "drained" || uploads != 1 {
					t.Fatal(status, uploads)
				}
				c, _ := loadCloudConfig(f.config)
				root, _, _, _ := executionScope(c, f.event)
				saved, _ := loadState(filepath.Join(root, "status.json"))
				if string(legacyJSON(saved)) != string(legacyJSON(status)) {
					t.Fatal("status not persisted")
				}
			})
		}
	}
}
func TestCloudSourcesStayWithinExecution(t *testing.T) {
	root := resolvePath(t.TempDir())
	main := filepath.Join(root, "session.jsonl")
	neighbor := filepath.Join(root, "neighbor.jsonl")
	children := filepath.Join(root, "session", "subagents")
	if err := os.MkdirAll(filepath.Join(children, "nested"), 0700); err != nil {
		t.Fatal(err)
	}
	child := filepath.Join(children, "agent-child.jsonl")
	for _, p := range []string{main, neighbor, child, filepath.Join(children, "unrelated.jsonl"), filepath.Join(children, "nested", "agent-hidden.jsonl")} {
		mustWrite(t, p, []byte("{}\n"))
	}
	symlink(t, neighbor, filepath.Join(children, "agent-link.jsonl"))
	if got := cloudSources(main, Claude); !reflect.DeepEqual(got, []string{main, child}) {
		t.Fatal(got)
	}
	if got := cloudSources(main, Codex); !reflect.DeepEqual(got, []string{main}) {
		t.Fatal(got)
	}
	other := filepath.Join(root, "other.jsonl")
	mustWrite(t, other, []byte("{}\n"))
	_ = os.Mkdir(filepath.Join(root, "other"), 0700)
	symlink(t, children, filepath.Join(root, "other", "subagents"))
	if got := cloudSources(other, Claude); !reflect.DeepEqual(got, []string{other}) {
		t.Fatal(got)
	}
}
func TestCloudLostLeaseResponseKeepsPrivateProducer(t *testing.T) {
	f := newCloudFixture(t)
	f.worker.change(func() { f.worker.dropPath = "/v0/cloud-enrollment/leases" })
	if _, err := f.collect(); err == nil {
		t.Fatal("lost response accepted")
	}
	path := filepath.Join(f.root, "producer.json")
	before, err := loadState(path)
	if err != nil || len(before) != 2 {
		t.Fatal(before, err)
	}
	if goruntime.GOOS != "windows" {
		for p, mode := range map[string]os.FileMode{path: 0600, f.root: 0700} {
			info, err := os.Stat(p)
			if err != nil || info.Mode().Perm() != mode {
				t.Fatalf("private mode: %s %v %v", p, info, err)
			}
		}
	}
	if status, err := f.collect(); err != nil || status["status"] != "drained" {
		t.Fatal(status, err)
	}
	requests := f.worker.at("/v0/cloud-enrollment/leases")
	if len(requests) != 2 || requests[0].Producer != before["producer_token"] || requests[0].Producer != requests[1].Producer || !reflect.DeepEqual(requests[0].Body, requests[1].Body) {
		t.Fatal("producer or lease request changed across retry")
	}
	producer, _ := os.ReadFile(path)
	status, _ := os.ReadFile(filepath.Join(f.root, "status.json"))
	if bytes.Contains(producer, []byte(testGrant)) || bytes.Contains(status, []byte(str(before["producer_token"]))) {
		t.Fatal("secret persisted in wrong state")
	}
}
func TestCloudRenewalOnlyUploadsNewRecords(t *testing.T) {
	f := newCloudFixture(t)
	for i, want := range []int{1, 0, 1} {
		if i == 2 {
			file, err := os.OpenFile(f.main, os.O_APPEND|os.O_WRONLY, 0600)
			if err != nil {
				t.Fatal(err)
			}
			_, err = file.WriteString("{\"type\":\"assistant\"}\n")
			_ = file.Close()
			if err != nil {
				t.Fatal(err)
			}
		}
		status, err := f.collect()
		if err != nil || number(status["uploaded_batches"]) != int64(want) {
			t.Fatal(status, err)
		}
	}
	uploads := f.worker.at("/v0/traces/batches")
	if len(uploads) != 2 {
		t.Fatal(len(uploads))
	}
	first := obj(uploads[0].Body["chunks"].([]any)[0])
	second := obj(uploads[1].Body["chunks"].([]any)[0])
	if first["end_offset"] != second["start_offset"] {
		t.Fatal(first, second)
	}
	producers := map[string]bool{}
	for _, r := range f.worker.at("") {
		producers[r.Producer] = true
	}
	if len(producers) != 1 {
		t.Fatal("producer changed on renewal")
	}
}
func TestCloudFailedAcknowledgmentRetriesIdenticalBatch(t *testing.T) {
	for _, failure := range []string{"lost_response", "invalid_ack"} {
		t.Run(failure, func(t *testing.T) {
			f := newCloudFixture(t)
			f.worker.change(func() {
				if failure == "lost_response" {
					f.worker.dropPath = "/v0/traces/batches"
				} else {
					f.worker.rejectAck = true
				}
			})
			if _, err := f.collect(); err == nil {
				t.Fatal("bad acknowledgment accepted")
			}
			f.assertNoLedger(t)
			f.worker.change(func() { f.worker.rejectAck = false })
			if status, err := f.collect(); err != nil || number(status["uploaded_batches"]) != 1 {
				t.Fatal(status, err)
			}
			uploads := f.worker.at("/v0/traces/batches")
			if len(uploads) != 2 || uploads[0].Body["batch_id"] != uploads[1].Body["batch_id"] || !reflect.DeepEqual(uploads[0].Body["chunks"], uploads[1].Body["chunks"]) {
				t.Fatal("retry changed captured range identity")
			}
			if status, err := f.collect(); err != nil || number(status["uploaded_batches"]) != 0 {
				t.Fatal(status, err)
			}
		})
	}
}
func TestCloudConcurrentCaptureSerializesAcknowledgments(t *testing.T) {
	f := newCloudFixture(t)
	var wg sync.WaitGroup
	outcomes := make(chan object, 2)
	failures := make(chan error, 2)
	for range 2 {
		wg.Add(1)
		go func() { defer wg.Done(); status, err := f.collect(); outcomes <- status; failures <- err }()
	}
	wg.Wait()
	close(outcomes)
	close(failures)
	for err := range failures {
		if err != nil {
			t.Fatal(err)
		}
	}
	total := 0
	for status := range outcomes {
		total += int(number(status["uploaded_batches"]))
	}
	if total != 1 || len(f.worker.at("/v0/traces/batches")) != 1 {
		t.Fatal("concurrent hooks duplicated a range")
	}
}
func TestCloudRevokedAuthorizationNeverAdvancesLedger(t *testing.T) {
	for _, path := range []string{"/v0/cloud-enrollment/leases", "/v0/host-enrollment/policy", "/v0/traces/batches"} {
		t.Run(path, func(t *testing.T) {
			f := newCloudFixture(t)
			f.worker.change(func() { f.worker.rejectPath = path })
			if _, err := f.collect(); !authError(err) {
				t.Fatalf("expected authorization error: %v", err)
			}
			f.assertNoLedger(t)
			status, _ := loadState(filepath.Join(f.root, "status.json"))
			if status["status"] != "partial" || status["error_type"] != "*runtime.HTTPError" || strings.Contains(string(legacyJSON(status)), testGrant) {
				t.Fatal(status)
			}
			requests := f.worker.at("")
			if requests[len(requests)-1].Path != path {
				t.Fatal("continued after revocation")
			}
		})
	}
}
func TestCloudGrantTransports(t *testing.T) {
	for _, transport := range []string{"secret", "proxy"} {
		t.Run(transport, func(t *testing.T) {
			f := newCloudFixture(t)
			config, _ := loadState(f.config)
			config["transport"] = transport
			_ = writeState(f.config, config)
			authorization := "Bearer " + testGrant
			if transport == "proxy" {
				t.Setenv("PROMPTLESS_CLOUD_GRANT", "")
				authorization = ""
			}
			if _, err := f.collect(); err != nil {
				t.Fatal(err)
			}
			requests := f.worker.at("")
			if len(requests) != 3 {
				t.Fatal(len(requests))
			}
			for _, r := range requests {
				if r.Authorization != authorization || !cloudProducer.MatchString(r.Producer) {
					t.Fatal("incorrect credential headers")
				}
			}
		})
	}
	f := newCloudFixture(t)
	c, _ := loadCloudConfig(f.config)
	c.Transport = "proxy"
	if _, err := grantSecret(c); err == nil {
		t.Fatal("proxy accepted sandbox secret")
	}
	c.Transport = "secret"
	file := filepath.Join(t.TempDir(), "grant")
	mustWrite(t, file, []byte(testGrant+"\n"))
	t.Setenv("PROMPTLESS_CLOUD_GRANT_FILE", file)
	if _, err := grantSecret(c); err == nil {
		t.Fatal("accepted two secret sources")
	}
	t.Setenv("PROMPTLESS_CLOUD_GRANT", "")
	if got, err := grantSecret(c); err != nil || got != testGrant {
		t.Fatal("file secret failed", err)
	}
	t.Setenv("PROMPTLESS_CLOUD_GRANT_FILE", "")
	if _, err := grantSecret(c); err == nil {
		t.Fatal("accepted missing secret")
	}
}
func TestCloudRejectsUnsafeOriginsAndLeaseScopes(t *testing.T) {
	for _, url := range []string{"http://example.test", "https://user:password@example.test", "https://example.test/path", "https://example.test?token=secret", "https://example.test#fragment"} {
		t.Run(url, func(t *testing.T) {
			f := newCloudFixture(t)
			config, _ := loadState(f.config)
			config["worker_url"] = url
			_ = writeState(f.config, config)
			if _, err := loadCloudConfig(f.config); err == nil {
				t.Fatal("unsafe origin accepted")
			}
		})
	}
	for key, value := range (object{"execution_id": "other", "target": "codex", "provider": "other", "expires_at": "2000-01-01T00:00:00Z", "id": "", "grant_id": "", "host_instance_id": "", "deployment_instance_id": ""}) {
		t.Run(key, func(t *testing.T) {
			f := newCloudFixture(t)
			f.worker.change(func() { f.worker.leaseOverrides[key] = value })
			if _, err := f.collect(); err == nil {
				t.Fatal("invalid lease accepted")
			}
			if len(f.worker.at("")) != 1 {
				t.Fatal("continued past invalid lease")
			}
			f.assertNoLedger(t)
		})
	}
	for _, key := range []string{"id", "grant_id", "host_instance_id", "deployment_instance_id"} {
		t.Run("renewal/"+key, func(t *testing.T) {
			f := newCloudFixture(t)
			if _, err := f.collect(); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(f.root, "producer.json")
			before, _ := os.ReadFile(path)
			f.worker.change(func() { f.worker.leaseOverrides[key] = "different" })
			if _, err := f.collect(); err == nil {
				t.Fatal("identity changed silently")
			}
			after, _ := os.ReadFile(path)
			if !bytes.Equal(before, after) || len(f.worker.at("/v0/traces/batches")) != 1 {
				t.Fatal("invalid identity persisted or uploaded")
			}
		})
	}
}
func TestCloudNeverFollowsRedirects(t *testing.T) {
	for _, method := range []string{"GET", "POST"} {
		for _, status := range []int{301, 302, 303, 307, 308} {
			t.Run(fmt.Sprintf("%s/%d", method, status), func(t *testing.T) {
				var hits atomic.Int32
				server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					hits.Add(1)
					if r.URL.Path == "/sink" {
						_, _ = io.WriteString(w, "{}")
						return
					}
					http.Redirect(w, r, "/sink", status)
				}))
				defer server.Close()
				original := http.DefaultTransport
				http.DefaultTransport = server.Client().Transport
				t.Cleanup(func() { http.DefaultTransport = original })
				if _, err := requestJSON(method, server.URL+"/redirect", &Credential{Value: testGrant, ProducerToken: "producer"}, object{}); err == nil {
					t.Fatal("cloud redirect followed")
				}
				if hits.Load() != 1 {
					t.Fatal("credentials reached redirect target")
				}
			})
		}
	}
}

func TestCloudTransportRequiresHTTPSDespiteTestURLOverride(t *testing.T) {
	w := newTestWorker(t, false)
	_, err := requestJSON("POST", w.server.URL+"/v0/cloud-enrollment/leases", &Credential{Value: testGrant, ProducerToken: "producer"}, object{})
	if err == nil || !strings.Contains(err.Error(), "HTTPS") || len(w.at("")) != 0 {
		t.Fatalf("cloud credentials sent over HTTP: %v", err)
	}
}

// Expired leases must not become renewable local identity merely because their
// other fields are valid. The valid renewal checks above also exercise this path.
func TestCloudLeaseExpiryMustBeInFuture(t *testing.T) {
	f := newCloudFixture(t)
	f.worker.change(func() { f.worker.leaseOverrides["expires_at"] = time.Now().Add(-time.Second).Format(time.RFC3339) })
	if _, err := f.collect(); err == nil {
		t.Fatal("expired lease accepted")
	}
	state, _ := loadState(filepath.Join(f.root, "producer.json"))
	if len(state) != 2 {
		t.Fatal("expired lease persisted")
	}
}
