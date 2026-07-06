// Command mcp-cerbos-shim is an agentgateway ExtMcp policy server that
// authorizes MCP tool calls against a Cerbos PDP.
package main

import (
	"log"
	"net"
	"os"

	"github.com/cerbos/cerbos-sdk-go/cerbos"
	"google.golang.org/grpc"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/authz"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/server"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
	pb "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/proto/gen"
)

func main() {
	cfg := loadEnv()

	// Startup failure is fail-closed: k8s restarts the pod, gateway FailClosed denies meanwhile.
	mapping, err := config.Load(cfg.mappingPath)
	if err != nil {
		log.Fatalf("FATAL load mapping: %v", err)
	}
	engine, err := eval.Compile(mapping)
	if err != nil {
		log.Fatalf("FATAL compile mapping expressions: %v", err)
	}

	var cerbosOpts []cerbos.Opt
	if cfg.cerbosPlaintext {
		cerbosOpts = append(cerbosOpts, cerbos.WithPlaintext())
	}
	decider, err := authz.New(cfg.cerbosAddr, cerbosOpts...)
	if err != nil {
		log.Fatalf("FATAL connect cerbos: %v", err)
	}

	// Notion update-page ancestry gate: NOTION_SCRATCHPAD_PAGE_ID is Flux's
	// ${notionScratchpadPageId} substituted directly into this Deployment's env
	// (see deployment.yaml) -- the SAME cluster-var notion-create-pages's Cerbos
	// deny rule checks against (defs/resource_notion.yaml), so create and update
	// share one source of truth. This is intentionally NOT read back out of the
	// mapping's notion-create-pages entry: that tool no longer carries a `force`
	// block (it now denies via Cerbos, see mapping.yaml), so parsing it out of
	// there would silently break this gate the moment that tool's mapping shape
	// changes again. A plain env var is a stable, independent source. If it's
	// absent, leave the gate unconfigured -- the server then fails every
	// update-page closed (deny), never silently open.
	var opts []server.Option
	if spid := cfg.notionScratchpadPageID; spid != "" {
		opts = append(opts, server.WithNotionAncestry(upstream.New(upstream.DefaultVMCPURL, nil), spid))
		log.Printf("notion update-page ancestry gate enabled")
	} else {
		log.Printf("WARNING: NOTION_SCRATCHPAD_PAGE_ID unset; notion update-page will fail closed")
	}

	srv := server.New(mapping, engine, decider, server.Principal{
		ID:    "hermes",
		Roles: []string{"agent"},
	}, opts...)

	lis, err := net.Listen("tcp", cfg.listenAddr)
	if err != nil {
		log.Fatalf("FATAL listen %s: %v", cfg.listenAddr, err)
	}
	gs := grpc.NewServer()
	pb.RegisterExtMcpServer(gs, srv)
	log.Printf("mcp-cerbos-shim listening on %s; cerbos=%s; backends=%d",
		cfg.listenAddr, cfg.cerbosAddr, len(mapping.Backends))
	if err := gs.Serve(lis); err != nil {
		log.Fatalf("FATAL serve: %v", err)
	}
}

type envConfig struct {
	listenAddr             string
	mappingPath            string
	cerbosAddr             string
	cerbosPlaintext        bool
	notionScratchpadPageID string
}

func loadEnv() envConfig {
	return envConfig{
		listenAddr:             envOr("LISTEN_ADDR", ":4445"),
		mappingPath:            envOr("MAPPING_PATH", "/etc/mcp-cerbos-shim/mapping.yaml"),
		cerbosAddr:             envOr("CERBOS_ADDR", "cerbos.cerbos.svc.cluster.local:3593"),
		cerbosPlaintext:        envOr("CERBOS_PLAINTEXT", "true") == "true",
		notionScratchpadPageID: envOr("NOTION_SCRATCHPAD_PAGE_ID", ""),
	}
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
