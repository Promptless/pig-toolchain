package runtime

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"time"
)

const managedBegin = "# BEGIN PROMPTLESS MANAGED HOST ENROLLMENT"
const managedEnd = "# END PROMPTLESS MANAGED HOST ENROLLMENT"
const claudeMarker = "PROMPTLESS_MANAGED_HOST_ENROLLMENT"

var managedBlock = regexp.MustCompile(`(?s)` + regexp.QuoteMeta(managedBegin) + `.*?` + regexp.QuoteMeta(managedEnd) + `\n?`)

func envPath(key, defaultPath string) string {
	if v := os.Getenv(key); nonempty(v) {
		return expandPath(v)
	}
	return defaultPath
}
func appConfigRoot(app string) string {
	switch runtime.GOOS {
	case "darwin":
		return homePath("Library", "Application Support", app)
	case "windows":
		return filepath.Join(envPath("APPDATA", homePath("AppData", "Roaming")), app)
	default:
		return filepath.Join(envPath("XDG_CONFIG_HOME", homePath(".config")), app)
	}
}
func desktopRoots() []string {
	base := appConfigRoot("Claude")
	return []string{filepath.Join(base, "local-agent-mode-sessions"), filepath.Join(base, "claude-code-sessions")}
}
func claudeConfigRoot() string {
	return envPath("CLAUDE_CONFIG_DIR", homePath(".claude"))
}
func nativeRoots(h Host) []string {
	switch h {
	case Cursor:
		return []string{filepath.Join(cursorSpool(), "journals")}
	case Claude:
		return []string{filepath.Join(claudeConfigRoot(), "projects")}
	case ClaudeDesktop:
		return desktopRoots()
	default:
		root := envPath("CODEX_HOME", homePath(".codex"))
		return []string{filepath.Join(root, "sessions"), filepath.Join(root, "archived_sessions")}
	}
}
func configPath(h Host) string {
	switch h {
	case Codex:
		return filepath.Join(envPath("CODEX_HOME", homePath(".codex")), "config.toml")
	case Claude:
		return filepath.Join(claudeConfigRoot(), "settings.json")
	case Cursor:
		return cursorDBPath()
	default:
		return "Claude Desktop local audit stores"
	}
}
func malformedBlocks(s string) bool {
	a, b := strings.Count(s, managedBegin), strings.Count(s, managedEnd)
	return a != b || len(managedBlock.FindAllString(s, -1)) != a
}
func configStatus(h Host) object {
	path := configPath(h)
	result := object{"path": path, "exists": exists(path), "managed_config_detected": false}
	if h == ClaudeDesktop {
		roots := desktopRoots()
		present := false
		for _, root := range roots {
			present = present || exists(root)
		}
		result["paths"] = roots
		result["exists"] = present
		return result
	}
	if h == Cursor {
		result["exists"] = isFile(path)
		return result
	}
	if !exists(path) {
		return result
	}
	b, err := os.ReadFile(path)
	if err != nil {
		result["read_error"] = redactText(err.Error())
		return result
	}
	if h == Codex {
		bad := malformedBlocks(string(b))
		result["malformed_managed_config"] = bad
		result["managed_config_detected"] = !bad && managedBlock.Match(b)
	} else {
		settings, err := decodeObject(b)
		if err != nil {
			result["read_error"] = err.Error()
		} else {
			result["managed_config_detected"] = obj(settings["env"])[claudeMarker] == "1"
		}
	}
	return result
}
func effectiveConfig(h Host, configured, managed bool, endpoint, ledger string) object {
	var upload any
	if configured {
		upload = endpoint
	}
	result := object{"host": h, "configured": configured, "trace_upload_endpoint": upload, "native_root_count": len(nativeRoots(h)), "source_ledger_path": ledger, "managed_config_detected": managed}
	result["config_hash"] = hash(legacyJSON(result))
	return result
}
func blockedConfig(h Host, kind, message string, details object, endpoint, ledger string) ConfigResult {
	return ConfigResult{"blocked", false, effectiveConfig(h, false, false, endpoint, ledger), []object{{"kind": kind, "message": message, "repaired": false, "details": details}}}
}
func ensureConfig(h Host, endpoint, ledger string) (ConfigResult, error) {
	changed := false
	path := configPath(h)
	if (h == Codex || h == Claude) && exists(path) {
		original, err := os.ReadFile(path)
		if err != nil {
			return ConfigResult{}, err
		}
		updated := original
		if h == Codex {
			if malformedBlocks(string(original)) {
				return blockedConfig(h, "manual_config_required", "Existing Codex managed telemetry markers are malformed", object{"path": path}, endpoint, ledger), nil
			}
			updated = managedBlock.ReplaceAll(original, nil)
		} else {
			settings, err := decodeObject(original)
			if err != nil {
				return ConfigResult{}, fmt.Errorf("Claude settings: %w", err)
			}
			env := obj(settings["env"])
			if env[claudeMarker] == "1" {
				for key := range env {
					if key == claudeMarker || strings.HasPrefix(key, "OTEL_") || key == "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA" || key == "CLAUDE_CODE_ENABLE_TELEMETRY" || key == "ENABLE_BETA_TRACING_DETAILED" || key == "BETA_TRACING_ENDPOINT" {
						delete(env, key)
					}
				}
				if len(env) == 0 {
					delete(settings, "env")
				}
				updated, _ = json.MarshalIndent(settings, "", "  ")
				updated = append(updated, '\n')
			}
		}
		changed = string(original) != string(updated)
		if changed {
			backup := path + "." + time.Now().UTC().Format("20060102150405.000000") + ".bak"
			if err = atomicWrite(backup, original); err != nil {
				return ConfigResult{}, err
			}
			if err = atomicWrite(path, updated); err != nil {
				return ConfigResult{}, err
			}
		}
	}
	r := ConfigResult{"configured", changed, effectiveConfig(h, true, changed, endpoint, ledger), []object{}}
	if changed {
		r.Status = "needs_restart"
		r.Drift = append(r.Drift, object{"kind": "removed_managed_config", "message": "Removed managed " + string(h) + " telemetry config", "repaired": true, "details": object{"path": path}})
	}
	return r, nil
}
func scanSources(h Host, deadline time.Time, active bool) ([]string, bool) {
	paths := []string{}
	seenDirs, seenFiles := map[string]bool{}, map[string]bool{}
	var walk func(string) error
	walk = func(path string) error {
		if err := checkDeadline(deadline); err != nil {
			return err
		}
		// Desktop users may relocate transcript stores using symlinks. Resolve
		// directories before descending so aliases and cycles are visited once.
		info, err := os.Stat(path)
		if err != nil {
			return nil
		}
		resolved := resolvePath(path)
		if info.IsDir() {
			if seenDirs[resolved] {
				return nil
			}
			seenDirs[resolved] = true
			entries, err := os.ReadDir(resolved)
			if err != nil {
				return nil
			}
			for _, entry := range entries {
				if err := walk(filepath.Join(resolved, entry.Name())); err != nil {
					return err
				}
			}
			return nil
		}
		if !info.Mode().IsRegular() || seenFiles[resolved] || filepath.Ext(path) != ".jsonl" || (h == ClaudeDesktop && filepath.Base(path) != "audit.jsonl") {
			return nil
		}
		if active || time.Since(info.ModTime()) >= 12*time.Hour {
			paths = append(paths, resolved)
			seenFiles[resolved] = true
		}
		return nil
	}
	for _, root := range nativeRoots(h) {
		if err := walk(root); err != nil {
			sort.Strings(paths)
			return paths, false
		}
	}
	sort.Strings(paths)
	return paths, true
}
func hasSources(h Host) bool {
	if h == Cursor {
		return isFile(cursorDBPath())
	}
	paths, _ := scanSources(h, time.Now().Add(25*time.Second), true)
	return len(paths) > 0
}
