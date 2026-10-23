package runtime

import "sort"

func statusPayload(h Host) (object, error) {
	s, err := loadState(statePath())
	if err != nil {
		return nil, err
	}
	target := h.enrollment()
	credentials := []object{}
	for _, value := range obj(s["credentials"]) {
		c := obj(value)
		if str(c["target"]) != string(target) {
			continue
		}
		if _, ok := c["value"].(string); !ok {
			continue
		}
		summary := object{}
		for _, key := range []string{"credential_id", "deployment_instance_id", "worker_base_url", "updated_at"} {
			summary[key] = nullable(str(c[key]))
		}
		credentials = append(credentials, summary)
	}
	sort.Slice(credentials, func(i, j int) bool { return str(credentials[i]["updated_at"]) < str(credentials[j]["updated_at"]) })
	pending, legacy := 0, 0
	for _, value := range obj(s["pending_enrollments"]) {
		targetValue, ok := obj(value)["target"].(string)
		if !ok {
			legacy++
		} else if targetValue == string(target) {
			pending++
		}
	}
	return object{"status": "ok", "host": h, "runtime": runtimeInfo(), "state": object{"path": statePath(), "exists": exists(statePath()), "host_instance_id": nullable(str(s["host_instance_id"])), "credential_count": len(credentials), "credentials": credentials, "pending_enrollment_count": pending, "legacy_pending_enrollment_count": legacy, "last_seen_plugin_version": nullable(str(obj(s["last_seen_plugin_versions"])[string(target)]))}, "config": configStatus(h)}, nil
}
func resetState(h Host) (object, error) {
	credentials, pending := 0, 0
	target := h.enrollment()
	matches := func(t string) bool { return t == string(target) || (target == Claude && t == string(ClaudeDesktop)) }
	err := editState(func(s object) error {
		creds := obj(s["credentials"])
		for k, v := range creds {
			if matches(str(obj(v)["target"])) {
				delete(creds, k)
				credentials++
			}
		}
		s["credentials"] = creds
		p := obj(s["pending_enrollments"])
		for k, v := range p {
			t, ok := obj(v)["target"].(string)
			if !ok || matches(t) {
				delete(p, k)
				pending++
			}
		}
		s["pending_enrollments"] = p
		for _, key := range []string{firstShownKey, firstPendingKey} {
			m := obj(s[key])
			for t := range m {
				if matches(t) {
					delete(m, t)
					s[key] = m
				}
			}
		}
		return nil
	})
	return object{"status": "reset", "host": h, "credentials_removed": credentials, "pending_enrollments_removed": pending}, err
}
