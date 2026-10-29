package runtime

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type cloudConfig struct {
	Worker, Provider, Transport, Integration string
	Target                                   Host
}

var cloudProvider = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
var cloudIntegration = regexp.MustCompile(`^[A-Za-z0-9_-]{1,100}$`)
var cloudGrant = regexp.MustCompile(`^plicg_[A-Za-z0-9_-]{43}$`)
var cloudProducer = regexp.MustCompile(`^plicl_[A-Za-z0-9_-]{43}$`)

func loadCloudConfig(path string) (cloudConfig, error) {
	v, err := loadState(path)
	if err != nil {
		return cloudConfig{}, err
	}
	keys := []string{"worker_url", "provider", "target", "transport", "integration_id"}
	if len(v) != len(keys) {
		return cloudConfig{}, errors.New("cloud configuration requires worker_url, provider, target, transport, and integration_id")
	}
	for _, key := range keys {
		if !nonempty(str(v[key])) {
			return cloudConfig{}, fmt.Errorf("cloud configuration missing %s", key)
		}
	}
	c := cloudConfig{strings.TrimRight(str(v["worker_url"]), "/"), str(v["provider"]), str(v["transport"]), str(v["integration_id"]), Host(str(v["target"]))}
	u, err := validateURL(c.Worker, false)
	if err != nil || u.Scheme != "https" || u.Path != "" {
		return c, errors.New("cloud worker URL must be an HTTPS origin without credentials, path, query, or fragment")
	}
	if !cloudProvider.MatchString(c.Provider) {
		return c, errors.New("invalid cloud provider")
	}
	if c.Target != Codex && c.Target != Claude {
		return c, errors.New("cloud capture supports claude and codex native JSONL")
	}
	if c.Transport != "proxy" && c.Transport != "secret" {
		return c, errors.New("cloud transport must be proxy or secret")
	}
	if !cloudIntegration.MatchString(c.Integration) {
		return c, errors.New("invalid cloud integration identifier")
	}
	return c, nil
}
func grantSecret(c cloudConfig) (string, error) {
	direct, file := os.Getenv("PROMPTLESS_CLOUD_GRANT"), os.Getenv("PROMPTLESS_CLOUD_GRANT_FILE")
	if c.Transport == "proxy" {
		if direct != "" || file != "" {
			return "", errors.New("proxy transport requires the grant in the connection, not in the sandbox")
		}
		return "", nil
	}
	if (direct != "") == (file != "") {
		return "", errors.New("set exactly one of PROMPTLESS_CLOUD_GRANT or PROMPTLESS_CLOUD_GRANT_FILE")
	}
	if file != "" {
		b, err := os.ReadFile(file)
		if err != nil {
			return "", err
		}
		direct = strings.TrimSpace(string(b))
	}
	if !cloudGrant.MatchString(direct) {
		return "", errors.New("invalid cloud grant secret")
	}
	return direct, nil
}
func executionScope(c cloudConfig, event object) (root, main, id string, err error) {
	h := hookContext(event)
	main, id = h.Transcript, h.SessionID
	if main == "" || id == "" || len([]rune(id)) > 160 {
		err = errors.New("cloud capture requires the hook's root session_id and transcript_path")
		return
	}
	if filepath.Ext(main) != ".jsonl" {
		err = errors.New("cloud capture requires a root native JSONL transcript")
		return
	}
	for _, part := range strings.Split(filepath.ToSlash(main), "/") {
		if part == "subagents" {
			err = errors.New("cloud capture requires a root native JSONL transcript")
			return
		}
	}
	root = homePath(".promptless", "cloud", hash(legacyJSON([]any{c.Worker, c.Integration, c.Provider, c.Target, id})))
	err = os.MkdirAll(root, 0700)
	if err == nil {
		err = os.Chmod(root, 0700)
	}
	return
}
func cloudSources(main string, h Host) []string {
	paths := []string{}
	if isFile(main) {
		paths = append(paths, main)
	}
	if h == Claude {
		children := strings.TrimSuffix(main, filepath.Ext(main)) + string(filepath.Separator) + "subagents"
		if resolvePath(children) == children {
			entries, _ := filepath.Glob(filepath.Join(children, "agent-*.jsonl"))
			for _, entry := range entries {
				if isFile(entry) && filepath.Dir(resolvePath(entry)) == children {
					paths = append(paths, entry)
				}
			}
		}
	}
	return paths
}
func enrollCloud(c cloudConfig, root, main, id, grant string) (*Credential, object, error) {
	path := filepath.Join(root, "producer.json")
	lock, err := acquireLock(path+".lock", time.Time{}, true)
	if err != nil {
		return nil, nil, err
	}
	defer lock.close()
	state, err := loadState(path)
	if err != nil {
		return nil, nil, err
	}
	if len(state) == 0 {
		producer, err := randomToken("plicl_")
		if err != nil {
			return nil, nil, err
		}
		state = object{"producer_token": producer, "transcript_path": main}
		if err = writeState(path, state); err != nil {
			return nil, nil, err
		}
	}
	if state["transcript_path"] != main {
		return nil, nil, errors.New("execution transcript changed; restore its original private state")
	}
	producer := str(state["producer_token"])
	if !cloudProducer.MatchString(producer) {
		return nil, nil, errors.New("invalid saved producer secret")
	}
	credential := &Credential{Value: grant, ProducerToken: producer}
	lease, err := requestJSON("POST", c.Worker+"/v0/cloud-enrollment/leases", credential, object{"execution_id": id, "credential_hash": hash([]byte(producer)), "target": c.Target})
	if err != nil {
		return nil, nil, err
	}
	if lease["execution_id"] != id || str(lease["target"]) != string(c.Target) || lease["provider"] != c.Provider {
		return nil, nil, errors.New("cloud lease scope mismatch")
	}
	expiry, err := time.Parse(time.RFC3339Nano, str(lease["expires_at"]))
	if err != nil || !expiry.After(time.Now()) {
		return nil, nil, errors.New("cloud lease has expired or has invalid expiry")
	}
	for _, key := range []string{"id", "grant_id", "host_instance_id", "deployment_instance_id"} {
		if !nonempty(str(lease[key])) {
			return nil, nil, fmt.Errorf("cloud lease missing %s", key)
		}
		if old, ok := state[key]; ok && old != lease[key] {
			return nil, nil, errors.New("cloud identity changed; use a new integration identifier for a new grant")
		}
		state[key] = lease[key]
	}
	if err = writeState(path, state); err != nil {
		return nil, nil, err
	}
	credential.ID = str(lease["id"])
	credential.DeploymentID = str(lease["deployment_instance_id"])
	return credential, lease, nil
}
func collectCloud(path string, event object, drain time.Duration) (status object, err error) {
	if drain < 0 || drain > 60*time.Second {
		return nil, errors.New("cloud drain must be between 0 and 60 seconds")
	}
	c, err := loadCloudConfig(path)
	if err != nil {
		return nil, err
	}
	root, main, id, err := executionScope(c, event)
	if err != nil {
		return nil, err
	}
	status = object{"status": "partial", "execution_id": id, "uploaded_batches": 0}
	statusPath := filepath.Join(root, "status.json")
	if err = writeState(statusPath, status); err != nil {
		return nil, err
	}
	defer func() {
		if err != nil {
			status["error_type"] = fmt.Sprintf("%T", err)
			_ = writeState(statusPath, status)
		}
	}()
	grant, err := grantSecret(c)
	if err != nil {
		return status, err
	}
	credential, lease, err := enrollCloud(c, root, main, id, grant)
	if err != nil {
		return status, err
	}
	signed, err := requestJSON("GET", c.Worker+"/v0/host-enrollment/policy?target="+string(c.Target), credential, nil)
	if err != nil {
		return status, err
	}
	policy, err := validatePolicy(signed, c.Target)
	if err != nil {
		return status, err
	}
	if requiresNewer(policy.RequiredVersion) {
		status["status"] = "blocked"
		status["reason"] = "bootstrap_upgrade_required"
		return status, writeState(statusPath, status)
	}
	lifecycle := "session_start"
	switch event["hook_event_name"] {
	case "Stop":
		lifecycle = "stop"
	case "SessionEnd":
		lifecycle = "session_end"
	case "SubagentStop":
		lifecycle = "subagent_stop"
	}
	deadline := time.Now().Add(drain)
	options := uploadOptions{URL: c.Worker + "/v0/traces/batches?target=" + string(c.Target), Credential: credential, Host: c.Target, Metadata: metadata(filepath.Dir(path), c.Target), Policy: policy, Lifecycle: lifecycle, Context: hookContext(event), Ledger: scopedLedger(filepath.Join(root, "ledger.json"), c.Worker, str(lease["deployment_instance_id"]), str(lease["host_instance_id"]), c.Target), Deadline: deadline.Add(10 * time.Second)}
	total := newCounts()
	for {
		counts, err := uploadPaths(cloudSources(main, c.Target), options)
		if err != nil {
			return status, err
		}
		total.add(counts)
		if !time.Now().Before(deadline) {
			break
		}
		time.Sleep(min(2*time.Second, time.Until(deadline)))
	}
	status["status"] = "drained"
	if len(total.Unreadable) > 0 || !isFile(main) {
		status["status"] = "partial"
	}
	status["uploaded_batches"] = total.Batches
	status["lease_id"] = lease["id"]
	return status, writeState(statusPath, status)
}
