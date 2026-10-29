package runtime

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	hashpkg "hash"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

var errDeadline = errors.New("native trace collection exceeded deadline")

func checkDeadline(deadline time.Time) error {
	if !deadline.IsZero() && !time.Now().Before(deadline) {
		return errDeadline
	}
	return nil
}

type sourceLedger struct {
	ContextHashes object
	Path          string
	Sources       object
	Reset         map[string]bool
	Drift         []object
}
type sourceEvent struct {
	Kind, Path, PathHash, ContentHash, Reason string
	Start, End                                int64
	Content, PrefixState                      []byte
	EndPrefix                                 string
}

func (e sourceEvent) rangeProof() object {
	return object{"kind": e.Kind, "source_path_hash": e.PathHash, "start_offset": e.Start, "end_offset": e.End, "content_sha256": e.ContentHash}
}
func (e sourceEvent) prefixAt(end int64) (string, error) {
	if end < e.Start || end > e.End {
		return "", errors.New("source offset is outside captured upload snapshot")
	}
	if end == e.End {
		return e.EndPrefix, nil
	}
	if e.Content == nil {
		return "", errors.New("source snapshot has no captured bytes")
	}
	h := sha256.New()
	if err := h.(encoding.BinaryUnmarshaler).UnmarshalBinary(e.PrefixState); err != nil {
		return "", err
	}
	h.Write(e.Content[:end-e.Start])
	return hex.EncodeToString(h.Sum(nil)), nil
}
func loadLedger(path string) (*sourceLedger, error) {
	l := &sourceLedger{Path: path, ContextHashes: object{}, Sources: object{}, Reset: map[string]bool{}, Drift: []object{}}
	if !exists(path) {
		return l, nil
	}
	v, err := loadState(path)
	reason := ""
	if err != nil {
		reason = "invalid_json"
	} else if n, ok := integer(v["schema_version"]); !ok || n != 1 {
		reason = "unsupported_schema"
	} else if _, ok := v["sources"].(map[string]any); !ok {
		reason = "invalid_sources"
	}
	if reason != "" {
		if err := os.Rename(path, path+".corrupt-"+time.Now().UTC().Format("20060102150405.000000000")); err != nil {
			return nil, err
		}
		l.Drift = append(l.Drift, object{"kind": "native_trace_ledger_reset", "reason": reason})
		return l, nil
	}
	for key, value := range obj(v["session_context_manifest_hashes"]) {
		if digest := str(value); len(digest) == 64 && key != "" {
			l.ContextHashes[key] = digest
		}
	}
	for key, value := range obj(v["sources"]) {
		source := obj(value)
		n, ok := integer(source["end_offset"])
		if len(key) != 64 || !ok || n < 0 {
			continue
		}
		delete(source, "instruction_hub_release_markers")
		delete(source, "provenance_only")
		l.Sources[key] = source
	}
	return l, nil
}
func (l *sourceLedger) write() error {
	return writeState(l.Path, object{"schema_version": 1, "updated_at": nowISO(), "sources": l.Sources, "session_context_manifest_hashes": l.ContextHashes})
}
func (l *sourceLedger) record(e sourceEvent, end int64) error {
	source := clone(obj(l.Sources[e.PathHash]))
	previous := number(source["end_offset"])
	if l.Reset[e.PathHash] {
		previous = 0
	}
	if previous > end {
		return errors.New("source snapshot does not match ledger offset")
	}
	prefix, err := e.prefixAt(end)
	if err != nil {
		return err
	}
	source["path"] = e.Path
	source["end_offset"] = end
	source["prefix_sha256"] = prefix
	source["updated_at"] = nowISO()
	delete(source, "instruction_hub_release_markers")
	delete(source, "provenance_only")
	l.Sources[e.PathHash] = source
	return nil
}
func copyDigest(h hashpkg.Hash) []byte {
	state, _ := h.(encoding.BinaryMarshaler).MarshalBinary()
	return state
}
func digestFrom(state []byte) hashpkg.Hash {
	h := sha256.New()
	_ = h.(encoding.BinaryUnmarshaler).UnmarshalBinary(state)
	return h
}

