package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// Fixtures are built via string concatenation so no literal credential-pattern
// string sits verbatim in this source file -- keeps this repo's detect-secrets
// commit hook from flagging the test file itself. Same convention as
// mcp-cerbos-shim's server_test.go.

// TestRedactGitleaks_CatchesDefaultRulesetSecret proves the shared Detector
// actually fires on a secret shape from gitleaks' embedded default ruleset.
// SendGrid API tokens (rule "sendgrid-api-token", shape "SG." + 66 chars) are
// the exemplar the shim uses too: they are NOT in scrub.py's hand-rolled regex
// registry, so redacting one can only be gitleaks doing it -- exactly the
// coverage this sidecar exists to add on top of the Python regex layer.
func TestRedactGitleaks_CatchesDefaultRulesetSecret(t *testing.T) {
	// SG. + 66 chars from [a-z0-9=_\-.], split across concatenated fragments.
	sendgridKey := "S" + "G." + strings.Repeat("a", 22) + "." + strings.Repeat("b", 43) // pragma: allowlist secret
	if got := len("SG.") + 66; got != len(sendgridKey) {
		t.Fatalf("sendgrid fixture is malformed: want %d chars, got %d", got, len(sendgridKey))
	}
	out, n := redactGitleaks("sendgrid credential: " + sendgridKey + " (do not leak)")
	if n == 0 {
		t.Fatalf("expected gitleaks to redact the SendGrid token, got zero redactions")
	}
	if strings.Contains(out, sendgridKey) {
		t.Errorf("SendGrid token survived redaction: %q", out)
	}
	if !strings.Contains(out, redactedPlaceholder) {
		t.Errorf("expected redaction placeholder in output: %q", out)
	}
}

// TestRedactGitleaks_LeavesOrdinaryTextUntouched guards against a Detector that
// redacts everything -- ordinary prose must survive with zero replacements so
// the sidecar never mangles a legitimate request/response body.
func TestRedactGitleaks_LeavesOrdinaryTextUntouched(t *testing.T) {
	in := "This PR closes the auth bug, no credentials involved."
	out, n := redactGitleaks(in)
	if n != 0 {
		t.Fatalf("expected zero redactions on ordinary text, got %d -> %q", n, out)
	}
	if out != in {
		t.Errorf("ordinary text was altered: %q -> %q", in, out)
	}
}

// TestRedactGitleaks_Idempotent proves a second pass over already-redacted text
// finds nothing new and the placeholder itself is never mistaken for a secret,
// so the scrub converges (scrub.py may run this on both a request and a later
// response that echoes it).
func TestRedactGitleaks_Idempotent(t *testing.T) {
	sendgridKey := "S" + "G." + strings.Repeat("c", 22) + "." + strings.Repeat("d", 43) // pragma: allowlist secret
	first, n1 := redactGitleaks("token=" + sendgridKey)
	if n1 == 0 {
		t.Fatalf("expected at least one redaction on first pass")
	}
	second, n2 := redactGitleaks(first)
	if n2 != 0 {
		t.Errorf("second pass over already-redacted text should redact nothing, got %d", n2)
	}
	if second != first {
		t.Errorf("second pass mutated already-redacted text:\n first=%q\nsecond=%q", first, second)
	}
}

// TestHandleRedact_RoundTrips exercises the HTTP wire shape end-to-end: a POST
// with a secret-bearing body returns redacted text and a positive count.
func TestHandleRedact_RoundTrips(t *testing.T) {
	sendgridKey := "S" + "G." + strings.Repeat("e", 22) + "." + strings.Repeat("f", 43) // pragma: allowlist secret
	body, _ := json.Marshal(redactRequest{Text: "leak: " + sendgridKey})
	rr := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/redact", bytes.NewReader(body))
	newMux().ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}
	var resp redactResponse
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatalf("response is not valid JSON: %v (%s)", err, rr.Body.String())
	}
	if resp.Count == 0 {
		t.Errorf("expected a positive redaction count, got %d", resp.Count)
	}
	if strings.Contains(resp.Text, sendgridKey) {
		t.Errorf("secret survived through the HTTP path: %q", resp.Text)
	}
}

// TestHandleRedact_CleanBody confirms a body with nothing to redact returns the
// text unchanged and count 0 -- the hot-path common case for most traffic.
func TestHandleRedact_CleanBody(t *testing.T) {
	body, _ := json.Marshal(redactRequest{Text: "just some ordinary response text"})
	rr := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/redact", bytes.NewReader(body))
	newMux().ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr.Code)
	}
	var resp redactResponse
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatalf("response is not valid JSON: %v", err)
	}
	if resp.Count != 0 || resp.Text != "just some ordinary response text" {
		t.Errorf("clean body was altered: count=%d text=%q", resp.Count, resp.Text)
	}
}

// TestHandleRedact_RejectsGet ensures the endpoint only serves POST -- a stray
// GET (e.g. a probe hitting the wrong path) gets 405, not a decode panic.
func TestHandleRedact_RejectsGet(t *testing.T) {
	rr := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/redact", nil)
	newMux().ServeHTTP(rr, req)
	if rr.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405 for GET /redact, got %d", rr.Code)
	}
}

// TestHandleRedact_BadJSON returns 400 rather than 500/panic on a malformed
// body.
func TestHandleRedact_BadJSON(t *testing.T) {
	rr := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/redact", strings.NewReader("{not json"))
	newMux().ServeHTTP(rr, req)
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for malformed JSON, got %d", rr.Code)
	}
}

// TestHandleHealthz is the kubelet liveness/readiness contract: a plain 200.
func TestHandleHealthz(t *testing.T) {
	rr := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	newMux().ServeHTTP(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200 from /healthz, got %d", rr.Code)
	}
}

// TestRunHealthcheck covers the exec-probe path end-to-end: against a live
// listener serving the real mux it returns 0, and against a dead address it
// returns non-zero (so the kubelet restarts a wedged sidecar).
func TestRunHealthcheck(t *testing.T) {
	srv := httptest.NewServer(newMux())
	defer srv.Close()
	addr := strings.TrimPrefix(srv.URL, "http://")
	if rc := runHealthcheck(addr); rc != 0 {
		t.Errorf("expected healthcheck rc 0 against a live server, got %d", rc)
	}
	// Port 1 has nothing listening -> connection refused -> non-zero.
	if rc := runHealthcheck("127.0.0.1:1"); rc == 0 {
		t.Errorf("expected non-zero healthcheck rc against a dead address")
	}
}
