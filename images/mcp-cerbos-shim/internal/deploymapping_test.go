package config

import (
	"os"
	"path/filepath"
	"testing"
)

// TestShippedMappingsParse ensures every mapping YAML we ship (the example and
// the deploy ConfigMap source) still parses + structurally validates after
// yamlfix reformatting. Guards against a formatter breaking runtime config.
func TestShippedMappingsParse(t *testing.T) {
	candidates := []string{
		filepath.Join("..", "mapping.example.yaml"),
		filepath.Join("..", "..", "..", "infrastructure", "controllers", "mcp-cerbos-shim", "mapping.yaml"),
	}
	checked := 0
	for _, p := range candidates {
		if _, err := os.Stat(p); err != nil {
			continue // path not present from this working dir; skip
		}
		checked++
		if _, err := Load(p); err != nil {
			t.Errorf("%s failed to parse/validate: %v", p, err)
		}
	}
	if checked == 0 {
		t.Skip("no shipped mapping files reachable from test working dir")
	}
}
