package upstream

import (
	"context"
	"errors"
	"testing"
)

func TestIncidentServiceID_ResolvesFromLiveShapeGuess(t *testing.T) {
	c := &fakeCaller{text: `{"service":{"id":"PSERVICEA","summary":"API Service"}}`}
	got, err := IncidentServiceID(context.Background(), "pagerduty_get_incident", c, "PT1")
	if err != nil {
		t.Fatalf("IncidentServiceID: %v", err)
	}
	if got != "PSERVICEA" {
		t.Errorf("IncidentServiceID = %q, want PSERVICEA", got)
	}
	if c.gotTool != "pagerduty_get_incident" {
		t.Errorf("gotTool = %q, want %q", c.gotTool, "pagerduty_get_incident")
	}
	if c.gotArgs["incident_id"] != "PT1" {
		t.Errorf("gotArgs[incident_id] = %v, want PT1", c.gotArgs["incident_id"])
	}
}

func TestIncidentServiceID_QueriesTheGivenBackendsGetIncidentTool(t *testing.T) {
	// A gov-originated call must look the incident up via the GOV backend's own
	// get_incident tool, not the (default/commercial) one -- the incident only
	// exists in the account it actually belongs to.
	c := &fakeCaller{text: `{"service":{"id":"PSERVICEB","summary":"Gov Service"}}`}
	got, err := IncidentServiceID(context.Background(), "pagerduty_gov_get_incident", c, "PT2")
	if err != nil {
		t.Fatalf("IncidentServiceID: %v", err)
	}
	if got != "PSERVICEB" {
		t.Errorf("IncidentServiceID = %q, want PSERVICEB", got)
	}
	if c.gotTool != "pagerduty_gov_get_incident" {
		t.Errorf("gotTool = %q, want %q", c.gotTool, "pagerduty_gov_get_incident")
	}
}

func TestIncidentServiceID_MissingServiceFieldFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"id":"PT1","title":"some incident"}`}
	_, err := IncidentServiceID(context.Background(), "pagerduty_get_incident", c, "PT1")
	if err == nil {
		t.Fatal("expected an error when the result has no resolvable service id, got nil (would fail open)")
	}
}

func TestIncidentServiceID_EmptyServiceIDFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"service":{"id":""}}`}
	_, err := IncidentServiceID(context.Background(), "pagerduty_get_incident", c, "PT1")
	if err == nil {
		t.Fatal("expected an error for an empty service id, got nil (would fail open)")
	}
}

func TestIncidentServiceID_MalformedJSONFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{not valid json`}
	_, err := IncidentServiceID(context.Background(), "pagerduty_get_incident", c, "PT1")
	if err == nil {
		t.Fatal("expected an error for malformed JSON, got nil (would fail open)")
	}
}

func TestIncidentServiceID_LookupFailurePropagates(t *testing.T) {
	c := &fakeCaller{err: errors.New("connection refused")}
	_, err := IncidentServiceID(context.Background(), "pagerduty_get_incident", c, "PT1")
	if err == nil {
		t.Fatal("expected the underlying CallTool error to propagate, got nil")
	}
}
