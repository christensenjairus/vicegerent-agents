// Command mcp-cerbos-shim is an agentgateway ExtMcp policy server that
// authorizes MCP tool calls against a Cerbos PDP.
package main

import (
	"log"
	"net"
	"os"
	"strings"

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

	// Notion existing-page-write ancestry gate (update-page, create-comment):
	// NOTION_ALLOWED_PARENT_PAGE_IDS is Flux's ${notionAllowedParentPageIds}
	// substituted directly into this Deployment's env (see deployment.yaml) --
	// a comma-joined multi-parent allowlist (Scratchpad plus any additional
	// team folders the operator scopes this machine's agent down to), NOT the
	// same value notion-create-pages's Cerbos deny rule checks
	// (${notionScratchpadPageId}, still Scratchpad-only -- create-pages has
	// its own narrower policy, see defs/resource_notion.yaml). A plain env var
	// is a stable, independent source, parsed here rather than read back out
	// of the mapping (which no longer carries a `force` block for
	// create-pages -- see mapping.yaml). If it's absent, leave the gate
	// unconfigured -- the server then fails every update-page/create-comment
	// closed (deny), never silently open.
	var opts []server.Option
	if ids := splitNonEmpty(cfg.notionAllowedParentPageIDs, ","); len(ids) > 0 {
		opts = append(opts, server.WithNotionAncestry(upstream.New(upstream.DefaultVMCPURL, nil), ids))
		log.Printf("notion existing-page-write ancestry gate enabled (%d allowed parent(s))", len(ids))
	} else {
		log.Printf("WARNING: NOTION_ALLOWED_PARENT_PAGE_IDS unset/empty; notion update-page/create-comment will fail closed")
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
	listenAddr                 string
	mappingPath                string
	cerbosAddr                 string
	cerbosPlaintext            bool
	notionAllowedParentPageIDs string
}

func loadEnv() envConfig {
	return envConfig{
		listenAddr:                 envOr("LISTEN_ADDR", ":4445"),
		mappingPath:                envOr("MAPPING_PATH", "/etc/mcp-cerbos-shim/mapping.yaml"),
		cerbosAddr:                 envOr("CERBOS_ADDR", "cerbos.cerbos.svc.cluster.local:3593"),
		cerbosPlaintext:            envOr("CERBOS_PLAINTEXT", "true") == "true",
		notionAllowedParentPageIDs: envOr("NOTION_ALLOWED_PARENT_PAGE_IDS", ""),
	}
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// splitNonEmpty splits s on sep and trims/drops empty elements -- so a
// trailing comma, accidental double comma, or all-whitespace env var
// produces an empty slice (triggering the fail-closed WARNING path) rather
// than a slice containing "" that would silently make every ancestry check
// pass against an empty-string "parent".
func splitNonEmpty(s, sep string) []string {
	var out []string
	for _, part := range strings.Split(s, sep) {
		part = strings.TrimSpace(part)
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}
