package runtime

import (
	"bytes"
	"compress/gzip"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	goruntime "runtime"
	"sort"
	"strings"
	"time"
)

const maxContextFile = 16 << 20
const maxContextSource = 32 << 20
const maxContextTransport = 8 << 20
const maxContextFiles = 512
const maxContextDiagnostics = 64 << 10
const maxContextInventory = 512 << 10

// Diagnostics also appear outside compressed bundles, so bound their wire size.
// Reserve room for one explicit notice when detailed entries are omitted.
type contextDiagnostics struct {
	entries []object
	bytes   int
	omitted bool
}

func (d *contextDiagnostics) add(entries ...object) {
	for _, entry := range entries {
		size := len(legacyJSON(entry)) + 2
		if d.bytes+size > maxContextDiagnostics-256 {
			d.omitted = true
			continue
		}
		d.entries = append(d.entries, entry)
		d.bytes += size
	}
}
func (d *contextDiagnostics) values() []object {
	entries := append([]object{}, d.entries...)
	if d.omitted {
		entries = append(entries, object{"code": "context_diagnostics_truncated", "severity": "warning"})
	}
	return entries
}

var contextSecretPath = regexp.MustCompile(`(?i)(^\.env(\.|$)|(^|[-_.])(secrets?|credentials?|tokens?|password|passwd|private[-_]?key|api[-_]?key)([-_.]|$)|^(id_rsa|id_ed25519|\.netrc)$)`)
var contextSecretValue = regexp.MustCompile(`(?i)(api[_-]?key|secret|token|password|authorization|private[_-]?key)\s*['"]?\s*[:=]\s*['"]?[A-Za-z0-9._~+/=-]{20,}|authorization\s*['"]?\s*[:=]\s*['"]?(bearer|basic)\s+[A-Za-z0-9._~+/=-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----`)

