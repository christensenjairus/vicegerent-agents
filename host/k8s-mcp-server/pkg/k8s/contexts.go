// Package k8s provides a client for interacting with the Kubernetes API.
package k8s

import (
	"fmt"
	"os"
	"path/filepath"

	"k8s.io/client-go/tools/clientcmd"
	"k8s.io/client-go/util/homedir"
)

// ContextInfo holds the relevant fields for a single kubeconfig context.
type ContextInfo struct {
	Name       string `json:"name"`
	Cluster    string `json:"cluster"`
	User       string `json:"user"`
	Namespace  string `json:"namespace,omitempty"`
	IsCurrent  bool   `json:"isCurrent"`
}

// ContextsResult is the top-level response for ListContexts.
type ContextsResult struct {
	CurrentContext string        `json:"currentContext"`
	Contexts       []ContextInfo `json:"contexts"`
}

// ListContexts loads the kubeconfig at kubeconfigPath (or the default path when
// empty) and returns all contexts defined in it.
func ListContexts(kubeconfigPath string) (*ContextsResult, error) {
	kubeconfig := resolveKubeconfigPath(kubeconfigPath)

	loadingRules := &clientcmd.ClientConfigLoadingRules{ExplicitPath: kubeconfig}
	config, err := loadingRules.Load()
	if err != nil {
		return nil, fmt.Errorf("failed to load kubeconfig: %w", err)
	}

	result := &ContextsResult{
		CurrentContext: config.CurrentContext,
		Contexts:       make([]ContextInfo, 0, len(config.Contexts)),
	}

	for name, ctx := range config.Contexts {
		ns := ""
		if ctx.Namespace != "" {
			ns = ctx.Namespace
		}
		result.Contexts = append(result.Contexts, ContextInfo{
			Name:      name,
			Cluster:   ctx.Cluster,
			User:      ctx.AuthInfo,
			Namespace: ns,
			IsCurrent: name == config.CurrentContext,
		})
	}

	return result, nil
}

// resolveKubeconfigPath returns the kubeconfig path to use, applying the same
// priority order as BuildKubernetesConfig (explicit arg → KUBECONFIG env → ~/.kube/config).
func resolveKubeconfigPath(kubeconfigPath string) string {
	if kubeconfigPath != "" {
		return kubeconfigPath
	}
	if kubeconfigEnv := os.Getenv("KUBECONFIG"); kubeconfigEnv != "" {
		return kubeconfigEnv
	}
	if home := homedir.HomeDir(); home != "" {
		return filepath.Join(home, ".kube", "config")
	}
	return ""
}