// readRecord bounds retained bytes even for a multi-gigabyte JSONL record. The
// digest and byte count still describe the entire newline-terminated record.
func readRecord(r *bufio.Reader, prefix hashpkg.Hash, deadline time.Time) (content []byte, count int64, digest string, complete bool, err error) {
	h := sha256.New()
	for {
		if err = checkDeadline(deadline); err != nil {
			return
		}
		part, readErr := r.ReadSlice('\n')
		count += int64(len(part))
		h.Write(part)
		prefix.Write(part)
		if count <= maxRecord {
			content = append(content, part...)
		} else {
			content = nil
		}
		if readErr == nil {
			return content, count, hex.EncodeToString(h.Sum(nil)), true, nil
		}
		if errors.Is(readErr, bufio.ErrBufferFull) {
			continue
		}
		if errors.Is(readErr, io.EOF) {
			return nil, count, "", false, nil
		}
		return nil, count, "", false, readErr
	}
}
func nextSourceEvent(l *sourceLedger, path string, deadline time.Time) (*sourceEvent, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("trace source is not a regular file")
	}
	ph := hash([]byte(path))
	source := obj(l.Sources[ph])
	start := max(int64(0), number(source["end_offset"]))
	size := info.Size()
	prefix := sha256.New()
	reset := func(kind string) {
		l.Drift = append(l.Drift, object{"kind": kind, "source_path_hash": ph, "previous_end_offset": start, "current_size": size})
		l.Reset[ph] = true
		start = 0
		prefix = sha256.New()
	}
	if start > size {
		reset("native_trace_source_rewound")
	}
	remaining := start
	block := make([]byte, 1<<20)
	for remaining > 0 {
		if err = checkDeadline(deadline); err != nil {
			return nil, err
		}
		n, e := io.ReadFull(f, block[:min(int64(len(block)), remaining)])
		if e != nil {
			return nil, e
		}
		prefix.Write(block[:n])
		remaining -= int64(n)
	}
	if expected := str(source["prefix_sha256"]); start > 0 && expected != "" && hex.EncodeToString(prefix.Sum(nil)) != expected {
		reset("native_trace_source_identity_changed")
		if _, err = f.Seek(0, io.SeekStart); err != nil {
			return nil, err
		}
	}
	if start == size {
		return nil, nil
	}
	r := bufio.NewReaderSize(io.LimitReader(f, size-start), 64<<10)
	prefixState := copyDigest(prefix)
	end := start
	buffer := []byte{}
	for end < size {
		before := copyDigest(prefix)
		line, count, digest, complete, readErr := readRecord(r, prefix, deadline)
		if readErr != nil {
			return nil, readErr
		}
		if !complete {
			break
		}
		if len(buffer) > 0 && (count > maxRecord || int64(len(buffer))+count > chunkTarget) {
			prefix = digestFrom(before)
			break
		}
		if count > maxRecord {
			return &sourceEvent{Kind: "oversized_record", Path: path, PathHash: ph, ContentHash: digest, Reason: "content_size", Start: start, End: end + count, PrefixState: prefixState, EndPrefix: hex.EncodeToString(prefix.Sum(nil))}, nil
		}
		buffer = append(buffer, line...)
		end += count
	}
	if len(buffer) == 0 {
		return nil, nil
	}
	// A partial trailing record may have entered the read digest. Derive the
	// acknowledged prefix solely from retained, complete records.
	prefix = digestFrom(prefixState)
	prefix.Write(buffer)
	return &sourceEvent{Kind: "jsonl_range", Path: path, PathHash: ph, ContentHash: hash(buffer), Start: start, End: end, Content: buffer, PrefixState: prefixState, EndPrefix: hex.EncodeToString(prefix.Sum(nil))}, nil
}
func hookContext(v object) HookContext {
	first := func(keys ...string) string {
		for _, key := range keys {
			var value any = v
			for _, part := range strings.Split(key, ".") {
				value = obj(value)[part]
			}
			if s := str(value); nonempty(s) {
				return s
			}
		}
		return ""
	}
	h := HookContext{Transcript: first("transcript_path", "transcriptPath", "transcript.path"), AgentTranscript: first("agent_transcript_path", "agentTranscriptPath", "agent.transcript_path", "agent.transcriptPath"), SessionID: first("session_id", "sessionId", "conversation_id", "conversationId", "session.id"), ParentSessionID: first("parent_session_id", "parentSessionId", "parent_conversation_id", "parentConversationId", "parent_session.id", "parentSession.id"), AgentID: first("agent_id", "agentId", "subagent_id", "subagentId", "agent_name", "agentName", "agent.id"), AgentType: first("agent_type", "agentType", "subagent_type", "subagentType", "agent.type"), GenerationID: first("generation_id")}
	if h.Transcript != "" {
		h.Transcript = resolvePath(h.Transcript)
	}
	if h.AgentTranscript != "" {
		h.AgentTranscript = resolvePath(h.AgentTranscript)
	}
	return h
}
func (h HookContext) paths(lifecycle string) []string {
	paths := []string{}
	if lifecycle == "subagent_stop" && h.AgentTranscript != "" {
		paths = append(paths, h.AgentTranscript)
	} else if h.Transcript != "" {
		paths = append(paths, h.Transcript)
	}
	if h.AgentTranscript != "" && (len(paths) == 0 || paths[0] != h.AgentTranscript) {
		paths = append(paths, h.AgentTranscript)
	}
	return paths
}
func (h HookContext) eventPath(lifecycle string) string {
	if lifecycle == "subagent_stop" && h.AgentTranscript != "" {
		return h.AgentTranscript
	}
	return h.Transcript
}
func (h HookContext) requestContext(lifecycle string) object {
	if lifecycle == "subagent_stop" {
		if h.ParentSessionID == "" {
			h.ParentSessionID = h.SessionID
			h.SessionID = ""
		}
		if h.AgentID == "" && h.AgentTranscript != "" {
			h.AgentID = hash([]byte(h.AgentTranscript))[:32]
		}
		if h.AgentType == "" && h.AgentID != "" {
			h.AgentType = "subagent"
		}
	}
	p := object{}
	if h.SessionID != "" {
		p["session_id"] = h.SessionID
	}
	if h.ParentSessionID != "" && h.AgentID != "" {
		p["parent_session_id"] = h.ParentSessionID
		p["agent_id"] = h.AgentID
		if h.AgentType != "" {
			p["agent_type"] = h.AgentType
		}
	}
	return p
}
func uploadRequest(h Host, m Metadata, p Policy, event sourceEvent, context HookContext, lifecycle string) (object, error) {
	chunk := event.rangeProof()
	if lifecycle != "" && event.Path == context.eventPath(lifecycle) {
		chunk["lifecycle_event"] = lifecycle
	}
	if event.Kind == "oversized_record" {
		chunk["byte_count"] = event.End - event.Start
		chunk["oversized_reason"] = event.Reason
	} else {
		var compressed bytes.Buffer
		writer, err := gzip.NewWriterLevel(&compressed, gzip.BestCompression)
		if err != nil {
			return nil, err
		}
		if _, err = writer.Write(event.Content); err != nil {
			return nil, err
		}
		if err = writer.Close(); err != nil {
			return nil, err
		}
		chunk["line_count"] = bytes.Count(event.Content, []byte{'\n'})
		chunk["content_encoding"] = "gzip"
		chunk["content_base64"] = base64.StdEncoding.EncodeToString(compressed.Bytes())
	}
	id := "native-" + hash(legacyJSON(object{"host": h, "policy_version": p.Version, "ranges": []object{event.rangeProof()}}))[:48]
	request := object{"batch_id": id, "source": h, "host": h, "policy_version": p.Version, "collector_version": m.BootstrapVersion, "plugin_version": m.PluginVersion, "uploaded_at": nowISO(), "chunks": []object{chunk}}
	for k, v := range context.requestContext(lifecycle) {
		request[k] = v
	}
	return request, nil
}
func validateAcknowledgment(response, request object, p Policy, e sourceEvent) error {
	if response["accepted"] != true || str(response["batch_id"]) != str(request["batch_id"]) {
		return errors.New("trace response did not accept the requested batch")
	}
	for key, expected := range map[string]int64{"policy_version": p.Version, "raw_artifact_count": boolInt(e.Kind == "jsonl_range"), "skipped_record_count": boolInt(e.Kind == "oversized_record")} {
		n, ok := integer(response[key])
		if !ok || n != expected {
			return fmt.Errorf("trace response %s did not match upload request", key)
		}
	}
	if n, ok := integer(response["unparsed_record_count"]); !ok || n < 0 {
		return errors.New("trace response unparsed_record_count must be a non-negative integer")
	}
	if !bytes.Equal(legacyJSON(response["acknowledged_ranges"]), legacyJSON([]object{e.rangeProof()})) {
		return errors.New("trace response acknowledged_ranges did not match upload request")
	}
	return nil
}
func boolInt(b bool) int64 {
	if b {
		return 1
	}
	return 0
}
func reconcileConflict(l *sourceLedger, h Host, e sourceEvent, err error) (bool, error) {
	var httpErr *HTTPError
	if !errors.As(err, &httpErr) || httpErr.Status != 409 {
		return false, err
	}
	body, decodeErr := decodeObject(httpErr.Body)
	if decodeErr != nil {
		return false, err
	}
	d := obj(body["detail"])
	if d["code"] != "trace_source_sequence_conflict" || str(d["source"]) != string(h) || str(d["source_path_hash"]) != e.PathHash {
		return false, err
	}
	proof := obj(d["acknowledged_range"])
	start, ok1 := integer(d["requested_start_offset"])
	end, ok2 := integer(d["requested_end_offset"])
	watermark, ok3 := integer(d["acknowledged_offset"])
	proofStart, ok4 := integer(proof["start_offset"])
	proofEnd, ok5 := integer(proof["end_offset"])
	if !ok1 || !ok2 || !ok3 || !ok4 || !ok5 || start != e.Start || end != e.End || proofStart != start || proofEnd != watermark {
		return false, err
	}
	local := obj(l.Sources[e.PathHash])
	if number(local["end_offset"]) != start || !(start < watermark && watermark < end) || e.Content == nil {
		return false, errors.New("trace conflict did not identify an interior local watermark")
	}
	prefix, prefixErr := e.prefixAt(start)
	if prefixErr != nil {
		return false, prefixErr
	}
	if expected := str(local["prefix_sha256"]); expected != "" && expected != prefix {
		return false, errors.New("source identity changed before watermark reconciliation")
	}
	content := e.Content[:watermark-start]
	if !bytes.HasSuffix(content, []byte{'\n'}) || hash(content) != str(proof["content_sha256"]) {
		return false, errors.New("trace conflict proof did not match captured records")
	}
	if err = l.record(e, watermark); err != nil {
		return false, err
	}
	l.Drift = append(l.Drift, object{"kind": "native_trace_source_watermark_reconciled", "source_path_hash": e.PathHash, "previous_end_offset": start, "worker_end_offset": watermark})
	return true, nil
}