// OpenRoot confines reads even if a plugin changes symlinks while being scanned.
// Limits bound inspected entries as well as retained payload bytes.
func contextFile(root *os.Root, name string, remaining int) ([]byte, error) {
	info, err := root.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() > maxContextFile || info.Size() > int64(remaining) {
		return nil, errors.New("unsafe_or_large_file")
	}
	f, err := root.Open(name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return nil, errors.New("file_changed")
	}
	b, err := io.ReadAll(io.LimitReader(f, int64(min(maxContextFile, remaining))+1))
	if err != nil {
		return nil, err
	}
	if len(b) > maxContextFile || len(b) > remaining {
		return nil, errors.New("file_size")
	}
	if contextSecretValue.Match(b) {
		return nil, errors.New("sensitive_content")
	}
	return b, nil
}
func contextSensitivePath(path string) bool {
	for _, part := range strings.Split(filepath.ToSlash(path), "/") {
		if contextSecretPath.MatchString(part) {
			return true
		}
	}
	return false
}
func skillDescription(content []byte, fallback string) (string, string) {
	name, description := fallback, fallback
	lines := strings.Split(string(content), "\n")
	for _, line := range lines {
		if strings.HasPrefix(strings.TrimSpace(line), "# ") {
			description = strings.TrimSpace(strings.TrimSpace(line)[2:])
			break
		}
	}
	if len(lines) > 0 && strings.TrimSpace(lines[0]) == "---" {
		for _, line := range lines[1:] {
			if strings.TrimSpace(line) == "---" {
				break
			}
			k, v, ok := strings.Cut(line, ":")
			if !ok {
				continue
			}
			v = strings.Trim(strings.TrimSpace(v), "\"'")
			if v == "" {
				continue
			}
			switch strings.TrimSpace(k) {
			case "name":
				name = v
			case "description":
				description = v
			}
		}
	}
	// Metadata shares the transport budget and must not amplify a huge heading.
	if len(name) > 512 {
		name = fallback
	}
	if len(description) > 4096 {
		description = description[:4096]
	}
	return name, description
}
func skillBundle(root *os.Root, path, name string, deadline time.Time) (object, []object, error) {
	files := []object{}
	diagnostics := contextDiagnostics{}
	sourceBytes, visited := 0, 0
	truncated := false
	err := walkContext(root, func(relative string, entry fs.DirEntry, walkErr error) error {
		if err := checkDeadline(deadline); err != nil {
			return err
		}
		visited++
		if visited > maxContextFiles {
			truncated = true
			return fs.SkipAll
		}
		if walkErr != nil {
			diagnostics.add(object{"code": "context_artifact_scan_failed", "severity": "warning"})
			return fs.SkipDir
		}
		if relative == "." {
			return nil
		}
		if contextSensitivePath(relative) {
			if entry.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		if entry.IsDir() {
			return nil
		}
		b, err := contextFile(root, relative, maxContextSource-sourceBytes)
		if err != nil {
			diagnostics.add(object{"code": "context_artifact_file_skipped", "severity": "warning", "path": relative})
			return nil
		}
		sourceBytes += len(b)
		files = append(files, object{"path": relative, "byte_count": len(b), "sha256": hash(b), "content_encoding": "base64", "content_base64": base64.StdEncoding.EncodeToString(b)})
		return nil
	})
	if err != nil {
		return nil, nil, err
	}
	return object{"schema_version": 1, "kind": "skill_bundle", "name": name, "root_path_hash": hash([]byte(path)), "file_count": len(files), "file_source_byte_count": sourceBytes, "truncated": truncated, "files": files, "diagnostics": diagnostics.values()}, diagnostics.values(), nil
}
func sessionContext(rootPath string, o uploadOptions) (object, []object, error) {
	session := o.Context.SessionID
	if o.Lifecycle == "subagent_stop" && o.Context.ParentSessionID != "" {
		session = o.Context.ParentSessionID
	}
	if o.Policy.ContextVersion != 1 || session == "" {
		return nil, nil, nil
	}
	skills, artifacts, summaries := []object{}, []object{}, []object{}
	diagnostics := contextDiagnostics{}
	inventoryBytes := 0
	remaining := maxContextTransport
	skillsPath := filepath.Join(rootPath, "skills")
	info, err := os.Lstat(skillsPath)
	if rootPath != "" && err == nil && info.IsDir() && info.Mode()&os.ModeSymlink == 0 {
		plugin, err := os.OpenRoot(rootPath)
		if err != nil {
			return nil, nil, err
		}
		defer plugin.Close()
		root, err := plugin.OpenRoot("skills")
		if err != nil {
			return nil, nil, err
		}
		defer root.Close()
		dir, err := root.Open(".")
		if err != nil {
			return nil, nil, err
		}
		defer dir.Close()
		entries, err := dir.ReadDir(maxContextFiles + 1)
		if err != nil && err != io.EOF {
			return nil, nil, err
		}
		if len(entries) > maxContextFiles {
			entries = entries[:maxContextFiles]
			diagnostics.add(object{"code": "context_inventory_limit_exceeded", "severity": "warning"})
		}
		// Directory enumeration order must not change manifest hashes.
		sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
		for _, entry := range entries {
			if err := checkDeadline(o.Deadline); err != nil {
				return nil, nil, err
			}
			if !entry.IsDir() || entry.Type()&os.ModeSymlink != 0 || contextSensitivePath(entry.Name()) {
				continue
			}
			skill, err := root.OpenRoot(entry.Name())
			if err != nil {
				continue
			}
			b, err := contextFile(skill, "SKILL.md", maxContextFile)
			if err != nil {
				skill.Close()
				continue
			}
			name, description := skillDescription(b, entry.Name())
			path := filepath.Join(skillsPath, entry.Name())
			inventory := object{"name": name, "description": description, "source": "plugin", "plugin_id": o.Metadata.PluginID, "plugin_version": o.Metadata.PluginVersion, "root_path_hash": hash([]byte(path)), "skill_file_sha256": hash(b)}
			// Include space for the artifact ID added after compression.
			size := len(legacyJSON(inventory)) + 128
			if inventoryBytes+size > maxContextInventory {
				skill.Close()
				diagnostics.add(object{"code": "context_inventory_budget_exceeded", "severity": "warning"})
				break
			}
			inventoryBytes += size
			bundle, notes, err := skillBundle(skill, path, name, o.Deadline)
			skill.Close()
			if err != nil {
				return nil, nil, err
			}
			diagnostics.add(notes...)
			raw := contextJSON(bundle)
			var compressed bytes.Buffer
			z := gzip.NewWriter(&compressed)
			_, err = z.Write(raw)
			if err != nil {
				return nil, nil, err
			}
			if err = z.Close(); err != nil {
				return nil, nil, err
			}
			digest := hash(compressed.Bytes())
			id := "skill-bundle-" + hash(contextJSON(object{"plugin_id": o.Metadata.PluginID, "plugin_version": o.Metadata.PluginVersion, "name": name, "content_sha256": digest}))[:48]
			meta := object{"plugin_id": o.Metadata.PluginID, "plugin_version": o.Metadata.PluginVersion, "root_path_hash": hash([]byte(path))}
			summary := object{"artifact_id": id, "kind": "skill_bundle", "name": name, "content_sha256": digest, "source_byte_count": len(raw), "metadata": meta}
			artifact := clone(summary)
			artifact["session_id"] = session
			artifact["content_type"] = "application/json"
			artifact["content_encoding"] = "gzip"
			artifact["content_base64"] = base64.StdEncoding.EncodeToString(compressed.Bytes())
			size = len(legacyJSON(artifact))
			if size <= remaining {
				remaining -= size
				inventory["artifact_id"] = id
				artifacts = append(artifacts, artifact)
				summaries = append(summaries, summary)
			} else {
				diagnostics.add(object{"code": "context_artifact_transport_budget_exceeded", "severity": "warning", "name": name})
			}
			skills = append(skills, inventory)
		}
	} else {
		diagnostics.add(object{"code": "missing_available_skills", "severity": "warning"})
	}
	cwd, _ := os.Getwd()
	home, _ := os.UserHomeDir()
	workspace := object{"cwd": cwd, "home_path_hash": hash([]byte(home))}
	if o.Context.Transcript != "" {
		workspace["transcript_path_hash"] = hash([]byte(o.Context.Transcript))
	}
	if o.Context.AgentTranscript != "" {
		workspace["agent_transcript_path_hash"] = hash([]byte(o.Context.AgentTranscript))
	}
	m := o.Metadata
	manifest := object{"schema_version": 1, "session_id": session, "collector_version": m.BootstrapVersion, "plugin_version": m.PluginVersion, "installed_plugins": []object{{"plugin_id": m.PluginID, "version": m.PluginVersion, "plugin_version": m.PluginVersion, "target": m.Target, "source": "managed_runtime"}}, "available_skills": skills, "workspace": workspace, "runtime": object{"host": o.Host, "runtime_version": m.BootstrapVersion, "toolchain_version": m.ToolchainVersion, "go_version": goruntime.Version(), "platform": goruntime.GOOS + "/" + goruntime.GOARCH}, "sidecars": []object{{"kind": "managed_runtime", "id": "host-runtime", "version": m.BootstrapVersion, "plugin_root_hash": hash([]byte(rootPath))}}, "raw_artifacts": summaries, "diagnostics": diagnostics.values()}
	for k, v := range o.Context.requestContext(o.Lifecycle) {
		manifest[k] = v
	}
	manifest["session_id"] = session
	// Admit artifacts against the complete wire request, including the manifest,
	// summaries, inventory, diagnostics, and envelope. Hash the admitted payload.
	for {
		delete(manifest, "manifest_hash")
		delete(manifest, "captured_at")
		manifest["manifest_hash"] = hash(contextJSON(manifest))
		manifest["captured_at"] = nowISO()
		if len(legacyJSON(contextRequest(manifest, artifacts, o))) <= maxTransport {
			return manifest, artifacts, nil
		}
		if len(artifacts) == 0 {
			return nil, nil, errors.New("context metadata exceeds transport limit")
		}
		last := len(artifacts) - 1
		id := artifacts[last]["artifact_id"]
		artifacts, summaries = artifacts[:last], summaries[:last]
		for _, skill := range skills {
			if skill["artifact_id"] == id {
				delete(skill, "artifact_id")
			}
		}
		diagnostics.add(object{"code": "context_artifact_transport_budget_exceeded", "severity": "warning"})
		manifest["raw_artifacts"], manifest["diagnostics"] = summaries, diagnostics.values()
	}
}

// Context acknowledgment never advances transcript offsets. Holding the same
// ledger lock makes deduplication and transcript progress survive concurrent hooks.
func uploadSessionContext(root string, o uploadOptions) (bool, error) {
	manifest, artifacts, err := sessionContext(root, o)
	if err != nil || manifest == nil {
		return false, err
	}
	lock, err := acquireLock(o.Ledger+".lock", o.Deadline, true)
	if err != nil {
		return false, err
	}
	defer lock.close()
	ledger, err := loadLedger(o.Ledger)
	if err != nil {
		return false, err
	}
	session, digest := str(manifest["session_id"]), str(manifest["manifest_hash"])
	if ledger.ContextHashes[session] == digest {
		return false, nil
	}
	request := contextRequest(manifest, artifacts, o)
	if len(legacyJSON(request)) > maxTransport {
		return false, errors.New("context exceeds transport limit")
	}
	if err = checkDeadline(o.Deadline); err != nil {
		return false, err
	}
	response, err := requestJSON("POST", o.URL, o.Credential, request)
	if err != nil {
		return false, err
	}
	if err = validateContextAck(response, request, o.Policy, digest, len(artifacts)); err != nil {
		return false, err
	}
	ledger.ContextHashes[session] = digest
	return true, ledger.write()
}
func contextRequest(manifest object, artifacts []object, o uploadOptions) object {
	digest := str(manifest["manifest_hash"])
	request := object{"batch_id": "native-" + hash(contextJSON(object{"host": o.Host, "policy_version": o.Policy.Version, "context_manifest_hash": digest, "ranges": []object{}}))[:48], "source": o.Host, "host": o.Host, "policy_version": o.Policy.Version, "collector_version": o.Metadata.BootstrapVersion, "plugin_version": o.Metadata.PluginVersion, "uploaded_at": nowISO(), "chunks": []object{}, "session_context_manifests": []object{manifest}, "context_artifacts": artifacts}
	for k, v := range o.Context.requestContext(o.Lifecycle) {
		request[k] = v
	}
	return request
}
func validateContextAck(response, request object, p Policy, digest string, count int) error {
	if response["accepted"] != true || response["batch_id"] != request["batch_id"] {
		return errors.New("context batch not acknowledged")
	}
	for key, want := range map[string]int64{"policy_version": p.Version, "raw_artifact_count": 0, "skipped_record_count": 0, "raw_context_artifact_count": int64(count)} {
		if n, ok := integer(response[key]); !ok || n != want {
			return errors.New("context acknowledgment count mismatch")
		}
	}
	if n, ok := integer(response["unparsed_record_count"]); !ok || n < 0 {
		return errors.New("invalid context unparsed count")
	}
	ranges, ok := response["acknowledged_ranges"].([]any)
	if !ok || len(ranges) != 0 {
		return errors.New("unexpected context ranges")
	}
	hashes, ok := response["accepted_session_context_manifest_hashes"].([]any)
	if !ok || len(hashes) != 1 || hashes[0] != digest {
		return errors.New("context manifest hash not acknowledged")
	}
	return nil
}

// Preserve the compact, ASCII JSON hashing contract used by the worker.
func contextJSON(value any) []byte {
	var result bytes.Buffer
	_ = json.Compact(&result, legacyJSON(value))
	return result.Bytes()
}

// Read bounded batches instead of WalkDir's unbounded directory allocation.
func walkContext(root *os.Root, visit func(string, fs.DirEntry, error) error) error {
	queue := []string{"."}
	visited := 0
	for len(queue) > 0 {
		path := queue[0]
		queue = queue[1:]
		dir, err := root.Open(path)
		if err != nil {
			if e := visit(path, nil, err); e != nil && e != fs.SkipDir {
				return e
			}
			continue
		}
		entries, err := dir.ReadDir(maxContextFiles - visited + 1)
		dir.Close()
		if err != nil && err != io.EOF {
			return err
		}
		sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
		for _, entry := range entries {
			relative := filepath.ToSlash(filepath.Join(path, entry.Name()))
			err := visit(relative, entry, nil)
			visited++
			if err == fs.SkipAll {
				return nil
			}
			if err == fs.SkipDir {
				continue
			}
			if err != nil {
				return err
			}
			if entry.IsDir() {
				queue = append(queue, relative)
			}
		}
	}
	return nil
}
