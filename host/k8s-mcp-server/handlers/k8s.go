// Package handlers provides MCP tool handlers for interacting with Kubernetes.
package handlers

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/reza-gholizade/k8s-mcp-server/pkg/k8s"

	"github.com/mark3labs/mcp-go/mcp"
)

// toolErr returns a tool-level error result (isError=true, HTTP 200) rather than a
// JSON-RPC protocol error (-32603). This is the correct way to signal expected failures
// (resource not found, invalid kind, etc.) — protocol errors cause mcp-proxy-server to
// treat the server as broken and tear down the stdio connection.
func toolErr(format string, args ...any) (*mcp.CallToolResult, error) {
	return mcp.NewToolResultError(fmt.Sprintf(format, args...)), nil
}

// Helper functions for consistent parameter extraction
func getStringArg(args map[string]interface{}, key string, defaultValue string) string {
	if val, ok := args[key].(string); ok {
		return val
	}
	return defaultValue
}

func getBoolArg(args map[string]interface{}, key string, defaultValue bool) bool {
	if val, ok := args[key].(bool); ok {
		return val
	}
	return defaultValue
}

func getRequiredStringArg(args map[string]interface{}, key string) (string, error) {
	val, ok := args[key].(string)
	if !ok || val == "" {
		return "", fmt.Errorf("missing required parameter: %s", key)
	}
	return val, nil
}

