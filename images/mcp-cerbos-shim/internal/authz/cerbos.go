// Package authz wraps the Cerbos PDP client behind a small interface so the
// server can be unit-tested without a live PDP.
package authz

import (
	"context"
	"fmt"

	"github.com/cerbos/cerbos-sdk-go/cerbos"
)

// Decider answers "is this principal allowed this action on this resource?"
// and, on deny, returns the policy-authored reason if the matched deny rule
// carries a Cerbos `output` block. reason is empty when Cerbos allows, or
// when the matched rule (allow or deny) has no output configured.
type Decider interface {
	IsAllowed(ctx context.Context, principalID string, roles []string,
		resourceType, resourceID string, attr map[string]any, action string) (allowed bool, reason string, err error)
}

// CerbosClient is the production Decider backed by a Cerbos PDP over gRPC.
type CerbosClient struct {
	c *cerbos.GRPCClient
}

// New dials the Cerbos PDP. addr is host:port (e.g. "cerbos.cerbos.svc:3593").
// TLS options are passed through; in-cluster same-namespace traffic may use
// WithPlaintext during bring-up and mTLS later.
func New(addr string, opts ...cerbos.Opt) (*CerbosClient, error) {
	c, err := cerbos.New(addr, opts...)
	if err != nil {
		return nil, fmt.Errorf("dial cerbos %q: %w", addr, err)
	}
	return &CerbosClient{c: c}, nil
}

// IsAllowed builds a single-resource CheckResources request and returns the
// boolean verdict for the action, plus the policy-authored output string for
// whichever rule matched (deny or allow), if that rule has an `output` block.
//
// We use CheckResources (not the simpler IsAllowed helper) because only
// CheckResources's response carries per-rule `outputs`; the SDK's IsAllowed
// convenience method discards them. See:
// https://raw.githubusercontent.com/cerbos/cerbos-sdk-go/v0.2.0/cerbos/grpc.go
// (GRPCClient.CheckResources vs GRPCClient.IsAllowed) and
// https://raw.githubusercontent.com/cerbos/cerbos-sdk-go/v0.2.0/cerbos/model.go
// (ResourceResult.Output, keyed by the output's `src`, i.e.
// "resource.<kind>.v<version>#<rule-name>").
func (cc *CerbosClient) IsAllowed(ctx context.Context, principalID string, roles []string,
	resourceType, resourceID string, attr map[string]any, action string) (bool, string, error) {

	principal := cerbos.NewPrincipal(principalID, roles...)
	resource := cerbos.NewResource(resourceType, resourceID)
	for k, v := range attr {
		resource = resource.WithAttr(k, v)
	}

	batch := cerbos.NewResourceBatch().Add(resource, action)
	resp, err := cc.c.CheckResources(ctx, principal, batch)
	if err != nil {
		return false, "", err
	}

	result := resp.GetResource(resourceID, cerbos.MatchResourceKind(resourceType))
	if err := result.Err(); err != nil {
		return false, "", err
	}

	allowed := result.IsAllowed(action)

	// The output key is "resource.<kind>.v<version>#<rule-name>", but we don't
	// know the rule name ahead of time (any deny rule in the policy could have
	// matched) or the policy version at the call site. Cerbos's own
	// ResourceResult.Output(key) requires the exact src key, which defeats the
	// purpose here — so we walk the raw Outputs list directly (same
	// underlying field ResourceResult.buildOutputMap reads, just without
	// requiring the caller to already know the winning rule name).
	// engine.v1.OutputEntry (this repo's pinned genpb) carries only Src/Val,
	// no per-entry action — but this call always requests exactly one
	// resource+action pair (see the single .Add(resource, action) above), so
	// every entry in GetOutputs() already belongs to that one action; no
	// action filter is needed or possible. Take the first non-empty string
	// output (in practice exactly one deny rule can match per resource,
	// since Cerbos DENY-overrides-ALLOW semantics stop at the first
	// matching deny, so there's only ever one to find).
	var reason string
	for _, o := range result.GetOutputs() {
		if s := o.GetVal().GetStringValue(); s != "" {
			reason = s
			break
		}
	}

	return allowed, reason, nil
}
