package eval

import (
	"reflect"
	"sort"
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
)

func TestLinearProjectHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("linearProjectAttr"); !ok {
		t.Fatal("linearProjectAttr not registered; helpers_linear.go init() did not run")
	}
}

// TestLinearProjectAttrEval exercises linearProjectAttr end-to-end through
// the Engine (compile + eval + toAnyResultMap) since this helper is the
// first to produce a list-valued attr; the map[string]string-only helpers
// don't exercise that code path.
func TestLinearProjectAttrEval(t *testing.T) {
	m := &config.Mapping{Backends: map[string]config.Backend{
		"linear": {
			Helpers: []string{"linearProjectAttr"},
			Tools: map[string]config.Tool{
				"save_project": {
					ResourceType: "linear_team",
					ID:           "get(args,'id','*')",
					AttrFrom:     "linearProjectAttr(args)",
				},
			},
		},
	}}
	eng, err := Compile(m)
	if err != nil {
		t.Fatalf("Compile: %v", err)
	}

	cases := []struct {
		name string
		args map[string]any
		want []string // nil means no "teams" key at all
	}{
		{
			name: "addTeams only",
			args: map[string]any{"addTeams": []any{"DEVOPS"}},
			want: []string{"DEVOPS"},
		},
		{
			name: "setTeams only",
			args: map[string]any{"setTeams": []any{"some-other-team"}},
			want: []string{"some-other-team"},
		},
		{
			name: "addTeams and setTeams union",
			args: map[string]any{
				"addTeams": []any{"DEVOPS"},
				"setTeams": []any{"some-other-team"},
			},
			want: []string{"DEVOPS", "some-other-team"},
		},
		{
			name: "neither present falls through (no teams key)",
			args: map[string]any{"name": "Q3 roadmap"},
			want: nil,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res, err := eng.Eval(CallInput{Tool: "save_project", Backend: "linear", Args: c.args})
			if err != nil {
				t.Fatalf("Eval: %v", err)
			}
			v, ok := res.Attr["teams"]
			if c.want == nil {
				if ok {
					t.Fatalf("expected no teams key, got %v", v)
				}
				return
			}
			if !ok {
				t.Fatalf("expected teams key, got none; attr=%v", res.Attr)
			}
			got, ok := v.([]string)
			if !ok {
				t.Fatalf("expected []string, got %T (%v)", v, v)
			}
			sort.Strings(got)
			want := append([]string(nil), c.want...)
			sort.Strings(want)
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("teams = %v, want %v", got, want)
			}
		})
	}
}

func TestLinearIssueHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("linearIssueAttr"); !ok {
		t.Fatal("linearIssueAttr not registered; helpers_linear.go init() did not run")
	}
}

// TestLinearIssueAttrEval covers the isCreate/teamId/assignee attr surface
// added for the force-self-assignee guardrail (resource_linear.yaml's
// deny-create-missing-assignee + deny-assignee-outside-allowed rules).
// has()-presence matters here: a key must be OMITTED, not empty-stringed,
// when not verifiable, since Cerbos's has() checks key presence.
func TestLinearIssueAttrEval(t *testing.T) {
	m := &config.Mapping{Backends: map[string]config.Backend{
		"linear": {
			Helpers: []string{"linearIssueAttr"},
			Tools: map[string]config.Tool{
				"save_issue": {
					ResourceType: "linear_team",
					ID:           "get(args,'id', get(args,'team',''))",
					AttrFrom:     "linearIssueAttr(args)",
				},
			},
		},
	}}
	eng, err := Compile(m)
	if err != nil {
		t.Fatalf("Compile: %v", err)
	}

	cases := []struct {
		name         string
		args         map[string]any
		wantIsCreate string
		wantTeamID   *string // nil means no teamId key at all
		wantAssignee *string // nil means no assignee key at all
	}{
		{
			name:         "create with team and assignee",
			args:         map[string]any{"title": "New issue", "team": "DEVOPS", "assignee": "jchristensen@moveworks.ai"},
			wantIsCreate: "true",
			wantTeamID:   strPtr("DEVOPS"),
			wantAssignee: strPtr("jchristensen@moveworks.ai"),
		},
		{
			name:         "create with no assignee at all",
			args:         map[string]any{"title": "New issue", "team": "DEVOPS"},
			wantIsCreate: "true",
			wantTeamID:   strPtr("DEVOPS"),
			wantAssignee: nil,
		},
		{
			name:         "update with no team, no assignee (ordinary update)",
			args:         map[string]any{"id": "LIN-123", "state": "Done"},
			wantIsCreate: "false",
			wantTeamID:   nil,
			wantAssignee: nil,
		},
		{
			name:         "update that reassigns",
			args:         map[string]any{"id": "LIN-123", "assignee": "someone-else@example.com"},
			wantIsCreate: "false",
			wantTeamID:   nil,
			wantAssignee: strPtr("someone-else@example.com"),
		},
		{
			name:         "update with team reassignment",
			args:         map[string]any{"id": "LIN-123", "team": "some-other-team"},
			wantIsCreate: "false",
			wantTeamID:   strPtr("some-other-team"),
			wantAssignee: nil,
		},
		{
			name:         "create with assignee=me",
			args:         map[string]any{"title": "New issue", "team": "DEVOPS", "assignee": "me"},
			wantIsCreate: "true",
			wantTeamID:   strPtr("DEVOPS"),
			wantAssignee: strPtr("me"),
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res, err := eng.Eval(CallInput{Tool: "save_issue", Backend: "linear", Args: c.args})
			if err != nil {
				t.Fatalf("Eval: %v", err)
			}
			if got, _ := res.Attr["isCreate"].(string); got != c.wantIsCreate {
				t.Errorf("isCreate = %q, want %q", got, c.wantIsCreate)
			}
			checkOptStringAttr(t, res.Attr, "teamId", c.wantTeamID)
			checkOptStringAttr(t, res.Attr, "assignee", c.wantAssignee)
		})
	}
}

func checkOptStringAttr(t *testing.T, attr map[string]any, key string, want *string) {
	t.Helper()
	v, ok := attr[key]
	if want == nil {
		if ok {
			t.Errorf("expected no %s key, got %v", key, v)
		}
		return
	}
	if !ok {
		t.Fatalf("expected %s key %q, got none; attr=%v", key, *want, attr)
	}
	got, ok := v.(string)
	if !ok {
		t.Fatalf("expected %s to be string, got %T (%v)", key, v, v)
	}
	if got != *want {
		t.Errorf("%s = %q, want %q", key, got, *want)
	}
}

func strPtr(s string) *string { return &s }
