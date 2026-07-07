package eval

import (
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
)

func TestGithubHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("githubReviewersAttr"); !ok {
		t.Fatal("githubReviewersAttr not registered; helpers_github.go init() did not run")
	}
}

func compileGithubTestEngine(t *testing.T) *Engine {
	t.Helper()
	m := &config.Mapping{
		Backends: map[string]config.Backend{
			"vmcp": {
				DefaultAction: config.ActionAllow,
				Helpers:       []string{"githubReviewersAttr"},
				Tools: map[string]config.Tool{
					"github_create_pull_request": {
						ResourceType: "github_repo",
						Action:       "access",
						ID:           "get(args,'owner','') + '/' + get(args,'repo','')",
						AttrFrom:     "githubReviewersAttr(args)",
						Attr: map[string]string{
							"owner": "get(args,'owner','')",
							"repo":  "get(args,'repo','')",
						},
					},
				},
			},
		},
	}
	e, err := Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	return e
}

func TestGithubReviewersAttr_NonEmptyArraySetsTrue(t *testing.T) {
	e := compileGithubTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "github_create_pull_request",
		Args: map[string]any{
			"owner":     "christensenjairus",
			"repo":      "vicegerent-agents",
			"reviewers": []any{"someuser"},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["hasReviewers"]; got != "true" {
		t.Errorf("hasReviewers = %q, want true", got)
	}
}

func TestGithubReviewersAttr_EmptyArraySetsFalse(t *testing.T) {
	e := compileGithubTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "github_create_pull_request",
		Args: map[string]any{
			"owner":     "christensenjairus",
			"repo":      "vicegerent-agents",
			"reviewers": []any{},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["hasReviewers"]; got != "false" {
		t.Errorf("hasReviewers = %q, want false for an empty array", got)
	}
}

func TestGithubReviewersAttr_AbsentSetsFalse(t *testing.T) {
	e := compileGithubTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "github_create_pull_request",
		Args: map[string]any{
			"owner": "christensenjairus",
			"repo":  "vicegerent-agents",
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["hasReviewers"]; got != "false" {
		t.Errorf("hasReviewers = %q, want false when reviewers is absent entirely", got)
	}
}

func TestGithubReviewersAttr_NonEmptyStringSetsTrue(t *testing.T) {
	// Covers a caller sending a comma-joined string instead of a real array.
	e := compileGithubTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "github_create_pull_request",
		Args: map[string]any{
			"owner":     "christensenjairus",
			"repo":      "vicegerent-agents",
			"reviewers": "someuser,anotheruser",
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["hasReviewers"]; got != "true" {
		t.Errorf("hasReviewers = %q, want true for a non-empty string form", got)
	}
}