// GetAPIResources returns a handler function for the getAPIResources tool.
func GetAPIResources(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		includeNamespaceScoped := getBoolArg(args, "includeNamespaceScoped", true)
		includeClusterScoped := getBoolArg(args, "includeClusterScoped", true)

		resources, err := client.GetAPIResources(ctx, includeNamespaceScoped, includeClusterScoped)
		if err != nil {
			return toolErr("failed to get API resources: %v", err)
		}

		jsonResponse, err := json.Marshal(resources)
		if err != nil {
			return toolErr("failed to serialize response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}

// ListResources returns a handler function for the listResources tool.
func ListResources(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		kind, err := getRequiredStringArg(args, "Kind")
		if err != nil {
			return toolErr("%v", err)
		}

		namespace := getStringArg(args, "namespace", "")
		labelSelector := getStringArg(args, "labelSelector", "")
		fieldSelector := getStringArg(args, "fieldSelector", "")

		resources, err := client.ListResources(ctx, kind, namespace, labelSelector, fieldSelector)
		if err != nil {
			return toolErr("failed to list resources for kind %q: %v", kind, err)
		}

		jsonResponse, err := json.Marshal(resources)
		if err != nil {
			return toolErr("failed to serialize response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}

// GetResources returns a handler function for the getResource tool.
func GetResources(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		kind, err := getRequiredStringArg(args, "kind")
		if err != nil {
			return toolErr("%v", err)
		}

		name, err := getRequiredStringArg(args, "name")
		if err != nil {
			return toolErr("%v", err)
		}

		namespace := getStringArg(args, "namespace", "")

		resource, err := client.GetResource(ctx, kind, name, namespace)
		if err != nil {
			return toolErr("failed to get resource %q of kind %q: %v", name, kind, err)
		}

		jsonResponse, err := json.Marshal(resource)
		if err != nil {
			return toolErr("failed to serialize response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}

// DescribeResources returns a handler function for the describeResource tool.
func DescribeResources(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		kind, err := getRequiredStringArg(args, "Kind")
		if err != nil {
			return toolErr("%v", err)
		}

		name, err := getRequiredStringArg(args, "name")
		if err != nil {
			return toolErr("%v", err)
		}

		namespace := getStringArg(args, "namespace", "")

		resourceDescription, err := client.DescribeResource(ctx, kind, name, namespace)
		if err != nil {
			return toolErr("failed to describe resource %q of kind %q: %v", name, kind, err)
		}

		jsonResponse, err := json.Marshal(resourceDescription)
		if err != nil {
			return toolErr("failed to serialize response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}

// GetPodsLogs returns a handler function for the getPodsLogs tool.
func GetPodsLogs(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		name, err := getRequiredStringArg(args, "Name")
		if err != nil {
			return toolErr("%v", err)
		}

		namespace, err := getRequiredStringArg(args, "namespace")
		if err != nil {
			return toolErr("%v", err)
		}

		containerName := getStringArg(args, "containerName", "")

		logs, err := client.GetPodsLogs(ctx, namespace, containerName, name)
		if err != nil {
			return toolErr("failed to get logs for pod %q: %v", name, err)
		}

		return mcp.NewToolResultText(logs), nil
	}
}

// GetNodeMetrics returns a handler function for the getNodeMetrics tool.
func GetNodeMetrics(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		name, err := getRequiredStringArg(args, "Name")
		if err != nil {
			return toolErr("%v", err)
		}

		resourceUsage, err := client.GetNodeMetrics(ctx, name)
		if err != nil {
			return toolErr("failed to get metrics for node %q: %v", name, err)
		}

		jsonResponse, err := json.Marshal(resourceUsage)
		if err != nil {
			return toolErr("failed to serialize response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}

// GetPodMetrics returns a handler function for the getPodMetrics tool.
func GetPodMetrics(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		namespace, err := getRequiredStringArg(args, "namespace")
		if err != nil {
			return toolErr("%v", err)
		}

		podName, err := getRequiredStringArg(args, "podName")
		if err != nil {
			return toolErr("%v", err)
		}

		metrics, err := client.GetPodMetrics(ctx, namespace, podName)
		if err != nil {
			return toolErr("failed to get metrics for pod %q in namespace %q: %v", podName, namespace, err)
		}

		jsonResponse, err := json.Marshal(metrics)
		if err != nil {
			return toolErr("failed to serialize metrics response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}

// GetEvents returns a handler function for the getEvents tool.
func GetEvents(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		namespace := getStringArg(args, "namespace", "")

		events, err := client.GetEvents(ctx, namespace)
		if err != nil {
			return toolErr("failed to get events: %v", err)
		}

		jsonResponse, err := json.Marshal(events)
		if err != nil {
			return toolErr("failed to serialize events response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}

// CreateOrUpdateResourceJSON returns a handler function for the createResource tool.
func CreateOrUpdateResourceJSON(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		manifest, err := getRequiredStringArg(args, "manifest")
		if err != nil {
			return toolErr("%v", err)
		}

		namespace := getStringArg(args, "namespace", "")
		kind := getStringArg(args, "kind", "")

		resource, err := client.CreateOrUpdateResourceJSON(ctx, namespace, manifest, kind)
		if err != nil {
			return toolErr("failed to create or update resource: %v", err)
		}

		jsonResponse, err := json.Marshal(resource)
		if err != nil {
			return toolErr("failed to serialize response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}

// CreateOrUpdateResourceYAML returns a handler function for the createResourceYAML tool.
func CreateOrUpdateResourceYAML(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		yamlManifest, err := getRequiredStringArg(args, "yamlManifest")
		if err != nil {
			return toolErr("%v", err)
		}

		namespace := getStringArg(args, "namespace", "")
		kind := getStringArg(args, "kind", "")

		resource, err := client.CreateOrUpdateResourceYAML(ctx, namespace, yamlManifest, kind)
		if err != nil {
			return toolErr("failed to create or update resource from YAML: %v", err)
		}

		jsonResponse, err := json.Marshal(resource)
		if err != nil {
			return toolErr("failed to serialize response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}

// DeleteResource returns a handler function for the deleteResource tool.
func DeleteResource(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		kind, err := getRequiredStringArg(args, "kind")
		if err != nil {
			return toolErr("%v", err)
		}

		name, err := getRequiredStringArg(args, "name")
		if err != nil {
			return toolErr("%v", err)
		}

		namespace := getStringArg(args, "namespace", "")

		if err := client.DeleteResource(ctx, kind, name, namespace); err != nil {
			return toolErr("failed to delete resource: %v", err)
		}

		return mcp.NewToolResultText("Resource deleted successfully"), nil
	}
}

// GetIngresses returns a handler function for the getIngresses tool.
func GetIngresses(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		host, err := getRequiredStringArg(args, "host")
		if err != nil {
			return toolErr("%v", err)
		}

		ingresses, err := client.GetIngresses(ctx, host)
		if err != nil {
			return toolErr("failed to get ingresses for host %q: %v", host, err)
		}

		jsonResponse, err := json.Marshal(ingresses)
		if err != nil {
			return toolErr("failed to serialize response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}

// RolloutRestart returns a handler function for the rolloutRestart tool.
func RolloutRestart(factory *k8s.ClientFactory) func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args, ok := request.Params.Arguments.(map[string]interface{})
		if !ok {
			return toolErr("invalid arguments type: expected map[string]interface{}")
		}

		contextName, err := getRequiredStringArg(args, "context")
		if err != nil {
			return toolErr("context is required: %v", err)
		}
		client, err := factory.GetOrCreate(contextName)
		if err != nil {
			return toolErr("failed to get client for context %q: %v", contextName, err)
		}

		kind := getStringArg(args, "kind", "")
		name := getStringArg(args, "name", "")
		namespace := getStringArg(args, "namespace", "")

		if kind == "" || name == "" || namespace == "" {
			return toolErr("kind, name, and namespace are required")
		}

		result, err := client.RolloutRestart(ctx, kind, name, namespace)
		if err != nil {
			return toolErr("failed to rollout restart resource: %v", err)
		}

		jsonResponse, err := json.Marshal(result)
		if err != nil {
			return toolErr("failed to serialize response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}

// ListContexts returns a handler for the listContexts tool.
// It reads the kubeconfig the server is using and returns all defined contexts.
func ListContexts() func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, request mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		result, err := k8s.ListContexts("")
		if err != nil {
			return toolErr("failed to list contexts: %v", err)
		}

		jsonResponse, err := json.Marshal(result)
		if err != nil {
			return toolErr("failed to serialize response: %v", err)
		}

		return mcp.NewToolResultText(string(jsonResponse)), nil
	}
}
