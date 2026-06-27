// Package k8s provides a client for interacting with the Kubernetes API.
package k8s

import (
	"sync"
)

// ClientFactory manages a cache of Kubernetes clients keyed by context name.
type ClientFactory struct {
	cache sync.Map // key: context string → *Client
}

// NewClientFactory creates a new ClientFactory.
func NewClientFactory() *ClientFactory {
	return &ClientFactory{}
}

// GetOrCreate returns a cached client for the given context, or builds one.
// It uses BuildKubernetesConfig with a ConfigOverrides that pins CurrentContext.
func (f *ClientFactory) GetOrCreate(contextName string) (*Client, error) {
	// Check cache first
	if cached, ok := f.cache.Load(contextName); ok {
		return cached.(*Client), nil
	}

	// Cache miss; create new client
	client, err := NewClient("", contextName)
	if err != nil {
		return nil, err
	}

	// Store in cache (use LoadOrStore to handle concurrent creation)
	actual, _ := f.cache.LoadOrStore(contextName, client)
	return actual.(*Client), nil
}
