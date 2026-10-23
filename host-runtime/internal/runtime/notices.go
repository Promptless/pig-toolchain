package runtime

import "fmt"

const firstShownKey = "first_enrollment_success_shown_at_by_target"
const firstPendingKey = "pending_first_enrollment_success_by_target"
const welcomeShownKey = "internal_promptless_welcome_shown_at_by_version"

type pluginUpdate struct {
	Target          Host
	Version, Notice string
}

func pendingUpdate(m Metadata) *pluginUpdate {
	if m.PluginVersion == "unknown" {
		return nil
	}
	s, err := loadState(statePath())
	if err != nil {
		return nil
	}
	last := str(obj(s["last_seen_plugin_versions"])[string(m.Target)])
	if last == m.PluginVersion {
		return nil
	}
	p := &pluginUpdate{Target: m.Target, Version: m.PluginVersion}
	if last != "" {
		p.Notice = fmt.Sprintf("Promptless Instruction Hub updated to v%s (was v%s).", m.PluginVersion, last)
	}
	return p
}
func recordUpdate(p *pluginUpdate) {
	if p == nil {
		return
	}
	_ = editState(func(s object) error {
		versions := obj(s["last_seen_plugin_versions"])
		versions[string(p.Target)] = p.Version
		s["last_seen_plugin_versions"] = versions
		return nil
	})
}
func firstSuccessMessage(h Host, status string) string {
	if status != "configured" && status != "needs_restart" {
		return ""
	}
	name := hostName(h)
	if name == "" {
		return ""
	}
	if status == "needs_restart" {
		return "Promptless Instruction Governance telemetry is now enrolled for " + name + "."
	}
	return "Promptless Instruction Governance telemetry is now active for " + name + ". No restart or plugin reload is needed."
}
func deferSuccess(h Host, status string) {
	if firstSuccessMessage(h, status) == "" {
		return
	}
	_ = editState(func(s object) error {
		if str(obj(s[firstShownKey])[string(h)]) != "" {
			return nil
		}
		pending := obj(s[firstPendingKey])
		pending[string(h)] = status
		s[firstPendingKey] = pending
		return nil
	})
}
func claimSuccess(h Host, status string) (string, string) {
	message := ""
	err := editState(func(s object) error {
		pending := obj(s[firstPendingKey])
		if status == "" {
			status = str(pending[string(h)])
		}
		candidate := firstSuccessMessage(h, status)
		if candidate == "" {
			return nil
		}
		shown := obj(s[firstShownKey])
		if str(shown[string(h)]) != "" {
			return nil
		}
		shown[string(h)] = nowISO()
		delete(pending, string(h))
		s[firstShownKey] = shown
		s[firstPendingKey] = pending
		message = candidate
		return nil
	})
	if err != nil {
		return "", ""
	}
	return status, message
}
func claimWelcome(c *Credential, quiet bool, version string, updated bool) string {
	if quiet || version == "" || version == "unknown" {
		return ""
	}
	claimed := false
	err := editState(func(s object) error {
		internal := c != nil && c.Internal
		for _, value := range obj(s["credentials"]) {
			internal = internal || obj(value)["is_internal_promptless_user"] == true
		}
		if !internal {
			return nil
		}
		shown := obj(s[welcomeShownKey])
		if str(shown[version]) != "" {
			return nil
		}
		at := nowISO()
		shown[version] = at
		s[welcomeShownKey] = shown
		s["internal_promptless_welcome_shown_at"] = at
		claimed = true
		return nil
	})
	if err != nil || !claimed {
		return ""
	}
	label := "version"
	if updated {
		label = "version updated"
	}
	return "            ,-,------,\n          _ \\(\\(_,--'\n     <`--'\\>/(/(__\n     /. .  `'` '  \\\n    (`')  ,        @\n     `-._,        /\n        )-)_/--( > \n       ''''  ''''\nwelcome promptless pigfooder.\n" + label + ": v" + version
}
