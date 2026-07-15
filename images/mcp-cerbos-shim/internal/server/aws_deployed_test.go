package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// Runs the SHIPPED mapping through the request path with the live vMCP tool
// names (backend "vmcp", tool "aws_call_aws") to prove the wiring that turns a
// call_aws command into the aws_command resource carrying the parsed awsOps
// list Cerbos denies for Secrets Manager value-reads; the deny *decision* is
// proven by defs/aws_test.yaml.

func TestDeployedAwsMapping_SecretReadReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	cases := []struct {
		name string
		cmd  any
		want string
	}{
		{"get-secret-value", "aws secretsmanager get-secret-value --secret-id foo", "secretsmanager/get-secret-value"},
		{"batch-get-secret-value", "aws --region us-west-2 secretsmanager batch-get-secret-value --secret-id-list a", "secretsmanager/batch-get-secret-value"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall("aws_call_aws", map[string]any{"cli_command": tc.cmd})))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isDeny(res) {
				t.Fatalf("expected deny when Cerbos denies, got pass")
			}
			assertNoSideEffects(t, res)
			if d.gotType != "aws_command" {
				t.Errorf("resourceType = %q, want aws_command", d.gotType)
			}
			ops, ok := d.gotAttr["awsOps"].([]string)
			if !ok {
				t.Fatalf("attr.awsOps not a []string: %T (%v)", d.gotAttr["awsOps"], d.gotAttr["awsOps"])
			}
			found := false
			for _, o := range ops {
				if o == tc.want {
					found = true
				}
			}
			if !found {
				t.Errorf("attr.awsOps = %v, want to contain %q", ops, tc.want)
			}
		})
	}
}

func TestDeployedAwsMapping_NonSecretCommandPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("aws_call_aws", map[string]any{"cli_command": "aws s3api list-buckets"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for a non-secret command")
	}
	if d.gotType != "aws_command" {
		t.Errorf("resourceType = %q, want aws_command", d.gotType)
	}
}

func TestDeployedAwsMapping_SuggestUnmapped(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// suggest_aws_commands only returns suggestion text; it's deliberately
	// unmapped -> passes without a Cerbos call.
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("aws_suggest_aws_commands", map[string]any{"query": "how do I read a secret"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for unmapped suggest_aws_commands")
	}
	if d.calls != 0 {
		t.Errorf("unmapped tool must not call Cerbos, got %d calls", d.calls)
	}
}
