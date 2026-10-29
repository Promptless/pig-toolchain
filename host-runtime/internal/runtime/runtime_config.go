package runtime

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

const runtimeConfigName = "hub.runtime-config.json"

var httpsOriginPattern = regexp.MustCompile(`^https://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?/?$`)

func parseRuntimeConfig(data []byte) (map[string]string, error) {
	// Decode keys explicitly: encoding/json otherwise silently accepts duplicates.
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	token, err := d.Token()
	if err != nil || token != json.Delim('{') {
		return nil, errors.New("must contain a JSON object")
	}
	seen := map[string]bool{}
	endpoints := map[string]string{}
	for d.More() {
		token, err := d.Token()
		if err != nil {
			return nil, errors.New("invalid JSON")
		}
		key, ok := token.(string)
		if !ok || seen[key] {
			return nil, errors.New("duplicate configuration key")
		}
		seen[key] = true
		var value any
		if err := d.Decode(&value); err != nil {
			return nil, errors.New("invalid JSON")
		}
		switch key {
		case "schema_version":
			if value != json.Number("1") {
				return nil, errors.New("requires schema_version 1")
			}
		case "worker_base_url", "dashboard_base_url":
			raw, ok := value.(string)
			u, err := validateURL(raw, false)
			if !ok || !httpsOriginPattern.MatchString(raw) || err != nil || u.Scheme != "https" || (u.Path != "" && u.Path != "/") {
				return nil, fmt.Errorf("%s must be an HTTPS origin without credentials, path, query, or fragment", key)
			}
			endpoints[key] = strings.TrimSuffix(raw, "/")
		default:
			return nil, errors.New("contains unsupported fields")
		}
	}
	if _, err := d.Token(); err != nil {
		return nil, errors.New("invalid JSON")
	}
	if d.Decode(new(any)) != io.EOF {
		return nil, errors.New("unexpected data after configuration")
	}
	if !seen["schema_version"] {
		return nil, errors.New("requires schema_version 1")
	}
	return endpoints, nil
}

func runtimeConfigBytes(root string, retained bool) ([]byte, error) {
	if root == "" {
		return []byte(`{"schema_version":1}`), nil
	}
	path := filepath.Join(root, runtimeConfigName)
	if retained {
		// Use retained config only when the entire plugin cache has disappeared.
		// An existing plugin with no config uses defaults, not an older destination.
		if _, err := os.Stat(root); errors.Is(err, os.ErrNotExist) {
			path = hookMetadataPath(root) + ".config"
		}
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		if _, linkErr := os.Lstat(path); errors.Is(linkErr, os.ErrNotExist) {
			return []byte(`{"schema_version":1}`), nil
		}
	}
	if err != nil {
		return nil, fmt.Errorf("%s could not be read", runtimeConfigName)
	}
	if _, err = parseRuntimeConfig(data); err != nil {
		return nil, fmt.Errorf("%s: %w", runtimeConfigName, err)
	}
	return data, nil
}
func configuredBaseURL(key, env, fallback string) (string, error) {
	data, err := runtimeConfigBytes(pluginRoot(), true)
	if err != nil {
		return "", err
	}
	config, err := parseRuntimeConfig(data)
	if err != nil {
		return "", err
	}
	if configured := config[key]; configured != "" {
		fallback = configured
	}
	return baseURL(env, fallback)
}

// Infer only our installed runtime layout. Never discover configuration from cwd.
func installedPluginRoot(exe string) string {
	dir := filepath.Dir(exe)
	for i := 0; i < 4; i++ {
		if filepath.Base(dir) == "runtime" {
			root := filepath.Dir(dir)
			for _, name := range []string{runtimeConfigName, "hub.managed-runtimes.json"} {
				if _, err := os.Lstat(filepath.Join(root, name)); err == nil {
					return root
				}
			}
			return ""
		}
		dir = filepath.Dir(dir)
	}
	return ""
}
