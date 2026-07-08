// Command egress-gitleaks-sidecar is a tiny localhost-only HTTP service that
// runs gitleaks' embedded default ruleset over arbitrary strings on behalf of
// the egress-proxy's mitmproxy scrub.py addon.
//
// Why a sidecar at all: gitleaks (github.com/zricethezav/gitleaks/v8) is a Go
// library with no Python binding, and mitmproxy's addon is Python. The two
// alternatives -- shelling out to a gitleaks CLI per request (subprocess +
// temp-file overhead per call, and the CLI is oriented around file/git
// scanning, not stdin-string scanning) or reimplementing ~180 rules in Python
// -- are both worse than running the exact same detect.Detector the
// mcp-cerbos-shim already proved out, once, as a second container in the same
// Pod. scrub.py POSTs each string it wants scanned to 127.0.0.1 and gets back
// the gitleaks-redacted text plus a count.
//
// This is the SECOND redaction layer for egress traffic; scrub.py still owns
// the hand-rolled regex registry (the Python mirror of secrets_redact.go's
// secretPatternRegistry) as the first layer. Keeping gitleaks here and the
// registry there avoids maintaining two copies of the ~180-rule ruleset while
// still giving request+response traffic the same two-layer coverage the shim
// gives MCP tool calls. See charts/egress-proxy/templates/addon-configmap.yaml
// and images/mcp-cerbos-shim/internal/server/secrets_redact.go.
//
// Reachability: it binds 127.0.0.1 ONLY (never 0.0.0.0). Containers in a Pod
// share a network namespace, so the mitmproxy container reaches it over
// loopback; nothing outside the Pod can. Intra-pod loopback traffic is not
// subject to Cilium/NetworkPolicy enforcement (policy is enforced on traffic
// crossing the pod boundary), so no CiliumNetworkPolicy rule is needed or
// added for this port -- see charts/egress-proxy/templates/networkpolicy.yaml.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/rs/zerolog"
	"github.com/zricethezav/gitleaks/v8/detect"
)

const redactedPlaceholder = "<masked>"

// gitleaksDetector is the single, process-wide gitleaks Detector shared across
// every /redact call. It MUST be built exactly once: NewDetectorDefaultConfig
// mutates a global viper singleton (the only data race in the whole flow) and
// construction costs ~20ms -- fine as one-time init, ruinous per-request. Once
// built, DetectString is safe for concurrent use (it accumulates into a local
// slice with no shared mutable state), which matters because net/http serves
// each request on its own goroutine. If construction fails this stays nil and
// /redact degrades to a no-op (count 0, text unchanged) -- scrub.py's own regex
// registry still runs, so a broken gitleaks init weakens coverage but never
// takes egress traffic (or this sidecar) down. Mirrors the exact same
// build-once/share pattern proven race-safe in mcp-cerbos-shim's
// secrets_redact.go.
var gitleaksDetector *detect.Detector

func init() {
	// gitleaks logs through zerolog's global logger (e.g. a Debug line about a
	// missing .gitleaksignore). Silence it once so none of it leaks into this
	// sidecar's own stdlib-log container output. This sidecar logs via the
	// standard "log" package, not zerolog, so this only gags gitleaks.
	zerolog.SetGlobalLevel(zerolog.Disabled)

	d, err := detect.NewDetectorDefaultConfig()
	if err != nil {
		log.Printf("gitleaks detector init failed; /redact will no-op and "+
			"scrub.py falls back to its regex registry only: %v", err)
		return
	}
	gitleaksDetector = d
}

// redactRequest/redactResponse are the /redact JSON wire shape.
type redactRequest struct {
	Text string `json:"text"`
}

type redactResponse struct {
	Text  string `json:"text"`
	Count int    `json:"count"`
}

// redactGitleaks runs the shared gitleaks Detector over s and replaces every
// finding's secret substring with redactedPlaceholder, returning the scrubbed
// string and the replacement count. It prefers Finding.Secret (the exact
// matched credential) and falls back to Finding.Match when Secret is empty. A
// finding whose secret text is empty, is already the placeholder, or no longer
// appears in s (because an earlier finding's replacement removed a shared
// substring) contributes nothing -- so two findings pointing at the same secret
// can't double-count. This is the exact logic of secrets_redact.go's
// redactStringGitleaks, so the sidecar and the shim scrub identically.
func redactGitleaks(s string) (string, int) {
	if gitleaksDetector == nil {
		return s, 0
	}
	findings := gitleaksDetector.DetectString(s)
	if len(findings) == 0 {
		return s, 0
	}
	total := 0
	for _, f := range findings {
		matched := f.Secret
		if matched == "" {
			matched = f.Match
		}
		if matched == "" || matched == redactedPlaceholder {
			continue
		}
		n := strings.Count(s, matched)
		if n == 0 {
			continue
		}
		s = strings.ReplaceAll(s, matched, redactedPlaceholder)
		total += n
	}
	return s, total
}

func handleRedact(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var req redactRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	scrubbed, count := redactGitleaks(req.Text)
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(redactResponse{Text: scrubbed, Count: count})
}

func handleHealthz(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}

func newMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/redact", handleRedact)
	mux.HandleFunc("/healthz", handleHealthz)
	return mux
}

func listenAddr() string {
	// Default to loopback-only; never 0.0.0.0. Overridable for tests, but the
	// deployment relies on the loopback bind for its no-external-reachability
	// property -- do not point this at a routable interface in production.
	if addr := os.Getenv("LISTEN_ADDR"); addr != "" {
		return addr
	}
	return "127.0.0.1:8081"
}

// runHealthcheck GETs the local /healthz and exits 0 on 200, non-zero otherwise.
// It is the kubelet liveness/readiness probe: because the server binds loopback
// only, the kubelet cannot reach it with a networked httpGet/tcpSocket probe
// (those dial the pod IP, not 127.0.0.1). Running this binary with -healthcheck
// as an `exec` probe instead runs INSIDE the container's network namespace, so
// it reaches the loopback listener -- and the distroless image has no shell for
// a curl/wget-based exec probe. This keeps the loopback-only bind AND a working
// probe.
func runHealthcheck(addr string) int {
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get("http://" + addr + "/healthz")
	if err != nil {
		fmt.Fprintf(os.Stderr, "healthcheck: %v\n", err)
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		fmt.Fprintf(os.Stderr, "healthcheck: status %d\n", resp.StatusCode)
		return 1
	}
	return 0
}

func main() {
	healthcheck := flag.Bool("healthcheck", false,
		"probe the local /healthz and exit 0/1 (for use as a kubelet exec probe)")
	flag.Parse()

	addr := listenAddr()
	if *healthcheck {
		os.Exit(runHealthcheck(addr))
	}

	log.Printf("egress-gitleaks-sidecar listening on %s; gitleaks_enabled=%t",
		addr, gitleaksDetector != nil)
	if err := http.ListenAndServe(addr, newMux()); err != nil {
		log.Fatalf("FATAL serve: %v", err)
	}
}
