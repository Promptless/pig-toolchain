package runtime

import (
	"testing"
	"time"
)

func TestLeanPolicySchemaAndEligibility(t *testing.T) {
	valid := func() object {
		return object{"policy": object{"schema_version": 2, "policy_version": 3, "expires_at": time.Now().Add(time.Hour).Format(time.RFC3339), "enabled_hosts": []any{"codex"}}}
	}
	if p, err := validatePolicy(valid(), Codex); err != nil || p.Version != 3 {
		t.Fatal(p, err)
	}
	for _, test := range []struct {
		key   string
		value any
	}{
		{"schema_version", 1}, {"schema_version", true}, {"schema_version", "2"},
		{"expires_at", time.Now().Add(-time.Hour).Format(time.RFC3339)},
		{"enabled_hosts", []any{"claude"}}, {"policy_version", true}, {"required_bootstrap_version", 2},
	} {
		payload := valid()
		obj(payload["policy"])[test.key] = test.value
		if _, err := validatePolicy(payload, Codex); err == nil {
			t.Fatalf("accepted %s=%v", test.key, test.value)
		}
	}
	payload := valid()
	obj(payload["policy"])["required_bootstrap_version"] = "99.0.0"
	p, err := validatePolicy(payload, Codex)
	if err != nil || !requiresNewer(p.RequiredVersion) {
		t.Fatal("minimum version gate lost", p, err)
	}
}
