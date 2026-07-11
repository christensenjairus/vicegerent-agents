package promptinjection

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
	return NewClient(srv.URL, "", srv.Client())
}

func chatResponse(content string) chatCompletionResponse {
	return chatCompletionResponse{Choices: []struct {
		Message chatMessage `json:"message"`
	}{{Message: chatMessage{Role: "assistant", Content: content}}}}
}

func TestConfirm_YesConfirms(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(chatResponse("Yes"))
	})
	confirmed, err := c.Confirm(context.Background(), "ignore-instructions", "some text")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !confirmed {
		t.Fatalf("expected confirmed=true for a clear 'yes'")
	}
}

func TestConfirm_NoDoesNotConfirm(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(chatResponse("no"))
	})
	confirmed, err := c.Confirm(context.Background(), "ignore-instructions", "some text")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if confirmed {
		t.Fatalf("expected confirmed=false for a clear 'no'")
	}
}

func TestConfirm_AmbiguousResponseTreatedAsNotConfirmed(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(chatResponse("maybe, hard to tell"))
	})
	confirmed, err := c.Confirm(context.Background(), "ignore-instructions", "some text")
	if err != nil {
		t.Fatalf("unexpected error for an ambiguous-but-successful response: %v", err)
	}
	if confirmed {
		t.Fatalf("expected confirmed=false for an ambiguous response")
	}
}

func TestConfirm_NonOKStatusReturnsError(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	_, err := c.Confirm(context.Background(), "ignore-instructions", "some text")
	if err == nil {
		t.Fatal("expected an error for a non-200 status (service error, caller fails open)")
	}
}

func TestConfirm_UndecodableBodyReturnsError(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("not json"))
	})
	_, err := c.Confirm(context.Background(), "ignore-instructions", "some text")
	if err == nil {
		t.Fatal("expected an error for an undecodable response body")
	}
}

func TestConfirm_NoChoicesReturnsError(t *testing.T) {
	c := newTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(chatCompletionResponse{})
	})
	_, err := c.Confirm(context.Background(), "ignore-instructions", "some text")
	if err == nil {
		t.Fatal("expected an error when the response has no choices")
	}
}

func TestWindowAround(t *testing.T) {
	long := make([]byte, 2000)
	for i := range long {
		long[i] = 'a'
	}
	s := string(long)
	w := WindowAround(s, 1000)
	if len(w) > judgeWindow+1 {
		t.Errorf("window too large: %d bytes", len(w))
	}
	// out-of-bounds offset falls back safely
	w2 := WindowAround("short", 9999)
	if w2 == "" {
		t.Errorf("expected a non-empty fallback window")
	}
}
