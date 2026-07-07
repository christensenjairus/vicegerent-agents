package upstream

import (
	"context"
	"errors"
	"testing"
)

func TestIncidentServiceID_ResolvesFromLiveShapeGuess(t *testing.T) {
	c := &fakeCaller{text: `{"service":{"id":"PSERVICEA","summary":"API Service"}}`}
	got, err := IncidentServiceID(context.Background(), c, "PT1")
	if err != nil {
		t.Fatalf("IncidentServiceID: %v", err)
	}
	if got != "PSERVICEA" {
		t.Errorf("IncidentServiceID = %q, want PSERVICEA", got)
	}
	if c.gotTool != pagerdutyGetIncidentTool {
		t.Errorf("gotTool = %q, want %q", c.gotTool, pagerdutyGetIncidentTool)
	}
	if c.gotArgs["incident_id"] != "PT1" {
		t.Errorf("gotArgs[incident_id] = %v, want PT1", c.gotArgs["incident_id"])
	}
}

func TestIncidentServiceID_MissingServiceFieldFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"id":"PT1","title":"some incident"}`}
	_, err := IncidentServiceID(context.Background(), c, "PT1")
	if err == nil {
		t.Fatal("expected an error when the result has no resolvable service id, got nil (would fail open)")
	}
}

func TestIncidentServiceID_EmptyServiceIDFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"service":{"id":""}}`}
	_, err := IncidentServiceID(context.Background(), c, "PT1")
	if err == nil {
		t.Fatal("expected an error for an empty service id, got nil (would fail open)")
	}
}

func TestIncidentServiceID_MalformedJSONFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{not valid json`}
	_, err := IncidentServiceID(context.Background(), c, "PT1")
	if err == nil {
		t.Fatal("expected an error for malformed JSON, got nil (would fail open)")
	}
}

func TestIncidentServiceID_LookupFailurePropagates(t *testing.T) {
	c := &fakeCaller{err: errors.New("connection refused")}
	_, err := IncidentServiceID(context.Background(), c, "PT1")
	if err == nil {
		t.Fatal("expected the underlying CallTool error to propagate, got nil")
	}
}