type uploadCounts struct {
	Batches, Chunks, Unparsed int64
	Unreadable                map[string]bool
}

func newCounts() uploadCounts { return uploadCounts{Unreadable: map[string]bool{}} }
func (a *uploadCounts) add(b uploadCounts) {
	a.Batches += b.Batches
	a.Chunks += b.Chunks
	a.Unparsed += b.Unparsed
	for h := range b.Unreadable {
		a.Unreadable[h] = true
	}
}

type uploadOptions struct {
	URL        string
	Credential *Credential
	Host       Host
	Metadata   Metadata
	Policy     Policy
	Lifecycle  string
	Context    HookContext
	Ledger     string
	Deadline   time.Time
	Limit      int64
}

func uploadPaths(paths []string, o uploadOptions) (uploadCounts, error) {
	counts := newCounts()
	for len(paths) > 0 && (o.Limit == 0 || counts.Batches < o.Limit) {
		if err := checkDeadline(o.Deadline); err != nil {
			return counts, err
		}
		more, err := func() (bool, error) {
			lock, err := acquireLock(o.Ledger+".lock", o.Deadline, true)
			if err != nil {
				return false, err
			}
			defer lock.close()
			ledger, err := loadLedger(o.Ledger)
			if err != nil {
				return false, err
			}
			var event *sourceEvent
			for _, path := range paths {
				if err = checkDeadline(o.Deadline); err != nil {
					return false, err
				}
				event, err = nextSourceEvent(ledger, path, o.Deadline)
				if errors.Is(err, errDeadline) {
					return false, err
				}
				if err != nil {
					counts.Unreadable[hash([]byte(path))] = true
					ledger.Drift = append(ledger.Drift, object{"kind": "native_trace_source_unreadable", "source_path_hash": hash([]byte(path))})
					continue
				}
				if event != nil {
					break
				}
			}
			if event == nil {
				if len(ledger.Drift) > 0 {
					return false, ledger.write()
				}
				return false, nil
			}
			request, err := uploadRequest(o.Host, o.Metadata, o.Policy, *event, o.Context, o.Lifecycle)
			if err != nil {
				return false, err
			}
			if len(legacyJSON(request)) > maxTransport && event.Kind == "jsonl_range" {
				event.Kind = "oversized_record"
				event.Reason = "transport_size"
				event.Content = nil
				request, err = uploadRequest(o.Host, o.Metadata, o.Policy, *event, o.Context, o.Lifecycle)
				if err != nil {
					return false, err
				}
			}
			if len(legacyJSON(request)) > maxTransport {
				return false, errors.New("native trace event exceeds transport limit")
			}
			if err = checkDeadline(o.Deadline); err != nil {
				return false, err
			}
			response, err := requestJSON("POST", o.URL, o.Credential, request)
			if err != nil {
				recovered, recoveryErr := reconcileConflict(ledger, o.Host, *event, err)
				if !recovered {
					return false, recoveryErr
				}
				return true, ledger.write()
			}
			if err = validateAcknowledgment(response, request, o.Policy, *event); err != nil {
				return false, err
			}
			if err = ledger.record(*event, event.End); err != nil {
				return false, err
			}
			if err = ledger.write(); err != nil {
				return false, err
			}
			counts.Batches++
			counts.Chunks++
			counts.Unparsed += number(response["unparsed_record_count"])
			return true, nil
		}()
		if err != nil {
			return counts, err
		}
		if !more {
			break
		}
	}
	return counts, nil
}
func collectBudget() time.Duration {
	if value := os.Getenv("PROMPTLESS_HOST_RUNTIME_COLLECT_DEADLINE_SECONDS"); value != "" {
		if seconds, err := strconv.ParseFloat(value, 64); err == nil && seconds >= 0 && seconds < 1e8 {
			return time.Duration(seconds * float64(time.Second))
		}
	}
	return 25 * time.Second
}
func collect(h Host, lifecycle string, context HookContext, active, quiet bool, out *output) (bool, error) {
	m := metadata(pluginRoot(), h)
	enrollmentMetadata := metadata(pluginRoot(), h.enrollment())
	c, err := enrollmentContext(enrollmentMetadata)
	if err != nil {
		return false, err
	}
	credential, err := cachedCredential(c)
	if err != nil {
		return false, err
	}
	if credential == nil {
		out.emit(object{"status": "trace_upload_skipped", "reason": "not_enrolled", "host": h}, quiet)
		return false, nil
	}
	signed, err := requestJSON("GET", c.Worker+"/v0/host-enrollment/policy?target="+string(h.enrollment()), credential, nil)
	if authError(err) {
		if err = forgetCredential(c); err != nil {
			return false, err
		}
		out.emit(object{"status": "trace_upload_skipped", "reason": "credential_rejected", "host": h}, quiet)
		return false, nil
	}
	if err != nil {
		return false, err
	}
	policy, err := validatePolicy(signed, h.enrollment())
	if err != nil {
		return false, err
	}
	if requiresNewer(policy.RequiredVersion) {
		out.emit(object{"status": "blocked", "reason": "bootstrap_upgrade_required", "host": h}, quiet)
		return false, nil
	}
	// Cursor journal export rewrites session identity for transcript routing only.
	hookContext, hookLifecycle := context, lifecycle
	exportComplete := true
	if h == Cursor {
		context, exportComplete, err = prepareCursorJournals(context, lifecycle)
		if err != nil {
			return false, err
		}
		lifecycle = ""
		active = true
	}
	current := context.paths(lifecycle)
	options := uploadOptions{URL: c.Worker + "/v0/traces/batches?target=" + string(h), Credential: credential, Host: h, Metadata: m, Policy: policy, Lifecycle: lifecycle, Context: context, Ledger: scopedLedger(ledgerPath(), c.Worker, c.DeploymentID, c.HostID, h), Deadline: time.Now().Add(25 * time.Second), Limit: 1}
	total, err := uploadPaths(current, options)
	if err != nil && !errors.Is(err, errDeadline) {
		return false, err
	}
	exceeded := errors.Is(err, errDeadline)
	options.Limit = 0
	options.Deadline = time.Now().Add(collectBudget())
	contextOptions := options
	contextOptions.Context, contextOptions.Lifecycle = hookContext, hookLifecycle
	contextUploaded, contextErr := uploadSessionContext(pluginRoot(), contextOptions)
	if authError(contextErr) {
		return false, contextErr
	}
	if contextUploaded {
		total.Batches++
	}
	if !exceeded {
		counts, e := uploadPaths(current, options)
		total.add(counts)
		err = e
		if err != nil && !errors.Is(err, errDeadline) {
			return false, err
		}
		exceeded = errors.Is(err, errDeadline)
	}
	if !exceeded {
		paths, complete := scanSources(h, options.Deadline, active)
		exceeded = !complete
		seen := map[string]bool{}
		for _, p := range current {
			seen[p] = true
		}
		idle := []string{}
		for _, p := range paths {
			if !seen[p] {
				idle = append(idle, p)
				seen[p] = true
			}
		}
		if len(current) == 0 && len(idle) == 0 && complete && policy.ContextVersion == 0 {
			out.emit(object{"status": "trace_upload_skipped", "reason": "no_sources", "host": h}, quiet)
			return false, nil
		}
		if !exceeded {
			counts, e := uploadPaths(idle, options)
			total.add(counts)
			err = e
			if err != nil && !errors.Is(err, errDeadline) {
				return false, err
			}
			exceeded = errors.Is(err, errDeadline)
		}
	}
	complete := !exceeded && len(total.Unreadable) == 0 && exportComplete && contextErr == nil
	status := "trace_upload_complete"
	if !complete {
		status = "trace_upload_partial"
	}
	payload := object{"status": status, "host": h, "batch_count": total.Batches, "chunk_count": total.Chunks, "unparsed_record_count": total.Unparsed}
	if contextErr != nil {
		payload["context_upload_pending"] = true
	}
	if len(total.Unreadable) > 0 {
		payload["unreadable_source_count"] = len(total.Unreadable)
	}
	if exceeded {
		payload["reason"] = "collection_deadline_exceeded"
	}
	out.emit(payload, quiet)
	return complete, nil
}
func uniquePaths(paths []string) []string {
	seen := map[string]bool{}
	result := []string{}
	for _, p := range paths {
		p = resolvePath(p)
		if !seen[p] {
			seen[p] = true
			result = append(result, p)
		}
	}
	return result
}
func childPath(parent, child string) bool {
	relative, err := filepath.Rel(parent, child)
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)) && !filepath.IsAbs(relative)
}
