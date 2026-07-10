package moderation

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func newTestClient(t *testing.T, handler http.HandlerFunc) *Client {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	return New(srv.URL, "", srv.Client())
}

func TestCheck_NonFlagged(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		var req moderationRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		if len(req.Input) != 1 || req.Input[0] != "a perfectly normal string" {
			t.Fatalf("unexpected request input: %v", req.Input)
		}
		json.NewEncoder(w).Encode(moderationResponse{
			Results: []struct {
				Flagged    bool               `json:"flagged"`
				Categories map[string]bool    `json:"categories"`
				Scores     map[string]float64 `json:"category_scores"`
			}{{Flagged: false}},
		})
	})
	res, err := c.Check(context.Background(), []string{"a perfectly normal string"})
	if err != nil {
		t.Fatalf("Check: %v", err)
	}
	if res.Flagged {
		t.Errorf("expected non-flagged result")
	}
}

func TestCheck_Flagged(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(moderationResponse{
			Results: []struct {
				Flagged    bool               `json:"flagged"`
				Categories map[string]bool    `json:"categories"`
				Scores     map[string]float64 `json:"category_scores"`
			}{{Flagged: true, Categories: map[string]bool{"harassment": true, "violence": false}}},
		})
	})
	res, err := c.Check(context.Background(), []string{"some flagged content"})
	if err != nil {
		t.Fatalf("Check: %v", err)
	}
	if !res.Flagged {
		t.Fatalf("expected flagged result")
	}
	if len(res.FlaggedCategories) != 1 || res.FlaggedCategories[0] != "harassment" {
		t.Errorf("got categories %v, want [harassment]", res.FlaggedCategories)
	}
}

func TestCheck_EmptyInputSkipsCallEntirely(t *testing.T) {
	called := false
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		called = true
	})
	res, err := c.Check(context.Background(), nil)
	if err != nil {
		t.Fatalf("Check: %v", err)
	}
	if res.Flagged {
		t.Errorf("expected non-flagged result for empty input")
	}
	if called {
		t.Errorf("no HTTP call should be made for empty input")
	}
}

func TestCheck_ShortStringsFilteredClientSide(t *testing.T) {
	called := false
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		called = true
		json.NewEncoder(w).Encode(moderationResponse{})
	})
	// All inputs are short/id-shaped (<4 chars after trim) -- none should
	// reach the HTTP call.
	res, err := c.Check(context.Background(), []string{"", "  ", "id", "42"})
	if err != nil {
		t.Fatalf("Check: %v", err)
	}
	if res.Flagged {
		t.Errorf("expected non-flagged result")
	}
	if called {
		t.Errorf("no HTTP call should be made when every input is filtered out")
	}
}

func TestCheck_NonOKStatusReturnsError(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	})
	_, err := c.Check(context.Background(), []string{"real content here"})
	if err == nil {
		t.Fatalf("expected an error for a non-200 response")
	}
}

func TestCheck_MalformedResponseReturnsError(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("not json"))
	})
	_, err := c.Check(context.Background(), []string{"real content here"})
	if err == nil {
		t.Fatalf("expected an error for a malformed response body")
	}
}

func TestNew_ModelOverride(t *testing.T) {
	var gotModel string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req moderationRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		gotModel = req.Model
		json.NewEncoder(w).Encode(moderationResponse{})
	}))
	defer srv.Close()

	t.Run("empty model falls back to DefaultModel", func(t *testing.T) {
		c := New(srv.URL, "", srv.Client())
		if _, err := c.Check(context.Background(), []string{"real content here"}); err != nil {
			t.Fatalf("Check: %v", err)
		}
		if gotModel != DefaultModel {
			t.Errorf("model = %q, want DefaultModel %q", gotModel, DefaultModel)
		}
	})

	t.Run("explicit model overrides DefaultModel", func(t *testing.T) {
		c := New(srv.URL, "some-other-model", srv.Client())
		if _, err := c.Check(context.Background(), []string{"real content here"}); err != nil {
			t.Fatalf("Check: %v", err)
		}
		if gotModel != "some-other-model" {
			t.Errorf("model = %q, want %q", gotModel, "some-other-model")
		}
	})
}
