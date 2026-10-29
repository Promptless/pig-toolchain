package runtime

import (
	"errors"
	"net/url"
	"strings"
	"time"

	"github.com/google/uuid"
)

func hostedAPIURL() (string, error) {
	return configuredBaseURL("hosted_api_base_url", "PROMPTLESS_HOSTED_API_BASE_URL", "https://api.gopromptless.ai")
}
func devicePollURL(api, id string) (string, error) {
	parsed, err := uuid.Parse(id)
	if err != nil {
		return "", errors.New("device enrollment response has invalid session_id")
	}
	return api + "/v1/instruction-hub/host-enrollments/sessions/" + parsed.String() + "/poll", nil
}
func validateApprovalURL(raw, dashboard string) error {
	for _, c := range raw {
		if c < 32 || (c >= 127 && c <= 159) {
			return errors.New("approval URL contains control characters")
		}
	}
	u, err := validateURL(raw, true)
	d, dashboardErr := url.Parse(dashboard)
	if err != nil || dashboardErr != nil || !sameOrigin(u, d) || u.Path != "/pig/enroll" || strings.Contains(raw, "#") {
		return errors.New("approval URL did not match the configured dashboard enrollment route")
	}
	return validateTransport(u)
}
func validateDeviceSession(c EnrollmentContext, s EnrollmentSession) error {
	api, err := hostedAPIURL()
	if err != nil {
		return err
	}
	if s.HostedAPI == "" || s.HostedAPI != api || s.ApprovalURL == "" {
		return errors.New("pending device enrollment endpoint changed or is invalid; reset host enrollment before retrying")
	}
	poll, err := devicePollURL(api, s.ID)
	if err != nil {
		return err
	}
	if s.PollURL != poll {
		return errors.New("pending device enrollment poll URL did not match configured API")
	}
	return validateApprovalURL(s.ApprovalURL, c.Dashboard)
}
func createDeviceEnrollment(c EnrollmentContext) (*EnrollmentSession, error) {
	api, err := hostedAPIURL()
	if err != nil {
		return nil, err
	}
	response, err := requestJSONRedirects("POST", api+"/v1/instruction-hub/host-enrollments/device-sessions", nil, object{
		"deployment_instance_id": c.DeploymentID, "target": c.Metadata.Target,
		"plugin_id": c.Metadata.PluginID, "plugin_version": c.Metadata.PluginVersion,
		"bootstrap_version": c.Metadata.BootstrapVersion,
		"toolchain_version": c.Metadata.ToolchainVersion, "host_instance_id": c.HostID,
		"host_label": c.Label, "host_platform": c.Platform,
	}, false)
	if err != nil {
		var httpErr *HTTPError
		if errors.As(err, &httpErr) {
			switch httpErr.Status {
			case 404, 405:
				return nil, errors.New("hosted API does not support device enrollment or recognize this deployment; check hosted_api_base_url and deploy the device-enrollment API before retrying")
			case 429:
				return nil, errors.New("too many pending device enrollment requests; wait before retrying")
			}
		}
		return nil, err
	}
	s := &EnrollmentSession{ID: str(response["session_id"]), DeviceCode: str(response["device_code"]), DeploymentID: c.DeploymentID, HostedAPI: api, ApprovalURL: str(response["approval_url"])}
	if !nonempty(s.DeviceCode) {
		return nil, errors.New("device enrollment response missing device_code")
	}
	s.PollURL, err = devicePollURL(api, s.ID)
	if err != nil {
		return nil, err
	}
	if err = validateApprovalURL(s.ApprovalURL, c.Dashboard); err != nil {
		return nil, err
	}
	s.ExpiresAt, err = time.Parse(time.RFC3339Nano, str(response["expires_at"]))
	lifetime := time.Until(s.ExpiresAt)
	if err != nil || lifetime <= 0 || lifetime > 20*time.Minute {
		return nil, errors.New("device enrollment expiry exceeds its 15-minute lifetime and clock allowance")
	}
	interval, ok := integer(response["poll_interval_seconds"])
	if !ok || interval < 1 || interval > 30 {
		return nil, errors.New("device enrollment poll interval must be between 1 and 30 seconds")
	}
	s.PollInterval = int(interval)
	return s, nil
}
