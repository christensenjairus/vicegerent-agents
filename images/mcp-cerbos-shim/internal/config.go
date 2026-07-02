// Package config loads and validates the connector's tool-to-policy mapping.
//
// The mapping is the single source of truth for how each MCP (backend, tool)
// call is translated into a Cerbos CheckResources request. Every value under
// id/attr/attrFrom is a CEL expression, compiled and type-checked at startup so
// a malformed mapping fails the process rather than silently failing open.
package config

import (
	"bytes"
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// DefaultAction decides what happens for a method/tool the mapping does not
// explicitly handle on a given backend.
type DefaultAction string

const (
	ActionDeny  DefaultAction = "deny"
	ActionAllow DefaultAction = "allow"
)

// Mapping is the top-level config document.
type Mapping struct {
	Backends map[string]Backend `yaml:"backends"`
}

// Backend is one MCP server's policy mapping.
type Backend struct {
	// DefaultAction governs unmapped tools and unmapped request-phase methods.
	// Use "deny" unless an allow-default backend is intentional.
	DefaultAction DefaultAction `yaml:"defaultAction"`
	// Helpers names the backend-scoped CEL helper functions in scope for this
	// backend (e.g. "canonicalK8s"). A helper named here but unknown to the
	// connector is a startup error.
	Helpers []string `yaml:"helpers"`
	// Tools maps an MCP tool name to how its call is authorized.
	Tools map[string]Tool `yaml:"tools"`
}

// Tool describes how one tool call becomes a Cerbos CheckResources request.
type Tool struct {
	// ResourceType is the Cerbos resource kind (e.g. "k8s_resource").
	ResourceType string `yaml:"resourceType"`
	// Action is the Cerbos action; defaults to the tool name when empty.
	Action string `yaml:"action"`
	// ID is a CEL expression yielding the Cerbos resource id (string).
	ID string `yaml:"id"`
	// Attr is a map of attr-name -> CEL expression (string-valued).
	Attr map[string]string `yaml:"attr"`
	// AttrFrom is a single CEL expression yielding a map<string,string> that is
	// merged into Attr. Used by canonicalizing helpers (e.g. canonicalK8s).
	// When both Attr and AttrFrom are set, AttrFrom is evaluated first and Attr
	// overrides individual keys.
	AttrFrom string `yaml:"attrFrom"`
	// Force is a set of literal key/value overrides applied to the call's
	// arguments before forwarding, when Cerbos allows the call. Unlike
	// id/attr/attrFrom these are NOT CEL expressions — always-the-same
	// constants (e.g. forcing draft: true on PR creation), not derived from
	// the caller's args. Only applied on allow; a denied call is never mutated.
	Force map[string]any `yaml:"force"`
}

// Load reads and structurally validates a mapping file. CEL compilation happens
// separately in the eval package so this stays dependency-light and testable.
func Load(path string) (*Mapping, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read mapping %q: %w", path, err)
	}
	return Parse(b)
}

// Parse validates an in-memory mapping document.
func Parse(b []byte) (*Mapping, error) {
	var m Mapping
	dec := yaml.NewDecoder(bytes.NewReader(b))
	dec.KnownFields(true) // reject unknown keys: typos fail closed at startup
	if err := dec.Decode(&m); err != nil {
		return nil, fmt.Errorf("parse mapping: %w", err)
	}
	if err := m.validate(); err != nil {
		return nil, err
	}
	return &m, nil
}

func (m *Mapping) validate() error {
	if len(m.Backends) == 0 {
		return fmt.Errorf("mapping has no backends")
	}
	for name, b := range m.Backends {
		switch b.DefaultAction {
		case ActionDeny, ActionAllow:
		case "":
			return fmt.Errorf("backend %q: defaultAction is required (deny|allow)", name)
		default:
			return fmt.Errorf("backend %q: invalid defaultAction %q", name, b.DefaultAction)
		}
		for tname, t := range b.Tools {
			if t.ResourceType == "" {
				return fmt.Errorf("backend %q tool %q: resourceType is required", name, tname)
			}
			if t.ID == "" {
				return fmt.Errorf("backend %q tool %q: id expression is required", name, tname)
			}
			if len(t.Attr) == 0 && t.AttrFrom == "" {
				return fmt.Errorf("backend %q tool %q: at least one of attr/attrFrom is required", name, tname)
			}
		}
	}
	return nil
}
