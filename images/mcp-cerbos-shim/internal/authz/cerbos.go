// Package authz wraps the Cerbos PDP client behind a small interface so the
// server can be unit-tested without a live PDP.
package authz

import (
	"context"
	"fmt"

	"github.com/cerbos/cerbos-sdk-go/cerbos"
)

// Decider answers "is this principal allowed this action on this resource?".
type Decider interface {
	IsAllowed(ctx context.Context, principalID string, roles []string,
		resourceType, resourceID string, attr map[string]string, action string) (bool, error)
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
// boolean verdict for the action.
func (cc *CerbosClient) IsAllowed(ctx context.Context, principalID string, roles []string,
	resourceType, resourceID string, attr map[string]string, action string) (bool, error) {

	principal := cerbos.NewPrincipal(principalID, roles...)
	resource := cerbos.NewResource(resourceType, resourceID)
	for k, v := range attr {
		resource = resource.WithAttr(k, v)
	}
	return cc.c.IsAllowed(ctx, principal, resource, action)
}
